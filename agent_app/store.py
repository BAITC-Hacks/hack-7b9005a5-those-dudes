"""Read-only data access for the investigation assistant.

The store intentionally keeps graph identifiers as strings.  The source values are
larger than JavaScript's safe integer range, and treating them as opaque identifiers
also prevents accidental arithmetic on customer IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import pandas as pd

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
from money_graph.io import load_inputs
from money_graph.contracts import extended_path


ID_COLUMNS = ("gid", "src", "dst")


@dataclass(frozen=True)
class ProjectPaths:
    """Filesystem locations used by the assistant."""

    root: Path
    data_dir: Path
    output_dir: Path

    @classmethod
    def from_values(
        cls,
        root: str | Path | None = None,
        data_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> "ProjectPaths":
        project_root = (
            Path(root).expanduser().resolve()
            if root is not None
            else Path(__file__).resolve().parents[1]
        )
        raw_dir = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else project_root / "input_data" / "data"
        )
        result_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else project_root / "output"
        )
        return cls(root=project_root, data_dir=raw_dir, output_dir=result_dir)


def _stringify_ids(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in ID_COLUMNS:
        if column in frame.columns:
            frame[column] = frame[column].astype("string").str.strip()
    return frame


def _read_csv(path: Path, id_columns: tuple[str, ...] = ID_COLUMNS) -> pd.DataFrame:
    path = extended_path(path)
    if not path.exists():
        return pd.DataFrame()
    header = pd.read_csv(path, nrows=0)
    dtype = {column: "string" for column in id_columns if column in header.columns}
    return _stringify_ids(pd.read_csv(path, dtype=dtype))


def native(value: Any) -> Any:
    """Convert pandas/numpy values to JSON-safe Python primitives."""

    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def row_to_native(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    items = row.items() if hasattr(row, "items") else []
    return {str(key): native(value) for key, value in items}


class GraphDataStore:
    """In-memory, read-only view over pipeline outputs and source parquet files."""

    def __init__(
        self,
        root: str | Path | None = None,
        data_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        *,
        strict_outputs: bool = False,
    ) -> None:
        self.paths = ProjectPaths.from_values(root, data_dir, output_dir)
        self._load(strict_outputs=strict_outputs)

    def _load(self, *, strict_outputs: bool) -> None:
        missing_raw = [
            path.name
            for path in (
                self.paths.data_dir / "nodes.parquet",
                self.paths.data_dir / "edges.parquet",
                self.paths.data_dir / "transactions.parquet",
            )
            if not path.exists()
        ]
        if missing_raw:
            joined = ", ".join(missing_raw)
            raise FileNotFoundError(
                f"Missing source data in {self.paths.data_dir}: {joined}"
            )

        # Use the same validated, normalized values as the pipeline and browser.
        # For example, string "false" is not a truthy seed and numeric 1.0 is GID 1.
        inputs = load_inputs(self.paths.data_dir)
        self.nodes = _stringify_ids(inputs.nodes)
        self.edges = _stringify_ids(inputs.edges)
        self.transactions = _stringify_ids(inputs.transactions)

        self.roles = _read_csv(self.paths.output_dir / "nodes_roles.csv")
        self.clusters = _read_csv(
            self.paths.output_dir / "clusters.csv", id_columns=("top_gids",)
        )
        self.top_nodes = _read_csv(self.paths.output_dir / "top_nodes.csv")

        resilience_path = self.paths.output_dir / "resilience.csv"
        if not resilience_path.exists():
            resilience_path = self.paths.output_dir / "resilience_summary.csv"
        self.resilience = _read_csv(resilience_path, id_columns=())

        if strict_outputs and self.roles.empty:
            raise FileNotFoundError(
                f"Pipeline output is missing: {self.paths.output_dir / 'nodes_roles.csv'}"
            )

        # A raw-only fallback keeps health checks and graph tools useful before the
        # analytical pipeline has run. It never invents role or priority values.
        if self.roles.empty:
            base_columns = [
                column
                for column in ("gid", "depth", "is_seed")
                if column in self.nodes.columns
            ]
            self.roles = self.nodes[base_columns].copy()

        self._role_index = self.roles.drop_duplicates("gid").set_index("gid", drop=False)
        self._node_index = self.nodes.drop_duplicates("gid").set_index("gid", drop=False)
        self.node_ids = set(self._node_index.index.astype(str))

        self.outgoing: dict[str, list[dict[str, Any]]] = {gid: [] for gid in self.node_ids}
        self.incoming: dict[str, list[dict[str, Any]]] = {gid: [] for gid in self.node_ids}
        for row in self.edges.itertuples(index=False):
            src = str(getattr(row, "src"))
            dst = str(getattr(row, "dst"))
            edge = {
                "src": src,
                "dst": dst,
                "sum_kzt": float(getattr(row, "sum_kzt", 0.0)),
                "n_tx": int(getattr(row, "n_tx", 0)),
            }
            if hasattr(row, "depth"):
                edge["depth"] = int(getattr(row, "depth"))
            self.outgoing.setdefault(src, []).append(edge)
            self.incoming.setdefault(dst, []).append(edge)

        for edges in self.outgoing.values():
            edges.sort(key=lambda item: (-item["sum_kzt"], item["dst"]))
        for edges in self.incoming.values():
            edges.sort(key=lambda item: (-item["sum_kzt"], item["src"]))

    @property
    def pipeline_ready(self) -> bool:
        return bool(
            "role" in self.roles.columns
            and self.roles["role"].astype("string").str.len().fillna(0).gt(0).any()
        )

    def has_node(self, gid: str | int) -> bool:
        return str(gid).strip() in self.node_ids

    def node_row(self, gid: str | int) -> dict[str, Any] | None:
        key = str(gid).strip()
        if key not in self.node_ids:
            return None
        raw = row_to_native(self._node_index.loc[key])
        if key in self._role_index.index:
            raw.update(row_to_native(self._role_index.loc[key]))
        raw["gid"] = key
        return raw

    def cluster_members(self, cluster_id: str | int) -> pd.DataFrame:
        if "cluster_id" not in self.roles.columns:
            return self.roles.iloc[0:0].copy()
        wanted = str(cluster_id).strip()
        return self.roles[
            self.roles["cluster_id"].astype("string").str.strip() == wanted
        ].copy()
