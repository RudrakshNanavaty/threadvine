"""Turn an inferred wakeup graph into a thread-to-CPU placement plan.

Rule (kept deliberately simple so the experiment tests the graph, not the
planner):

1. Take only **confident** edges of a **wakeup** graph; ambiguous edges
   (including join/teardown wakeups into ``main``) are ignored.
2. Connected components of those edges (direction ignored) are the
   cooperating thread groups.
3. Each component gets its own disjoint, contiguous block of CPUs, sized in
   proportion to its thread count (at least one CPU each).
4. Threads that are in no component stay unpinned.

Optional CPU-aware mode (``cpu_share_by_thread`` given, from
``analyzer.cpu_usage.thread_cpu_shares``): threads that use at least
``hot_share`` of a CPU ("hogs") are isolated on their own block of CPUs, sized by
how much CPU they actually use, and the cooperating groups share the rest. This
keeps a CPU-hungry background thread away from latency-sensitive threads. If
there is no hog, or the hogs need every CPU, the result is the plain plan above.

Output contract (consumed by ``experiments/apply_plan.py``)::

    {"cpu_count": 4,
     "assignments": {"<thread_id>": [0, 1], ...},
     "unpinned": [<thread_id>, ...]}
"""

from __future__ import annotations

import math

from analyzer.graph import Graph


def _confident_components(graph: Graph) -> list[list[int]]:
    """Connected components of confident edges; each is a sorted thread list."""
    neighbours: dict[int, set[int]] = {}
    for edge in graph.edges:
        if edge.confidence != "confident":
            continue
        source = edge.source_thread_id
        destination = edge.destination_thread_id
        neighbours.setdefault(source, set()).add(destination)
        neighbours.setdefault(destination, set()).add(source)

    components: list[list[int]] = []
    visited: set[int] = set()
    for start in sorted(neighbours):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        members: list[int] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbour in neighbours[current]:
                if neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(neighbour)
        components.append(sorted(members))
    return components


def _block_sizes(component_sizes: list[int], cpu_count: int) -> list[int]:
    """CPUs per component: one each, then extras to the largest size-per-CPU ratio."""
    if not component_sizes:
        return []
    blocks = [1] * len(component_sizes)
    for _ in range(cpu_count - len(component_sizes)):
        neediest = max(
            range(len(component_sizes)),
            key=lambda index: (component_sizes[index] / blocks[index], -index),
        )
        blocks[neediest] += 1
    return blocks


def _plan_components_on_cpus(
    graph: Graph, cpus: list[int], excluded_thread_ids: set[int]
) -> dict[str, list[int]]:
    """Give each confident component its own disjoint contiguous block of ``cpus``.

    Threads in ``excluded_thread_ids`` are dropped from their components first.
    Components beyond the number of CPUs get no assignment (their threads stay unpinned).
    """
    components = [
        [thread_id for thread_id in members if thread_id not in excluded_thread_ids]
        for members in _confident_components(graph)
    ]
    # Largest components first so they keep their CPUs if there are too many groups.
    components = sorted(
        (members for members in components if members),
        key=lambda members: (-len(members), members[0]),
    )
    placed_components = components[: len(cpus)]
    blocks = _block_sizes([len(members) for members in placed_components], len(cpus))

    assignments: dict[str, list[int]] = {}
    next_index = 0
    for members, block_size in zip(placed_components, blocks):
        block = cpus[next_index : next_index + block_size]
        next_index += block_size
        for thread_id in members:
            assignments[str(thread_id)] = block
    return assignments


def build_plan(
    graph: Graph,
    cpu_count: int,
    cpu_share_by_thread: dict[int, float] | None = None,
    hot_share: float = 0.5,
) -> dict:
    """Build the placement plan dictionary for ``cpu_count`` CPUs."""
    if graph.metadata.method != "wakeup":
        raise ValueError(
            f"plan needs a wakeup graph, got method {graph.metadata.method!r}"
        )
    if cpu_count < 1:
        raise ValueError("cpu_count must be at least 1")

    all_cpus = list(range(cpu_count))
    assignments = _plan_components_on_cpus(graph, all_cpus, set())

    if cpu_share_by_thread:
        hot_thread_ids = {
            thread_id
            for thread_id, share in cpu_share_by_thread.items()
            if share >= hot_share
        }
        hot_demand = math.ceil(
            round(sum(cpu_share_by_thread[thread_id] for thread_id in hot_thread_ids), 6)
        )
        cool_demand = math.ceil(
            round(
                sum(
                    share
                    for thread_id, share in cpu_share_by_thread.items()
                    if thread_id not in hot_thread_ids
                ),
                6,
            )
        )
        # Always leave the cooperating threads at least one CPU.
        reserved_cpu_count = min(hot_demand, cpu_count - max(1, cool_demand))
        # No headroom (the hot threads alone need every CPU): keep the plain plan.
        if hot_thread_ids and hot_demand < cpu_count and reserved_cpu_count >= 1:
            hot_block = all_cpus[:reserved_cpu_count]
            # After isolating the hogs, the cooperating threads all share the remaining
            # CPUs. Splitting them further by group would squeeze each group (often
            # mostly asleep) onto one CPU, which only adds waiting between its threads.
            remaining_cpus = all_cpus[reserved_cpu_count:]
            assignments = {
                str(thread_id): list(remaining_cpus)
                for members in _confident_components(graph)
                for thread_id in members
                if thread_id not in hot_thread_ids
            }
            for thread_id in hot_thread_ids:
                assignments[str(thread_id)] = hot_block

    known_threads = set(graph.threads) | set(cpu_share_by_thread or {})
    for edge in graph.edges:
        known_threads.add(edge.source_thread_id)
        known_threads.add(edge.destination_thread_id)
    unpinned = sorted(known_threads - {int(thread_id) for thread_id in assignments})
    return {
        "cpu_count": cpu_count,
        "assignments": assignments,
        "unpinned": unpinned,
    }
