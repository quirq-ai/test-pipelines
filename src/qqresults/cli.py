"""The `qqresults` command line."""
from __future__ import annotations

import argparse
import sys

from qqresults import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qqresults", description=__doc__)
    p.add_argument("--version", action="version", version=f"qqresults {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
