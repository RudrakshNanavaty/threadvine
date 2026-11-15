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
// Profile phase (-W SECONDS): the same threads first run the normal workload for
// SECONDS (the "profile" phase, which is what the collector traces and the analyzer
// builds the thread graph from). They then pause and the program waits for SIGUSR1.
// While it waits you can pin the threads to CPUs. On SIGUSR1 a fresh timed interval
// starts on the SAME process (same thread ids), and only that interval counts for
// throughput and latency. Profile-phase handoffs are still written to the answer key
// (column `phase` = profile); without -W every row has phase = timed.
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
#include <signal.h>
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
static long cfg_profile_s = 0;   /* -W: seconds of profile phase before the timed interval (0 = off) */
static int cfg_hogs = 0;         /* -H: extra CPU-burning threads outside any pipeline (default 0) */
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
#define KIND_DATA 0      /* a normal item                                          */
#define KIND_PHASE_END 1 /* end of the profile phase (one per worker, then forwarded) */
#define KIND_FINAL 2     /* end of the run: a worker that takes it exits           */
#define PH_PROFILE 0
#define PH_TIMED 1

typedef struct {
    int64_t id;        /* item number inside its phase (0, 1, 2, ...)            */
    int32_t sender_tid;/* thread that put it in the queue                        */
    int32_t kind;      /* KIND_DATA, KIND_PHASE_END or KIND_FINAL                */
    int32_t phase;     /* PH_PROFILE or PH_TIMED                                 */
    uint64_t t0_ns;    /* intended arrival time (for latency)                    */
    uint64_t put_ns;   /* when the sender put it in the queue                    */
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
    int group, stage, from_tid, to_tid, phase;
    int64_t item;
    uint64_t put_ns, get_ns;
} Handoff;

typedef struct {
    Handoff *v;
    size_t n, cap;
    int growable; /* 1 only during the profile phase: the buffer may be reallocated */
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
    l->growable = 0;
}

/* Make room for `extra` more handoffs. Only called while no other thread can touch
 * this log (the threads that own it are idle), so the timed interval never has to
 * grow a buffer. */
static void log_reserve(Log *l, size_t extra)
{
    if (l->n + extra <= l->cap)
        return;
    size_t cap = l->n + extra;
    Handoff *v = realloc(l->v, cap * sizeof(Handoff));
    if (!v) {
        perror("realloc");
        exit(1);
    }
    l->v = v;
    l->cap = cap;
}

static void log_add(Log *l, int group, int stage, Item it, int to_tid, uint64_t get_ns)
{
    if (l->n >= l->cap) {
        if (!l->growable) { /* cannot happen: buffers are sized for the worst case */
            fprintf(stderr, "internal error: ground-truth buffer overflow\n");
            abort();
        }
        log_reserve(l, l->cap ? l->cap : 1024); /* profile phase only: double it */
    }
    l->v[l->n++] = (Handoff){ group, stage, it.sender_tid, to_tid, it.phase, it.id, it.put_ns, get_ns };
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
    long profile_items; /* items the producer sent in the profile phase (set before its end markers) */
} Group;

/* Phase control (used only with -W): sinks report when the profile phase has drained,
 * main waits for SIGUSR1, then releases the producers for the timed interval. */
static pthread_mutex_t phase_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t phase_cond = PTHREAD_COND_INITIALIZER;
static int sinks_profile_done = 0;
static int timed_go = 0;

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

/* Background CPU hog (-H): burns a CPU until told to stop and counts how much it did, so
 * an experiment can check that moving it does not just starve it. It is deliberately NOT
 * registered in the thread table: it is not part of any pipeline, so the answer key and
 * check_groundtruth.py are unchanged. */
static int hogs_stop = 0;
typedef struct {
    uint64_t blocks; /* finished blocks of busy work */
    char pad[56];    /* one counter per cache line, so hogs do not slow each other down */
} HogCounter;
static HogCounter *hog_counters;

static uint64_t hog_total(void)
{
    uint64_t total = 0;
    for (int h = 0; h < cfg_hogs; h++)
        total += __atomic_load_n(&hog_counters[h].blocks, __ATOMIC_RELAXED);
    return total;
}

static void *hog(void *p)
{
    HogCounter *counter = p;

    pthread_setname_np(pthread_self(), "hog");
    while (!__atomic_load_n(&hogs_stop, __ATOMIC_RELAXED)) {
        for (volatile int k = 0; k < 20000; k++)
            ;
        __atomic_fetch_add(&counter->blocks, 1, __ATOMIC_RELAXED);
    }
    return NULL;
}

