"""Fixed competition schemas; extended artifacts are separate, never discarded."""
from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path

CSV_COLUMNS = {
    "nodes_roles.csv": ("gid", "role", "role_score", "cluster_id", "priority_score", "evidence"),
    "clusters.csv": ("cluster_id", "n_nodes", "n_seed", "sum_kzt_internal", "top_gids", "hypothesis"),
    "top_nodes.csv": ("rank", "gid", "role", "priority_score", "why"),
}


def extended_path(path: Path) -> Path:
    candidate = path.with_name(path.stem + "_extended.csv")
    return candidate if path.name in CSV_COLUMNS and candidate.is_file() else path


def competition_bytes(source: Path, filename: str) -> bytes:
    """Project old or new baseline outputs without float conversion or API calls."""
    fields = CSV_COLUMNS[filename]
    output = StringIO(newline="")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(fields) <= set(reader.fieldnames or []):
            raise ValueError("Incomplete competition CSV")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")
