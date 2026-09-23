"""Read-only acceptance check against an independent temporal-cycle reference."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from io import BytesIO, StringIO
from urllib.request import urlopen
from zipfile import ZipFile

import networkx as nx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_graph.contracts import CSV_COLUMNS
from money_graph.features import build_graph
from money_graph.io import load_inputs
from money_graph.rules import decision_trace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "input_data/data")
    parser.add_argument("--out", type=Path, default=ROOT / "output")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--url", help="Optional localhost server to verify both live ZIP exports; no API calls")
    args = parser.parse_args()
    inputs = load_inputs(args.data)
    graph = build_graph(inputs.nodes, inputs.edges)
    # Absolute ordinals, not day-of-month, for multi-month uploaded windows.
    dates = {(int(s), int(t)): {pd.Timestamp(date).toordinal() for date in group.date}
             for (s, t), group in inputs.transactions.groupby(["src", "dst"])}
    counts = {int(gid): 0 for gid in inputs.nodes.gid}
    reference = 0
    for cycle in nx.simple_cycles(graph, length_bound=6):
        if len(cycle) < 2:
            continue
        valid = False
        for rotation in range(len(cycle)):
            route = cycle[rotation:] + cycle[:rotation]
            # Independent state expansion retains ALL feasible dates, unlike
            # the production greedy/bisect implementation.
            first_edge = (route[0], route[1])
            states = {(date, date) for date in dates[first_edge]}
            for i in range(1, len(route)):
                edge = (route[i], route[(i + 1) % len(route)])
                states = {(first, day) for first, previous in states for day in dates[edge]
                          if previous <= day <= first + 7}
                if not states:
                    break
            if states:
                valid = True
                break
        if valid:
            reference += 1
            for gid in cycle:
                counts[int(gid)] += 1
    frame = pd.read_csv(args.out / "features.csv", dtype={"gid": str})
    meta = json.loads((args.out / "thresholds.json").read_text(encoding="utf-8"))
    mismatches = sum(int(row.temporal_cycle_count) != counts[int(row.gid)] for row in frame.itertuples())
    assert mismatches == 0, f"Temporal mismatches: {mismatches}"
    assert meta["motifs"]["n_temporally_ordered_cycles_7d"] == reference
    contract = {}
    for name, fields in CSV_COLUMNS.items():
        with (args.out / name).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            assert tuple(reader.fieldnames) == fields
            rows = list(reader)
            contract[name] = {"columns": len(fields), "rows": len(rows)}
            for row in rows:
                for field in ("evidence", "why"):
                    if field in row:
                        assert 0 < len(row[field]) <= 200
    winners = []
    for row in frame.to_dict("records"):
        trace = decision_trace(row, meta)
        eligible = [role for role in trace["role_order"] if trace["roles"][role]["eligible"]]
        winners.append(max(eligible, key=lambda role: trace["roles"][role]["profile_score"]) == row["role"])
    assert all(winners)
    report = {"status": "ok", "independent_temporal_cycles": reference, "temporal_node_mismatches": mismatches,
              "fixed_contract": contract, "verified_role_decisions": len(winners)}
    if args.url:
        live = {}
        for mode in ("contest", "extended"):
            with urlopen(args.url.rstrip("/") + "/api/datasets/default/exports/results.zip?mode=" + mode, timeout=30) as response:
                archive_bytes = response.read()
            with ZipFile(BytesIO(archive_bytes)) as archive:
                assert set(archive.namelist()) == set(CSV_COLUMNS)
                live[mode] = {}
                for name, fields in CSV_COLUMNS.items():
                    reader = csv.DictReader(StringIO(archive.read(name).decode("utf-8-sig")))
                    rows = list(reader)
                    if mode == "contest":
                        assert tuple(reader.fieldnames) == fields
                        with (args.out / name).open(encoding="utf-8-sig", newline="") as stream:
                            assert rows == list(csv.DictReader(stream))
                    else:
                        assert set(fields) <= set(reader.fieldnames)
                        assert len(rows) == contract[name]["rows"]
                    live[mode][name] = {"columns": len(reader.fieldnames), "rows": len(rows)}
        report["http_exports"] = live
    if args.baseline:
        before = pd.read_csv(args.baseline / "features.csv", dtype={"gid": str}).set_index("gid").loc[frame.gid]
        after = frame.set_index("gid")
        report["changes"] = {"role": int((before.role != after.role).sum())}
        for field in ("temporal_cycle_count", "priority_score", "role_score"):
            report["changes"][field] = int((~np.isclose(before[field], after[field], rtol=0, atol=1e-12)).sum())
        old_top = pd.read_csv(args.baseline / "top_nodes.csv", dtype={"gid": str})
        new_top = pd.read_csv(args.out / "top_nodes.csv", dtype={"gid": str})
        report["changes"]["top_members_added"] = len(set(new_top.gid) - set(old_top.gid))
        report["changes"]["top_positions"] = int((old_top.gid != new_top.gid).sum())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
