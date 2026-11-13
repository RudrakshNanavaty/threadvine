// bench/pipeline.c - validation benchmark for the thread-relationship project.
//
// Shape of one pipeline ("group"):
//
//     producer --queue1--> worker(s) --queue2--> sink
//
// Several groups can run side by side. They never talk to each other, so a
// correct analyzer must NOT draw any edge between two groups.
//
// Ground truth: every time a thread takes an item out of a queue we log who
// put it in (from_tid) and who took it out (to_tid). The analyzer never reads
// these files while inferring relationships; it only uses them for scoring.
//
// Build:  make bench        (or: gcc -O2 -pthread bench/pipeline.c -o pipeline)
// Run:    ./pipeline -h
//
// Exit status: 0 = the run completed and both ground-truth files were written
// completely; 1 = bad options, a thread could not be created, or a file could
// not be written. Never trust a run whose exit status is not 0.
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define MAX_THREADS 1024
#define STAGE_P2W 0 /* producer -> worker */
#define STAGE_W2S 1 /* worker   -> sink   */

/* ---------- settings (filled from the command line) ---------- */
static int cfg_workers = 1;      /* worker threads per group              */
static int cfg_groups = 1;       /* independent pipelines                 */
static long cfg_items = 200;     /* items sent through each group         */
static long cfg_period_us = 10000; /* time between arrivals (0 = as fast as possible) */
static long cfg_cost_us = 5000;  /* work per item in a worker             */
static int cfg_spin = 0;         /* 0: sleep for the cost, 1: burn CPU    */
static int cfg_qsize = 8;        /* bounded queue capacity                */
static int cfg_delay_s = 0;      /* wait before starting (time to attach the collector) */
static const char *cfg_prefix = "groundtruth";

/* ---------- small helpers ---------- */
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts); /* same clock as bpf_ktime_get_ns() */
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* The kernel (and the eBPF collector) identify threads by this number. */
static int my_tid(void)
{
    return (int)syscall(SYS_gettid);
}

static void sleep_us(long us)
{
    struct timespec ts = { us / 1000000, (us % 1000000) * 1000 };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR)
        ;
}

static void sleep_until(uint64_t t_ns)
{
    struct timespec ts = { (time_t)(t_ns / 1000000000ull), (long)(t_ns % 1000000000ull) };
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR)
        ;
}

/* Burn CPU for `us` microseconds of THIS THREAD's CPU time. Counting CPU time
 * (not wall time) means the amount of work stays fixed: if the thread is
 * preempted, it simply takes longer on the clock. */
static void spin_us(long us)
{
    struct timespec a, b;
    long long target = (long long)us * 1000;

    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &a);
    do {
        clock_gettime(CLOCK_THREAD_CPUTIME_ID, &b);
    } while ((long long)(b.tv_sec - a.tv_sec) * 1000000000LL + (b.tv_nsec - a.tv_nsec) < target);
}

static void do_work(void)
{
    if (cfg_cost_us <= 0)
        return;
    if (cfg_spin)
        spin_us(cfg_cost_us);
    else
        sleep_us(cfg_cost_us);
}

/* ---------- bounded queue (mutex + two condition variables) ---------- */
typedef struct {
    int64_t id;        /* item number; -1 = "no more work" marker */
    int32_t sender_tid;/* thread that put it in the queue         */
    uint64_t t0_ns;    /* intended arrival time (for latency)     */
    uint64_t put_ns;   /* when the sender put it in the queue     */
} Item;

typedef struct {
    Item *slots;
    int cap, head, tail, count;
    pthread_mutex_t lock;          /* one thread at a time inside the queue */
    pthread_cond_t not_empty;      /* sleepers wait here for data           */
    pthread_cond_t not_full;       /* sleepers wait here for free space     */
} Queue;

static void queue_init(Queue *q, int cap)
{
    q->slots = calloc((size_t)cap, sizeof(Item));
    if (!q->slots) {
        perror("calloc");
        exit(1);
    }
    q->cap = cap;
    q->head = q->tail = q->count = 0;
    pthread_mutex_init(&q->lock, NULL);
    pthread_cond_init(&q->not_empty, NULL);
    pthread_cond_init(&q->not_full, NULL);
}

