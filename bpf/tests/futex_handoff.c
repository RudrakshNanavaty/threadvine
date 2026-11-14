// Two threads hand work over using raw futex calls, so the collector's futex
// events can be checked against known addresses and return values.
//
// Each round the waker thread signals the waiter thread on `work_word` and then
// waits for the waiter's answer on `acknowledge_word`:
//   waker:  FUTEX_WAKE on work_word        (returns 1: the waiter was blocked)
//           FUTEX_WAIT on acknowledge_word (returns 0, or -EAGAIN if already
//           answered)
//   waiter: FUTEX_WAIT on work_word        (returns 0 once woken)
//           FUTEX_WAKE on acknowledge_word (returns 1 if the waker was blocked,
//           else 0)
// Expected: events on exactly two addresses, printed when the run ends, with
// the waker's thread identifier on the work_word wakes and the waiter's on its
// waits.
//
// The main thread's pthread_join also waits (FUTEX_WAIT_BITSET) on a third
// address until the threads exit; that wait has no matching wake call because
// the kernel wakes it when the thread ends.
//
// Build: cc -g -O2 -Wall -pthread bpf/tests/futex_handoff.c -o
// build/futex_handoff Usage: futex_handoff [rounds]    (prints its pid, then
// waits for Enter so the collector can be attached first)
#define _GNU_SOURCE
#include <linux/futex.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static _Atomic uint32_t work_word;        /* 1 = work is ready for the waiter */
static _Atomic uint32_t acknowledge_word; /* 1 = the waiter finished the work */
static long rounds = 1000;
static pid_t waker_thread_identifier;
static pid_t waiter_thread_identifier;

static long futex_call(_Atomic uint32_t *address, int operation,
                       uint32_t value) {
  return syscall(SYS_futex, address, operation | FUTEX_PRIVATE_FLAG, value,
                 NULL, NULL, 0);
}

/* Block until *address is no longer 0 (the expected value passed to the
 * kernel). */
static void wait_until_set(_Atomic uint32_t *address) {
  while (atomic_load(address) == 0)
    futex_call(address, FUTEX_WAIT, 0);
}

static void set_and_wake(_Atomic uint32_t *address) {
  atomic_store(address, 1);
  futex_call(address, FUTEX_WAKE, 1);
}

static void *waiter(void *argument) {
  (void)argument;
  waiter_thread_identifier = (pid_t)syscall(SYS_gettid);
  pthread_setname_np(pthread_self(), "waiter");
  for (long i = 0; i < rounds; i++) {
    wait_until_set(&work_word);
    atomic_store(&work_word, 0);
    set_and_wake(&acknowledge_word);
  }
  return NULL;
}

static void *waker(void *argument) {
  struct timespec pause = {.tv_sec = 0, .tv_nsec = 200 * 1000}; /* 200 us */

  (void)argument;
  waker_thread_identifier = (pid_t)syscall(SYS_gettid);
  pthread_setname_np(pthread_self(), "waker");
  for (long i = 0; i < rounds; i++) {
    nanosleep(&pause, NULL); /* lets the waiter block, so the wake finds it */
    set_and_wake(&work_word);
    wait_until_set(&acknowledge_word);
    atomic_store(&acknowledge_word, 0);
  }
  return NULL;
}

int main(int argc, char **argv) {
  pthread_t waker_thread, waiter_thread;

  if (argc > 1)
    rounds = atol(argv[1]);
  printf("pid %d: press Enter to start %ld rounds\n", getpid(), rounds);
  fflush(stdout);
  getchar();
  pthread_create(&waiter_thread, NULL, waiter, NULL);
  pthread_create(&waker_thread, NULL, waker, NULL);
  pthread_join(waker_thread, NULL);
  pthread_join(waiter_thread, NULL);
  printf("work_word=0x%lx acknowledge_word=0x%lx waker_thread=%d "
         "waiter_thread=%d\n",
         (unsigned long)&work_word, (unsigned long)&acknowledge_word,
         waker_thread_identifier, waiter_thread_identifier);
  printf("done\n");
  return 0;
}
