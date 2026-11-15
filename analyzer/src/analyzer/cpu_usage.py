"""How much CPU time each traced thread actually used.

The wakeup graph says which threads cooperate, not how busy they are. A thread
that burns a whole CPU (a "hog") and a thread that sleeps most of the time look
the same to the graph, but they need very different placement. This module
measures the difference from the ``sched_switch`` events already in the trace.

A thread runs on a CPU from the switch that makes it the next thread until the
next switch on that same CPU that makes it the previous thread.
"""

from __future__ import annotations

from analyzer.trace import Trace


def thread_cpu_shares(trace: Trace) -> dict[int, float]:
    """Return each target thread's CPU time as a fraction of the trace duration.

    1.0 means the thread used one whole CPU for the whole trace. Threads that
    never appear in a scheduler switch are absent (their share is zero).
    """
    switch_events = [
        event
        for event in trace.events
        if event["event_type"] == "EVENT_TYPE_SCHEDULER_SWITCH"
    ]
    if len(switch_events) < 2:
        return {}

    trace_start_ns = switch_events[0]["timestamp_nanoseconds"]
    trace_end_ns = switch_events[-1]["timestamp_nanoseconds"]
    duration_ns = trace_end_ns - trace_start_ns
    if duration_ns <= 0:
        return {}

    run_time_ns: dict[int, int] = {}
    # cpu_index -> (thread now running there, when it started running).
    running_on_cpu: dict[int, tuple[int, int]] = {}

    def add_run_time(thread_id: int, start_ns: int, end_ns: int) -> None:
        if thread_id in trace.target_thread_ids and end_ns > start_ns:
            run_time_ns[thread_id] = run_time_ns.get(thread_id, 0) + end_ns - start_ns

    for event in switch_events:
        cpu_index = event["cpu_index"]
        timestamp_ns = event["timestamp_nanoseconds"]
        switch = event["scheduler_switch"]
        previous_thread_id = switch["previous_thread_id"]
        next_thread_id = switch["next_thread_id"]

        known = running_on_cpu.get(cpu_index)
        if known is None:
            # First switch seen on this CPU: the outgoing thread was already running
            # when the trace began.
            add_run_time(previous_thread_id, trace_start_ns, timestamp_ns)
        elif known[0] == previous_thread_id:
            add_run_time(previous_thread_id, known[1], timestamp_ns)
        # Otherwise the switch that put this thread on the CPU was not recorded
        # (switches between two threads outside the target process are dropped), so
        # we cannot tell how long it ran: count nothing rather than guess.
        running_on_cpu[cpu_index] = (next_thread_id, timestamp_ns)

    # Threads still running when the trace ends.
    for thread_id, started_ns in running_on_cpu.values():
        add_run_time(thread_id, started_ns, trace_end_ns)

    return {
        thread_id: run_time / duration_ns for thread_id, run_time in run_time_ns.items()
    }
