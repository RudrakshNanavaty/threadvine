"""Shim around ``analyzer.event_decoder``.

The binary / ``.jsonl`` decoder lives in the analyzer package so there is one
copy to keep in sync with ``bpf/event.h``. This script only exists so
``python3 bpf/tools/read_events.py TRACE`` keeps working from the repo root
without installing the package.

Summary of a trace::

    python3 bpf/tools/read_events.py run.events.bin
"""

import pathlib
import sys

# analyzer/src so "import analyzer" works without an install.
_analyzer_src = pathlib.Path(__file__).resolve().parents[2] / "analyzer" / "src"
sys.path.insert(0, str(_analyzer_src))

from analyzer.event_decoder import main

if __name__ == "__main__":
    main()
