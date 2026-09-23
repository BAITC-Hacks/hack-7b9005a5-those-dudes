#!/usr/bin/env python3
"""One-command local runner for the complete HackAlem solution.

The script deliberately uses only the Python standard library. It runs the
deterministic graph pipeline, rebuilds the local dashboard, and then starts the
analyst-assistant HTTP server, including optional .env-backed role explanations.
Use ``--no-server`` for a batch-only run.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "input_data" / "data"
DEFAULT_OUTPUT = ROOT / "output"
DEFAULT_STATIC = ROOT / "dashboard" / "dist"


def configure_console() -> None:
    """Keep Russian progress messages safe on Windows and redirected consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def child_environment() -> dict[str, str]:
    """Make source and the repository-local dependency cache importable."""

    environment = os.environ.copy()
    candidates = [ROOT / "src", ROOT]
    # A proper environment must not be shadowed by an old cache built for a
    # different Python version. Keep the cache as a legacy fallback only.
    if any(importlib.util.find_spec(name) is None for name in ("pandas", "pyarrow", "networkx")):
        candidates.insert(0, ROOT / ".deps")
    entries = [str(path) for path in candidates if path.exists()]
    current = environment.get("PYTHONPATH")
    if current:
        entries.append(current)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def run_step(label: str, command: list[str], environment: dict[str, str]) -> None:
    print(f"\n[{label}]", flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def main() -> None:
    configure_console()
    parser = argparse.ArgumentParser(
        description="Analyse the transaction graph, build the UI, and serve it locally."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--skip-pipeline", action="store_true")
    parser.add_argument("--skip-dashboard", action="store_true")
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()

    data_dir = args.data.resolve()
    output_dir = args.out.resolve()
    environment = child_environment()

    if not args.skip_pipeline:
        run_step(
            "1/2 Расчёт признаков, ролей, кластеров и приоритета",
            [
                sys.executable,
                str(ROOT / "run_pipeline.py"),
                "--data",
                str(data_dir),
                "--out",
                str(output_dir),
                "--validate",
            ],
            environment,
        )

    if not args.skip_dashboard:
        run_step(
            "2/2 Сборка локального интерфейса",
            [
                sys.executable,
                str(ROOT / "dashboard" / "build_dashboard.py"),
                "--data",
                str(data_dir),
                "--outputs",
                str(output_dir),
                "--dist",
                str(DEFAULT_STATIC),
            ],
            environment,
        )

    if args.no_server:
        print(f"\nГотово. Выгрузки: {output_dir}")
        print(f"Dashboard: {DEFAULT_STATIC / 'index.html'}")
        return

    url = f"http://{args.host}:{args.port}/"
    if args.open_browser:
        def open_when_ready() -> None:
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=open_when_ready, daemon=True).start()

    print(f"\nЛокальный интерфейс: {url}")
    print("Остановить: Ctrl+C")
    command = [
        sys.executable,
        "-m",
        "agent_app.server",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--static",
        str(DEFAULT_STATIC),
        "--data",
        str(data_dir),
        "--output",
        str(output_dir),
    ]
    try:
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == "__main__":
    main()
