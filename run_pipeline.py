#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from money_graph import PipelineConfig, run_pipeline  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Explainable data-only AML graph pipeline for HackAlem «Граф денег»."
    )
    parser.add_argument("--data", type=Path, default=ROOT / "input_data" / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "output")
    parser.add_argument("--seed", type=int, default=42, help="random seed for reproducible ensembles")
    parser.add_argument("--cluster-runs", type=int, default=16)
    parser.add_argument("--role-stability-runs", type=int, default=40)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="fail with a non-zero exit code when schema or synthetic checks fail",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metadata = run_pipeline(
        PipelineConfig(
            data_dir=args.data.resolve(),
            output_dir=args.out.resolve(),
            random_seed=args.seed,
            cluster_runs=max(1, args.cluster_runs),
            role_stability_runs=max(1, args.role_stability_runs),
            top_n=max(20, args.top_n),
            validate=args.validate,
        )
    )
    summary = {
        "status": metadata["status"],
        "output_dir": str(args.out.resolve()),
        "n_nodes": metadata["graph"]["n_nodes"],
        "roles": metadata["role_counts"],
        "total_seconds": round(metadata["timings"]["total_seconds"], 3),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