static void *producer(void *p)
{
    ThreadArg *a = p;
    int tid = register_thread("producer", a->group, 0);
    uint64_t base;

    if (cfg_profile_s > 0) {
        /* Profile phase: same workload, for cfg_profile_s seconds. */
        base = now_ns();
        uint64_t deadline = base + (uint64_t)cfg_profile_s * 1000000000ull;
        long i;
        for (i = 0;; i++) {
            uint64_t due = base + (uint64_t)i * (uint64_t)cfg_period_us * 1000ull;
            if (cfg_period_us > 0) {
                if (due >= deadline)
                    break;
                sleep_until(due);
            } else {
                due = now_ns();
                if (due >= deadline)
                    break;
            }
            queue_put(&a->g->q1, (Item){ i, tid, KIND_DATA, PH_PROFILE, due, 0 });
        }
        __atomic_store_n(&a->g->profile_items, i, __ATOMIC_SEQ_CST);
        for (int w = 0; w < cfg_workers; w++)
            queue_put(&a->g->q1, (Item){ -1, tid, KIND_PHASE_END, PH_PROFILE, 0, 0 });
        /* Wait here until main has seen SIGUSR1. */
        pthread_mutex_lock(&phase_lock);
        while (!timed_go)
            pthread_cond_wait(&phase_cond, &phase_lock);
        pthread_mutex_unlock(&phase_lock);
    }

    base = now_ns();
    for (long i = 0; i < cfg_items; i++) {
        uint64_t due = base + (uint64_t)i * (uint64_t)cfg_period_us * 1000ull;
        if (cfg_period_us > 0)
            sleep_until(due);
        else
            due = now_ns();
        /* Open loop: latency is measured from when the item was DUE, so a
         * slow pipeline cannot hide its delay by slowing the producer. */
        queue_put(&a->g->q1, (Item){ i, tid, KIND_DATA, PH_TIMED, due, 0 });
    }
    for (int w = 0; w < cfg_workers; w++)       /* one stop marker per worker */
        queue_put(&a->g->q1, (Item){ -1, tid, KIND_FINAL, PH_TIMED, 0, 0 });
    return NULL;
}

