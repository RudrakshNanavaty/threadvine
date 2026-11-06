// Two threads take turns through a mutex + condition variable.
// Expected wakeups: ping -> pong and pong -> ping, about once per round each.
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static int turn;                /* 0 = ping's turn, 1 = pong's turn */
static long rounds = 100000;

static void *player(void *arg)
{
    int me = (int)(long)arg;
    pthread_setname_np(pthread_self(), me ? "pong" : "ping");
    for (long i = 0; i < rounds; i++) {
        pthread_mutex_lock(&mu);
        while (turn != me)
            pthread_cond_wait(&cv, &mu);
        turn = !me;
        pthread_cond_signal(&cv);
        pthread_mutex_unlock(&mu);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc > 1)
        rounds = atol(argv[1]);
    printf("pid %d: press Enter to start %ld rounds\n", getpid(), rounds);
    getchar();
    pthread_t a, b;
    pthread_create(&a, NULL, player, (void *)0L);
    pthread_create(&b, NULL, player, (void *)1L);
    pthread_join(a, NULL);
    pthread_join(b, NULL);
    printf("done\n");
    return 0;
}