static void queue_put(Queue *q, Item it)
{
    pthread_mutex_lock(&q->lock);
    while (q->count == q->cap)                  /* full: sleep until space */
        pthread_cond_wait(&q->not_full, &q->lock);
    it.put_ns = now_ns();
    q->slots[q->tail] = it;
    q->tail = (q->tail + 1) % q->cap;
    q->count++;
    pthread_cond_signal(&q->not_empty);         /* wake ONE sleeping taker */
    pthread_mutex_unlock(&q->lock);
}

static Item queue_get(Queue *q)
{
    pthread_mutex_lock(&q->lock);
    while (q->count == 0)                       /* empty: sleep until data */
        pthread_cond_wait(&q->not_empty, &q->lock);
    Item it = q->slots[q->head];
    q->head = (q->head + 1) % q->cap;
    q->count--;
    pthread_cond_signal(&q->not_full);          /* wake ONE sleeping putter */
    pthread_mutex_unlock(&q->lock);
    return it;
}

/* ---------- ground-truth log (one private buffer per thread: no locking) ---------- */
typedef struct {
    int group, stage, from_tid, to_tid;
    int64_t item;
    uint64_t put_ns, get_ns;
} Handoff;

typedef struct {
    Handoff *v;
    size_t n, cap;
} Log;

static void log_init(Log *l, size_t cap)
{
    l->v = calloc(cap ? cap : 1, sizeof(Handoff));
    if (!l->v) {
        perror("calloc");
        exit(1);
    }
    l->n = 0;
    l->cap = cap;
}

static void log_add(Log *l, int group, int stage, Item it, int to_tid, uint64_t get_ns)
{
    if (l->n >= l->cap) { /* cannot happen: buffers are sized for the worst case */
        fprintf(stderr, "internal error: ground-truth buffer overflow\n");
        abort();
    }
    l->v[l->n++] = (Handoff){ group, stage, it.sender_tid, to_tid, it.id, it.put_ns, get_ns };
}

/* ---------- thread table: tid -> role, so the CSV is readable ---------- */
typedef struct {
    int tid, group, index;
    char role[16];
} ThreadInfo;

static ThreadInfo infos[MAX_THREADS];
static int ninfos;
static pthread_mutex_t info_lock = PTHREAD_MUTEX_INITIALIZER;

static int register_thread(const char *role, int group, int index)
{
    int tid = my_tid();

    pthread_setname_np(pthread_self(), role);
    pthread_mutex_lock(&info_lock);
    if (ninfos < MAX_THREADS) {
        ThreadInfo *t = &infos[ninfos++];
        t->tid = tid;
        t->group = group;
        t->index = index;
        snprintf(t->role, sizeof(t->role), "%s", role);
    }
    pthread_mutex_unlock(&info_lock);
    return tid;
}

/* ---------- the threads ---------- */
typedef struct {
    Queue q1, q2;
} Group;

typedef struct {
    Group *g;
    int group, index;
    Log log;
    uint64_t *lat; /* sink only: end-to-end latency per item */
    size_t nlat;
} ThreadArg;

/* Create a thread, or stop the whole run with a clear message. A benchmark
 * that silently runs with fewer threads would produce a wrong answer key. */
static void spawn(pthread_t *thread, void *(*fn)(void *), ThreadArg *a, const char *role)
{
    int rc = pthread_create(thread, NULL, fn, a);

    if (rc != 0) {
        fprintf(stderr, "error: cannot create %s thread (group %d): %s\n", role, a->group,
                strerror(rc));
        exit(1);
    }
}

static void *producer(void *p)
{
    ThreadArg *a = p;
    int tid = register_thread("producer", a->group, 0);
    uint64_t base = now_ns();

    for (long i = 0; i < cfg_items; i++) {
        uint64_t due = base + (uint64_t)i * (uint64_t)cfg_period_us * 1000ull;
        if (cfg_period_us > 0)
            sleep_until(due);
        else
            due = now_ns();
        /* Open loop: latency is measured from when the item was DUE, so a
         * slow pipeline cannot hide its delay by slowing the producer. */
        queue_put(&a->g->q1, (Item){ i, tid, due, 0 });
    }
    for (int w = 0; w < cfg_workers; w++)       /* one stop marker per worker */
        queue_put(&a->g->q1, (Item){ -1, tid, 0, 0 });
    return NULL;
}

