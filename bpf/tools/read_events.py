#!/usr/bin/env python3
"""Shim: the decoder lives in the analyzer package.

Summary of a trace:  python3 bpf/tools/read_events.py run.events.bin
"""
import pathlib
import sys

# analyzer/src so "import analyzer" works without an install.
_analyzer_src = pathlib.Path(__file__).resolve().parents[2] / 'analyzer' / 'src'
sys.path.insert(0, str(_analyzer_src))

from analyzer.trace_reader import main  # noqa: E402

if __name__ == '__main__':
    main()
