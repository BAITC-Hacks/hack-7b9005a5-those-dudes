"""Stable server entrypoint used by the project launcher.

Example:
    python -m agent_app.server --host 127.0.0.1 --port 8765 \
        --static dashboard/dist --data input_data/data --output output
"""

from __future__ import annotations

import argparse

from .main import _configure_console, serve


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve dashboard and local assistant API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--static", dest="static_dir", default="dashboard/dist")
    parser.add_argument("--root", default=None)
    parser.add_argument("--data", default="input_data/data")
    parser.add_argument("--output", default="output")
    return parser


def main() -> int:
    _configure_console()
    serve(make_parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
