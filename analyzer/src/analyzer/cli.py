"""Command-line interface: infer and score subcommands."""
from __future__ import annotations

import argparse
import sys

from analyzer.graph import (
    DEFAULT_MINIMUM_COUNT,
    DEFAULT_MINIMUM_FRACTION,
    InferenceSettings,
    load_graph_json,
    write_graph_dot,
    write_graph_json,
)
from analyzer.scoring import format_score_report, score_graph
from analyzer.trace import load_trace
from analyzer import timing_inference, wakeup_inference


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='analyzer',
        description='Infer and score thread relationship graphs from collector traces.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    infer = subparsers.add_parser('infer', help='Infer a graph from a collector trace')
    infer.add_argument('trace', help='Binary .events.bin or .jsonl trace file')
    infer.add_argument(
        '--method',
        choices=('wakeup', 'timing'),
        required=True,
        help='Inference method',
    )
    infer.add_argument('-o', '--output', default='graph.json', help='Output graph JSON path')
    infer.add_argument('--dot', default=None, help='Optional Graphviz DOT output path')
    infer.add_argument(
        '--minimum-count',
        type=int,
        default=DEFAULT_MINIMUM_COUNT,
        help=f'Minimum evidence count for a confident edge (default {DEFAULT_MINIMUM_COUNT})',
    )
    infer.add_argument(
        '--minimum-fraction',
        type=float,
        default=DEFAULT_MINIMUM_FRACTION,
        help=(
            'Minimum share of the destination thread\'s incoming evidence '
            f'(default {DEFAULT_MINIMUM_FRACTION})'
        ),
    )
    infer.add_argument(
        '--timing-window-us',
        type=int,
        default=100,
        help='Timing method: block-to-switch-in window in microseconds (default 100)',
    )

    score = subparsers.add_parser('score', help='Score a graph against ground truth')
    score.add_argument('graph', help='graph.json produced by infer')
    score.add_argument(
        '--groundtruth',
        required=True,
        help='Prefix of the benchmark ground-truth CSV files',
    )
    return parser


def _run_infer(arguments: argparse.Namespace) -> int:
    settings = InferenceSettings(
        method=arguments.method,
        minimum_count=arguments.minimum_count,
        minimum_fraction=arguments.minimum_fraction,
        timing_window_us=arguments.timing_window_us,
    )
    trace = load_trace(arguments.trace)
    if arguments.method == 'wakeup':
        graph = wakeup_inference.infer(trace, settings)
    else:
        graph = timing_inference.infer(trace, settings)

    for warning in graph.loss_warnings():
        print(f'WARNING: {warning}', file=sys.stderr)
    if trace.outside_process_event_count:
        print(
            f'note: dropped {trace.outside_process_event_count} events involving '
            'threads outside the target process',
            file=sys.stderr,
        )

    write_graph_json(graph, arguments.output)
    print(
        f'wrote {arguments.output}: {len(graph.edges)} edges '
        f'({sum(1 for edge in graph.edges if edge.confidence == "confident")} confident, '
        f'{sum(1 for edge in graph.edges if edge.confidence == "ambiguous")} ambiguous)'
    )
    if arguments.dot:
        write_graph_dot(graph, arguments.dot)
        print(f'wrote {arguments.dot}')
    return 0


def _run_score(arguments: argparse.Namespace) -> int:
    graph = load_graph_json(arguments.graph)
    report = score_graph(graph, arguments.groundtruth)
    print(format_score_report(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == 'infer':
        return _run_infer(arguments)
    if arguments.command == 'score':
        return _run_score(arguments)
    parser.error(f'unknown command {arguments.command}')
    return 2