static void *worker(void *p)
{
    ThreadArg *a = p;
    int tid = register_thread("worker", a->group, a->index);

    for (;;) {
        Item it = queue_get(&a->g->q1);
        uint64_t got = now_ns();
        if (it.id < 0)
            break;
        log_add(&a->log, a->group, STAGE_P2W, it, tid, got);
        do_work();
        it.sender_tid = tid;
        queue_put(&a->g->q2, it);
    }
    return NULL;
}

static void *sink(void *p)
{
    ThreadArg *a = p;
    int tid = register_thread("sink", a->group, 0);

    for (long k = 0; k < cfg_items; k++) {         /* knows how many to expect */
        Item it = queue_get(&a->g->q2);
        uint64_t got = now_ns();
        log_add(&a->log, a->group, STAGE_W2S, it, tid, got);
        a->lat[a->nlat++] = got - it.t0_ns;
    }
    return NULL;
}

/* ---------- output ---------- */
static int cmp_u64(const void *x, const void *y)
{
    uint64_t a = *(const uint64_t *)x, b = *(const uint64_t *)y;
    return (a > b) - (a < b);
}

static int cmp_handoff(const void *x, const void *y)
{
    const Handoff *a = x, *b = y;
    return (a->get_ns > b->get_ns) - (a->get_ns < b->get_ns);
}

/* Percentile convention (stated here so results can be reproduced):
 *   - sort the latencies ascending, indices start at 0;
 *   - pXX = the value at index floor(q * n), clamped to n - 1, where q = XX / 100;
 *   - no interpolation; q = 1.0 gives the maximum.
 * Example: n = 50, p50 -> index 25 (the 26th smallest value), p95 -> index 47.
 * The textbook "nearest-rank" rule uses index ceil(q * n) - 1; the two rules
 * differ only when q * n is a whole number (for example p50 with n = 50). */
static double pct_ms(const uint64_t *sorted, size_t n, double q)
{
    size_t idx = (size_t)(q * (double)n);
    if (idx >= n)
        idx = n - 1;
    return (double)sorted[idx] / 1e6;
}

/* Close a file and report whether everything reached it. fclose() is the call
 * that notices a failed final flush (for example a full disk). */
static int close_checked(FILE *f, const char *path)
{
    int bad = ferror(f) != 0;

    if (fclose(f) != 0)
        bad = 1;
    if (bad)
        fprintf(stderr, "error: failed while writing %s\n", path);
    return bad ? -1 : 0;
}

static int open_output(FILE **f, const char *suffix, char *path, size_t path_len)
{
    int len = snprintf(path, path_len, "%s.%s.csv", cfg_prefix, suffix);

    if (len < 0 || (size_t)len >= path_len) {
        fprintf(stderr, "error: output prefix is too long\n");
        return -1;
    }
    *f = fopen(path, "w");
    if (!*f) {
        perror(path);
        return -1;
    }
    return 0;
}

/* Returns 0 only if both files were written completely. */
static int write_files(const Handoff *all, size_t n)
{
    char path[512];
    FILE *f;

    if (open_output(&f, "threads", path, sizeof(path)) != 0)
        return -1;
    fprintf(f, "tid,role,group,index\n");
    for (int i = 0; i < ninfos; i++)
        fprintf(f, "%d,%s,%d,%d\n", infos[i].tid, infos[i].role, infos[i].group, infos[i].index);
    if (close_checked(f, path) != 0)
        return -1;

    if (open_output(&f, "handoffs", path, sizeof(path)) != 0)
        return -1;
    fprintf(f, "group,item_id,from_tid,to_tid,stage,put_ns,get_ns\n");
    for (size_t i = 0; i < n; i++)
        fprintf(f, "%d,%lld,%d,%d,%s,%llu,%llu\n", all[i].group, (long long)all[i].item,
                all[i].from_tid, all[i].to_tid,
                all[i].stage == STAGE_P2W ? "producer_to_worker" : "worker_to_sink",
                (unsigned long long)all[i].put_ns, (unsigned long long)all[i].get_ns);
    return close_checked(f, path);
}

