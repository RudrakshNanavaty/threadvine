"""Userspace analyzer: relationship graph inference and scoring."""

from analyzer.cli import main as _cli_main


def main() -> None:
    raise SystemExit(_cli_main())
