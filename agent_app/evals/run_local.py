#!/usr/bin/env python3
"""Offline contract evals over the real deterministic assistant path."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_app.engine import ANALYST_CAVEAT, InvestigationEngine  # noqa: E402
from agent_app.store import GraphDataStore  # noqa: E402


def check_case(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if result.get("status") != expected.get("status"):
        failures.append(
            f"status={result.get('status')!r}, expected={expected.get('status')!r}"
        )
    data = result.get("data", {})
    for key in expected.get("data_keys", []):
        if key not in data:
            failures.append(f"missing data key: {key}")
    max_items = expected.get("max_items")
    if max_items and len(data.get(max_items["key"], [])) > int(max_items["value"]):
        failures.append(f"too many items in {max_items['key']}")
    warning_contains = expected.get("warning_contains")
    if warning_contains and not any(
        warning_contains in str(warning) for warning in result.get("warnings", [])
    ):
        failures.append(f"warning does not contain: {warning_contains}")
    if result.get("caveat") != ANALYST_CAVEAT:
        failures.append("analyst caveat missing or changed")
    serialized = json.dumps(result, ensure_ascii=False).lower()
    for forbidden in ("доказана вина", "точно преступ", "guilty"):
        if forbidden in serialized:
            failures.append(f"forbidden assertion: {forbidden}")
    return failures


def materialize_request(
    case: dict[str, Any], store: GraphDataStore
) -> dict[str, Any]:
    request = dict(case["request"])
    selector = case.get("selector")
    if selector == "first_depth4":
        if "truncated_by_depth" not in store.roles.columns:
            raise RuntimeError("nodes_roles.csv has no truncated_by_depth column")
        mask = store.roles["truncated_by_depth"].astype("string").str.lower().isin(
            {"true", "1"}
        )
        matches = store.roles.loc[mask, "gid"].astype(str).tolist()
        if not matches:
            raise RuntimeError("no depth-4 censored node available for eval")
        request["gid"] = matches[0]
    elif selector == "top_pair":
        if "priority_score" not in store.roles.columns:
            raise RuntimeError("nodes_roles.csv has no priority_score column")
        ranked = store.roles.assign(
            _score=pd.to_numeric(store.roles["priority_score"], errors="coerce")
        ).sort_values(["_score", "gid"], ascending=[False, True])
        request["gids"] = ranked["gid"].astype(str).head(2).tolist()
    return request


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    cases_path = Path(__file__).with_name("cases.jsonl")
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    store = GraphDataStore(root=PROJECT_ROOT, strict_outputs=True)
    engine = InvestigationEngine(store)
    results = []
    failed = 0
    for case in cases:
        response = engine.execute(materialize_request(case, store))
        failures = check_case(response, case["expect"])
        failed += bool(failures)
        results.append(
            {
                "id": case["id"],
                "passed": not failures,
                "failures": failures,
                "status": response.get("status"),
                "action": response.get("action"),
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline",
        "api_called": False,
        "total": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "cases": results,
    }
    results_dir = Path(__file__).with_name("results")
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