static void usage(const char *prog)
{
    fprintf(stderr,
            "usage: %s [options]\n"
            "  -w N   worker threads per group            (default %d)\n"
            "  -g N   independent pipelines (groups)      (default %d)\n"
            "  -n N   items per group                     (default %ld)\n"
            "  -p US  microseconds between arrivals, 0 = no pause (default %ld)\n"
            "  -c US  work per item in a worker, microseconds     (default %ld)\n"
            "  -m MODE  sleep (no CPU used) or spin (burns CPU)   (default sleep)\n"
            "  -q N   queue capacity                      (default %d)\n"
            "  -d S   seconds to wait before starting, so the collector can attach (default 0)\n"
            "  -o PREFIX  ground-truth file prefix        (default %s)\n"
            "Writes PREFIX.handoffs.csv and PREFIX.threads.csv, prints a summary.\n",
            prog, cfg_workers, cfg_groups, cfg_items, cfg_period_us, cfg_cost_us, cfg_qsize,
            cfg_prefix);
}

static long parse_long(const char *s, long min, const char *what)
{
    char *end;
    long v;

    errno = 0;
    v = strtol(s, &end, 10);
    if (errno || *end || end == s || v < min) {
        fprintf(stderr, "invalid %s: %s (must be an integer >= %ld)\n", what, s, min);
        exit(1);
    }
    return v;
}

