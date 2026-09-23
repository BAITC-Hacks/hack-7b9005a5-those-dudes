from __future__ import annotations

import hashlib
import math
import numbers
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd


NODE_COLUMNS = {"gid", "depth", "is_seed"}
EDGE_COLUMNS = {"src", "dst", "sum_kzt", "n_tx", "depth"}
TX_COLUMNS = {"src", "dst", "date", "sum_kzt"}


@dataclass(frozen=True)
class InputData:
    nodes: pd.DataFrame
    edges: pd.DataFrame
    transactions: pd.DataFrame
    checks: dict[str, object]


def _require_columns(frame: pd.DataFrame, expected: set[str], name: str) -> None:
    missing = expected - set(frame.columns)
    if missing:
        raise ValueError(f"{name}: отсутствуют колонки {sorted(missing)}")
    for column in sorted(expected):
        if frame[column].isna().any():
            raise ValueError(f"{name}: колонка {column} содержит пустые значения")


def _integers(
    values: pd.Series, name: str, minimum: int = -(2**63), maximum: int = 2**63 - 1
) -> pd.Series:
    """Convert without truncating fractions, wrapping uint64, or rounding GIDs."""

    converted: list[int] = []
    for value in values:
        if isinstance(value, (bool, np.bool_)):
            raise ValueError(f"{name}: требуется целое число, не логическое значение")
        if isinstance(value, numbers.Integral):
            integer = int(value)
        elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
            integer = int(value.strip())
        elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
            integer = int(value)
        elif isinstance(value, numbers.Real) and math.isfinite(value) and float(value).is_integer():
            # Above 2**53 a floating-point source may have already lost digits.
            if abs(value) >= 2**53:
                raise ValueError(f"{name}: большие целые значения должны храниться как int64 или строки, не float")
            integer = int(value)
        else:
            raise ValueError(f"{name}: требуются конечные целые числа без дробной части")
        if not minimum <= integer <= maximum:
            raise ValueError(f"{name}: значение вне диапазона {minimum}..{maximum}")
        converted.append(integer)
    return pd.Series(converted, index=values.index, dtype="int64")


def _booleans(values: pd.Series, name: str) -> pd.Series:
    converted: list[bool] = []
    for value in values:
        if isinstance(value, (bool, np.bool_)):
            converted.append(bool(value))
        elif isinstance(value, numbers.Real) and value in (0, 1):
            converted.append(bool(value))
        elif isinstance(value, str) and value.strip().lower() in {"true", "false", "0", "1"}:
            converted.append(value.strip().lower() in {"true", "1"})
        else:
            raise ValueError(f"{name}: допустимы только true/false или 0/1")
    return pd.Series(converted, index=values.index, dtype=bool)