static void *worker(void *p)
{
    ThreadArg *a = p;
    int tid = register_thread("worker", a->group, a->index);

    for (;;) {
        Item it = queue_get(&a->g->q1);
        uint64_t got = now_ns();
        if (it.kind == KIND_FINAL)
            break;
        if (it.kind == KIND_PHASE_END) { /* profile phase over: pass the marker on, keep running */
            it.sender_tid = tid;
            queue_put(&a->g->q2, it);
            continue;
        }
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

    if (cfg_profile_s > 0) {
        /* Profile phase: done when every item the producer sent has arrived AND all
         * workers' end markers have been seen. Then tell main. */
        long received = 0;
        int markers = 0;
        for (;;) {
            Item it = queue_get(&a->g->q2);
            uint64_t got = now_ns();
            if (it.kind == KIND_PHASE_END)
                markers++;
            else {
                log_add(&a->log, a->group, STAGE_W2S, it, tid, got);
                received++;
            }
            if (markers == cfg_workers && received == __atomic_load_n(&a->g->profile_items, __ATOMIC_SEQ_CST))
                break;
        }
        pthread_mutex_lock(&phase_lock);
        sinks_profile_done++;
        pthread_cond_broadcast(&phase_cond);
        pthread_mutex_unlock(&phase_lock);
    }
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
    fprintf(f, "group,item_id,from_tid,to_tid,stage,put_ns,get_ns,phase\n");
    for (size_t i = 0; i < n; i++)
        fprintf(f, "%d,%lld,%d,%d,%s,%llu,%llu,%s\n", all[i].group, (long long)all[i].item,
                all[i].from_tid, all[i].to_tid,
                all[i].stage == STAGE_P2W ? "producer_to_worker" : "worker_to_sink",
                (unsigned long long)all[i].put_ns, (unsigned long long)all[i].get_ns,
                all[i].phase == PH_PROFILE ? "profile" : "timed");
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
            "  -W S   run the workload for S seconds as a profile phase, then wait for SIGUSR1\n"
            "         (kill -USR1 <pid>) and start a fresh timed interval; only that interval\n"
            "         counts for throughput and latency (default 0 = off)\n"
            "  -H N   extra CPU-hog threads outside the pipelines (default 0); prints\n"
            "         hog_iterations_per_s so you can check a placement does not starve them\n"
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

    while ((opt = getopt(argc, argv, "w:g:n:p:c:m:q:d:W:H:o:h")) != -1) {
        switch (opt) {
        case 'w': cfg_workers = (int)parse_long(optarg, 1, "-w"); break;
        case 'g': cfg_groups = (int)parse_long(optarg, 1, "-g"); break;
        case 'n': cfg_items = parse_long(optarg, 1, "-n"); break;
        case 'p': cfg_period_us = parse_long(optarg, 0, "-p"); break;
        case 'c': cfg_cost_us = parse_long(optarg, 0, "-c"); break;
        case 'q': cfg_qsize = (int)parse_long(optarg, 1, "-q"); break;
        case 'd': cfg_delay_s = (int)parse_long(optarg, 0, "-d"); break;
        case 'W': cfg_profile_s = parse_long(optarg, 0, "-W"); break;
        case 'H': cfg_hogs = (int)parse_long(optarg, 0, "-H"); break;
        case 'o': cfg_prefix = optarg; break;
        case 'm':
            if (!strcmp(optarg, "spin")) cfg_spin = 1;
            else if (!strcmp(optarg, "sleep")) cfg_spin = 0;
            else { fprintf(stderr, "-m must be sleep or spin\n"); return 1; }
            break;
        default: usage(argv[0]); return opt == 'h' ? 0 : 1;
        }
    }
    if (cfg_groups * (cfg_workers + 2) + 1 + cfg_hogs > MAX_THREADS) {
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

    sigset_t usr1;
    sigemptyset(&usr1);
    sigaddset(&usr1, SIGUSR1);
    if (cfg_profile_s > 0) /* block it in every thread; only main waits for it, later */
        pthread_sigmask(SIG_BLOCK, &usr1, NULL);

    pthread_t *hog_threads = NULL;
    if (cfg_hogs > 0) {
        hog_threads = calloc((size_t)cfg_hogs, sizeof(pthread_t));
        hog_counters = calloc((size_t)cfg_hogs, sizeof(HogCounter));
        if (!hog_threads || !hog_counters) {
            perror("calloc");
            return 1;
        }
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
            log_init(&args[t].log, cfg_profile_s > 0 ? 1024 : (size_t)cfg_items + 1);
            args[t].log.growable = cfg_profile_s > 0;
            workers[nworkers++] = &args[t];
        }
        args[t] = (ThreadArg){ .g = &groups[g], .group = g };               /* sink */
        log_init(&args[t].log, cfg_profile_s > 0 ? 1024 : (size_t)cfg_items);
        args[t].log.growable = cfg_profile_s > 0;
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
    for (int h = 0; h < cfg_hogs; h++) {
        int rc = pthread_create(&hog_threads[h], NULL, hog, &hog_counters[h]);
        if (rc != 0) {
            fprintf(stderr, "error: cannot create hog thread: %s\n", strerror(rc));
            exit(1);
        }
    }
    uint64_t hog_blocks_at_start = 0; /* hog work done before the timed interval starts */
    if (cfg_profile_s > 0) {
        /* Wait for the profile phase to drain, then for SIGUSR1. Every thread is now
         * idle (producers wait for timed_go, workers and sinks wait on empty queues),
         * so it is safe to size the answer-key buffers for the timed interval here. */
        pthread_mutex_lock(&phase_lock);
        while (sinks_profile_done < cfg_groups)
            pthread_cond_wait(&phase_cond, &phase_lock);
        pthread_mutex_unlock(&phase_lock);

        size_t profile_total = 0;
        for (int i = 0; i < nworkers; i++) {
            profile_total += workers[i]->log.n;
            log_reserve(&workers[i]->log, (size_t)cfg_items + 1);
            workers[i]->log.growable = 0;
        }
        for (int g = 0; g < cfg_groups; g++) {
            log_reserve(&sinks[g]->log, (size_t)cfg_items);
            sinks[g]->log.growable = 0;
        }
        printf("profile phase done: %zu producer->worker handoffs recorded\n", profile_total);
        printf("waiting for SIGUSR1 (kill -USR1 %d)...\n", (int)getpid());
        fflush(stdout);
        int sig;
        sigwait(&usr1, &sig); /* returns at once if SIGUSR1 already arrived */
        printf("timed interval starting\n");
        fflush(stdout);
        hog_blocks_at_start = hog_total();
        pthread_mutex_lock(&phase_lock);
        start = now_ns();
        timed_go = 1;
        pthread_cond_broadcast(&phase_cond);
        pthread_mutex_unlock(&phase_lock);
    }
    for (int i = 0; i < nthreads; i++)
        pthread_join(tids[i], NULL);
    uint64_t end = now_ns();
    uint64_t hog_blocks_in_interval = hog_total() - hog_blocks_at_start;
    __atomic_store_n(&hogs_stop, 1, __ATOMIC_RELAXED);
    for (int h = 0; h < cfg_hogs; h++)
        pthread_join(hog_threads[h], NULL);

    /* Merge logs and latencies. */
    size_t nall = 0, nlat = 0, all_cap = 1;
    for (int i = 0; i < nworkers; i++)
        all_cap += workers[i]->log.n;
    for (int g = 0; g < cfg_groups; g++)
        all_cap += sinks[g]->log.n;
    Handoff *all = calloc(all_cap, sizeof(Handoff));
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
    if (cfg_hogs > 0)
        printf("hogs=%d hog_iterations_per_s=%.1f\n", cfg_hogs, (double)hog_blocks_in_interval / secs);
    if (write_status != 0) {
        fprintf(stderr, "error: ground-truth files were not written correctly; this run is not valid\n");
        return 1;
    }
    printf("handoffs=%zu files: %s.handoffs.csv %s.threads.csv\n", nall, cfg_prefix, cfg_prefix);
    return 0;
}