int main(int argc, char **argv)
{
    int opt;

    while ((opt = getopt(argc, argv, "w:g:n:p:c:m:q:d:o:h")) != -1) {
        switch (opt) {
        case 'w': cfg_workers = (int)parse_long(optarg, 1, "-w"); break;
        case 'g': cfg_groups = (int)parse_long(optarg, 1, "-g"); break;
        case 'n': cfg_items = parse_long(optarg, 1, "-n"); break;
        case 'p': cfg_period_us = parse_long(optarg, 0, "-p"); break;
        case 'c': cfg_cost_us = parse_long(optarg, 0, "-c"); break;
        case 'q': cfg_qsize = (int)parse_long(optarg, 1, "-q"); break;
        case 'd': cfg_delay_s = (int)parse_long(optarg, 0, "-d"); break;
        case 'o': cfg_prefix = optarg; break;
        case 'm':
            if (!strcmp(optarg, "spin")) cfg_spin = 1;
            else if (!strcmp(optarg, "sleep")) cfg_spin = 0;
            else { fprintf(stderr, "-m must be sleep or spin\n"); return 1; }
            break;
        default: usage(argv[0]); return opt == 'h' ? 0 : 1;
        }
    }
    if (cfg_groups * (cfg_workers + 2) + 1 > MAX_THREADS) {
        fprintf(stderr, "too many threads (limit %d)\n", MAX_THREADS);
        return 1;
    }

    /* The collector needs this number: make run ARGS=<pid> */
    printf("benchmark pid=%d\n", (int)getpid());
    fflush(stdout);
    register_thread("main", -1, 0);
    if (cfg_delay_s > 0) {
        printf("waiting %d s before starting...\n", cfg_delay_s);
        fflush(stdout);
        sleep((unsigned)cfg_delay_s);
    }

    Group *groups = calloc((size_t)cfg_groups, sizeof(Group));
    int nthreads = cfg_groups * (cfg_workers + 2);
    pthread_t *tids = calloc((size_t)nthreads, sizeof(pthread_t));
    ThreadArg *args = calloc((size_t)nthreads, sizeof(ThreadArg));
    if (!groups || !tids || !args) {
        perror("calloc");
        return 1;
    }

    int t = 0;
    size_t total_items = (size_t)cfg_groups * (size_t)cfg_items;
    ThreadArg **sinks = calloc((size_t)cfg_groups, sizeof(ThreadArg *));
    ThreadArg **workers = calloc((size_t)cfg_groups * (size_t)cfg_workers, sizeof(ThreadArg *));
    int nworkers = 0;
    if (!sinks || !workers) {
        perror("calloc");
        return 1;
    }
    for (int g = 0; g < cfg_groups; g++) {
        queue_init(&groups[g].q1, cfg_qsize);
        queue_init(&groups[g].q2, cfg_qsize);
    }

    /* Build all arguments first, then start the clock and all threads. */
    for (int g = 0; g < cfg_groups; g++) {
        args[t] = (ThreadArg){ .g = &groups[g], .group = g };            /* producer */
        t++;
        for (int w = 0; w < cfg_workers; w++, t++) {                        /* workers */
            args[t] = (ThreadArg){ .g = &groups[g], .group = g, .index = w };
            log_init(&args[t].log, (size_t)cfg_items + 1);
            workers[nworkers++] = &args[t];
        }
        args[t] = (ThreadArg){ .g = &groups[g], .group = g };               /* sink */
        log_init(&args[t].log, (size_t)cfg_items);
        args[t].lat = calloc((size_t)cfg_items, sizeof(uint64_t));
        if (!args[t].lat) {
            perror("calloc");
            return 1;
        }
        sinks[g] = &args[t];
        t++;
    }

    uint64_t start = now_ns();
    t = 0;
    for (int g = 0; g < cfg_groups; g++) {
        spawn(&tids[t], producer, &args[t], "producer");
        t++;
        for (int w = 0; w < cfg_workers; w++, t++)
            spawn(&tids[t], worker, &args[t], "worker");
        spawn(&tids[t], sink, &args[t], "sink");
        t++;
    }
    for (int i = 0; i < nthreads; i++)
        pthread_join(tids[i], NULL);
    uint64_t end = now_ns();

    /* Merge logs and latencies. */
    size_t nall = 0, nlat = 0;
    Handoff *all = calloc(total_items * 2 + 1, sizeof(Handoff));
    uint64_t *lat = calloc(total_items, sizeof(uint64_t));
    if (!all || !lat) {
        perror("calloc");
        return 1;
    }
    for (int i = 0; i < nworkers; i++) {
        memcpy(all + nall, workers[i]->log.v, workers[i]->log.n * sizeof(Handoff));
        nall += workers[i]->log.n;
    }
    for (int g = 0; g < cfg_groups; g++) {
        memcpy(all + nall, sinks[g]->log.v, sinks[g]->log.n * sizeof(Handoff));
        nall += sinks[g]->log.n;
        memcpy(lat + nlat, sinks[g]->lat, sinks[g]->nlat * sizeof(uint64_t));
        nlat += sinks[g]->nlat;
    }
    qsort(all, nall, sizeof(Handoff), cmp_handoff);
    qsort(lat, nlat, sizeof(uint64_t), cmp_u64);

    /* Write the ground truth only after the timed part, so logging does not
     * disturb the measurement. */
    int write_status = write_files(all, nall);

    double secs = (double)(end - start) / 1e9;
    printf("groups=%d workers=%d items=%ld mode=%s period_us=%ld cost_us=%ld queue=%d\n",
           cfg_groups, cfg_workers, cfg_items, cfg_spin ? "spin" : "sleep", cfg_period_us,
           cfg_cost_us, cfg_qsize);
    printf("elapsed_s=%.3f throughput=%.1f items/s\n", secs, (double)nlat / secs);
    if (nlat)
        printf("latency_ms p50=%.2f p95=%.2f p99=%.2f max=%.2f\n", pct_ms(lat, nlat, 0.50),
               pct_ms(lat, nlat, 0.95), pct_ms(lat, nlat, 0.99), pct_ms(lat, nlat, 1.0));
    if (write_status != 0) {
        fprintf(stderr, "error: ground-truth files were not written correctly; this run is not valid\n");
        return 1;
    }
    printf("handoffs=%zu files: %s.handoffs.csv %s.threads.csv\n", nall, cfg_prefix, cfg_prefix);
    return 0;
}