def _amounts(values: pd.Series, name: str) -> pd.Series:
    if values.map(lambda value: isinstance(value, (bool, np.bool_))).any():
        raise ValueError(f"{name}: суммы должны быть числами, не логическими значениями")
    try:
        converted = pd.to_numeric(values, errors="raise").astype(float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: суммы должны быть конечными неотрицательными числами") from exc
    if not np.isfinite(converted).all() or (converted < 0).any():
        raise ValueError(f"{name}: суммы должны быть конечными неотрицательными числами")
    # Downstream features can add inbound and outbound volume; leave headroom.
    if not math.isfinite(float(converted.sum()) * 2):
        raise ValueError(f"{name}: суммарный объём слишком велик для численных вычислений")
    return converted


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_inputs(data_dir: Path) -> InputData:
    """Load and validate the three supplied parquet tables.

    Transaction rows are intentionally never de-duplicated: identical rows are
    legitimate observations in the source and contribute to counts and amounts.
    GIDs stay as int64 internally and are converted to strings only on export.
    """

    data_dir = Path(data_dir)
    paths = {
        "nodes": data_dir / "nodes.parquet",
        "edges": data_dir / "edges.parquet",
        "transactions": data_dir / "transactions.parquet",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Не найдены входные файлы: " + ", ".join(missing))

    nodes = pd.read_parquet(paths["nodes"]).copy()
    edges = pd.read_parquet(paths["edges"]).copy()
    tx = pd.read_parquet(paths["transactions"]).copy()
    _require_columns(nodes, NODE_COLUMNS, "nodes.parquet")
    _require_columns(edges, EDGE_COLUMNS, "edges.parquet")
    _require_columns(tx, TX_COLUMNS, "transactions.parquet")

    for name, frame, columns in (
        ("nodes.parquet", nodes, ["gid"]),
        ("edges.parquet", edges, ["src", "dst"]),
        ("transactions.parquet", tx, ["src", "dst"]),
    ):
        for column in columns:
            frame[column] = _integers(frame[column], f"{name}: {column}")
    nodes["depth"] = _integers(nodes["depth"], "nodes.parquet: depth", 0, 4)
    edges["depth"] = _integers(edges["depth"], "edges.parquet: depth", 0, 4)
    nodes["is_seed"] = _booleans(nodes["is_seed"], "nodes.parquet: is_seed")
    edges["n_tx"] = _integers(edges["n_tx"], "edges.parquet: n_tx", 1)
    edges["sum_kzt"] = _amounts(edges["sum_kzt"], "edges.parquet: sum_kzt")
    tx["sum_kzt"] = _amounts(tx["sum_kzt"], "transactions.parquet: sum_kzt")
    if tx["date"].map(lambda value: isinstance(value, numbers.Number)).any():
        raise ValueError("transactions.parquet: date должна содержать даты, не числовые метки времени")
    try:
        dates = pd.to_datetime(tx["date"], errors="raise")
        if dates.isna().any() or dates.dt.tz is not None:
            raise ValueError("date must be non-null and timezone-naive")
        tx["date"] = dates.dt.normalize()
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError("transactions.parquet: date должна содержать корректные даты без часового пояса") from exc

    if nodes["gid"].duplicated().any():
        raise ValueError("nodes.parquet содержит повторяющиеся gid")
    if edges.duplicated(["src", "dst"]).any():
        raise ValueError("edges.parquet должен содержать одну строку на направленную пару")
    node_ids = set(nodes["gid"])
    edge_ids = set(edges["src"]) | set(edges["dst"])
    tx_ids = set(tx["src"]) | set(tx["dst"])
    if not edge_ids.issubset(node_ids) or not tx_ids.issubset(node_ids):
        raise ValueError("В рёбрах или транзакциях есть gid, отсутствующие в nodes.parquet")

    # Reconcile both amount and count, retaining duplicate transaction rows.
    aggregate = (
        tx.groupby(["src", "dst"], as_index=False, sort=False)
        .agg(tx_sum_kzt=("sum_kzt", "sum"), tx_n=("sum_kzt", "size"))
    )
    reconciled = edges.merge(aggregate, on=["src", "dst"], how="outer", indicator=True)
    pairs_match = bool((reconciled["_merge"] == "both").all())
    sums_match = pairs_match and bool(
        np.allclose(reconciled["sum_kzt"], reconciled["tx_sum_kzt"], rtol=1e-9, atol=0.01)
    )
    counts_match = pairs_match and bool((reconciled["n_tx"] == reconciled["tx_n"]).all())
    if not (pairs_match and sums_match and counts_match):
        raise ValueError("edges.parquet не сходится с transactions.parquet по парам, суммам или n_tx")

    checks: dict[str, object] = {
        "n_nodes": int(len(nodes)),
        "n_edges": int(len(edges)),
        "n_transactions": int(len(tx)),
        "n_exact_duplicate_transaction_rows": int(tx.duplicated().sum()),
        "n_seed": int(nodes["is_seed"].sum()),
        "n_isolated_nodes": int(len(node_ids - edge_ids)),
        "total_kzt": float(tx["sum_kzt"].sum()),
        "date_min": tx["date"].min().date().isoformat() if len(tx) else None,
        "date_max": tx["date"].max().date().isoformat() if len(tx) else None,
        "edge_transaction_reconciliation": True,
        "sha256": {name: _file_sha256(path) for name, path in paths.items()},
    }
    return InputData(nodes=nodes, edges=edges, transactions=tx, checks=checks)
