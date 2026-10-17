/* Threads and the sync objects games lean on: a mutex-guarded counter
 * across many threads, a semaphore handoff, a manual-reset event fan-out
 * and a wait that must time out rather than hang. */
#include "probe.h"

#define THREADS 16
#define ROUNDS 20000

static HANDLE mutex, start_event, sem;
static volatile LONG counter;

static DWORD WINAPI worker(void *arg)
{
    (void)arg;
    WaitForSingleObject(start_event, INFINITE);
    for (int i = 0; i < ROUNDS; i++) {
        WaitForSingleObject(mutex, INFINITE);
        LONG v = counter;
        counter = v + 1;
        ReleaseMutex(mutex);
    }
    ReleaseSemaphore(sem, 1, NULL);
    return 0;
}

int main(void)
{
    mutex = CreateMutexA(NULL, FALSE, NULL);
    start_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    sem = CreateSemaphoreA(NULL, 0, THREADS, NULL);
    PROBE_REQUIRE(mutex && start_event && sem, "object creation failed (%lu)", GetLastError());

    HANDLE threads[THREADS];
    for (int i = 0; i < THREADS; i++) {
        threads[i] = CreateThread(NULL, 0, worker, NULL, 0, NULL);
        PROBE_REQUIRE(threads[i], "CreateThread %d failed (%lu)", i, GetLastError());
    }

    /* GetTickCount moves in ~16 ms steps (Wine refreshes it from the
     * wineserver), coarser than the slack allowed here. */
    LARGE_INTEGER freq, t0, t1;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    DWORD w = WaitForSingleObject(sem, 100);
    QueryPerformanceCounter(&t1);
    LONGLONG waited_ms = (t1.QuadPart - t0.QuadPart) * 1000 / freq.QuadPart;
    PROBE_REQUIRE(w == WAIT_TIMEOUT, "semaphore signaled before start (%lu)", w);
    PROBE_REQUIRE(waited_ms >= 90, "timed wait returned early (%lld ms of 100)", waited_ms);

    SetEvent(start_event);
    for (int i = 0; i < THREADS; i++) {
        w = WaitForSingleObject(sem, 30000);
        PROBE_REQUIRE(w == WAIT_OBJECT_0, "worker %d never finished (%lu)", i, w);
    }
    w = WaitForMultipleObjects(THREADS, threads, TRUE, 30000);
    PROBE_REQUIRE(w < WAIT_OBJECT_0 + THREADS, "threads did not exit (%lu)", w);
    PROBE_REQUIRE(counter == THREADS * ROUNDS, "counter %ld, want %d (lost updates)",
                  counter, THREADS * ROUNDS);

    probe_verdict(1, "%d threads x %d mutex rounds, semaphore and event handoff", THREADS, ROUNDS);
    return 0;
}
