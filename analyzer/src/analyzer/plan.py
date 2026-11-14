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

Output contract (consumed by ``experiments/apply_plan.py``)::

    {"cpu_count": 4,
     "assignments": {"<thread_id>": [0, 1], ...},
     "unpinned": [<thread_id>, ...]}
"""

from __future__ import annotations

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
    blocks = [1] * len(component_sizes)
    for _ in range(cpu_count - len(component_sizes)):
        neediest = max(
            range(len(component_sizes)),
            key=lambda index: (component_sizes[index] / blocks[index], -index),
        )
        blocks[neediest] += 1
    return blocks


def build_plan(graph: Graph, cpu_count: int) -> dict:
    """Build the placement plan dictionary for ``cpu_count`` CPUs."""
    if graph.metadata.method != "wakeup":
        raise ValueError(
            f"plan needs a wakeup graph, got method {graph.metadata.method!r}"
        )
    if cpu_count < 1:
        raise ValueError("cpu_count must be at least 1")

    # Largest components first so they keep their CPUs if there are too many groups.
    components = sorted(
        _confident_components(graph), key=lambda members: (-len(members), members[0])
    )
    placed_components = components[:cpu_count]
    skipped_components = components[cpu_count:]

    blocks = _block_sizes([len(members) for members in placed_components], cpu_count)

    assignments: dict[str, list[int]] = {}
    next_cpu = 0
    for members, block_size in zip(placed_components, blocks):
        block = list(range(next_cpu, next_cpu + block_size))
        next_cpu += block_size
        for thread_id in members:
            assignments[str(thread_id)] = block

    in_a_placed_component = {
        thread_id for members in placed_components for thread_id in members
    }
    known_threads = set(graph.threads)
    for edge in graph.edges:
        known_threads.add(edge.source_thread_id)
        known_threads.add(edge.destination_thread_id)
    unpinned = sorted(known_threads - in_a_placed_component)
    return {
        "cpu_count": cpu_count,
        "assignments": assignments,
        "unpinned": unpinned,
    }
