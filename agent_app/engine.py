"""Deterministic investigation tools and offline query routing.

This module is the authoritative data path for both the HTTP/CLI interface and
the optional OpenAI Agents SDK wrapper. No tool enriches customer data or makes
claims about guilt; outputs are numerical hypotheses for analyst review.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Iterable
import math
import re

import pandas as pd

from .store import GraphDataStore, native, row_to_native


ANALYST_CAVEAT = (
    "Это гипотеза по неполной исходящей выборке, а не вывод о нарушении; "
    "нужна проверка аналитиком и, при наличии полномочий, по полным данным."
)

ROLE_LABELS = {
    "consolidator": "сборщик",
    "transit": "транзит",
    "distributor": "распределитель",
    "terminal": "терминал",
    "coordinator": "координатор",
    "peripheral": "периферия",
}

ROLE_QUERY_STEMS = {
    "сбор": "consolidator",
    "консолид": "consolidator",
    "транз": "transit",
    "распредел": "distributor",
    "терминал": "terminal",
    "координ": "coordinator",
    "перифер": "peripheral",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да"}


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() not in {"", "nan", "<NA>"}:
            return value
    return default


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [row_to_native(row) for _, row in frame.iterrows()]


def _numeric_column(
    frame: pd.DataFrame, column: str, default: float = 0.0
) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").fillna(default)


class InvestigationEngine:
    """Explainable read-only operations over the financial graph."""

    def __init__(self, store: GraphDataStore) -> None:
        self.store = store

    def _response(
        self,
        action: str,
        message: str,
        data: Any = None,
        *,
        status: str = "ok",
        warnings: Iterable[str] = (),
    ) -> dict[str, Any]:
        return {
            "status": status,
            "action": action,
            "message": message,
            "data": data if data is not None else {},
            "warnings": list(warnings),
            "caveat": ANALYST_CAVEAT,
        }

    def _not_found(self, action: str, gid: str) -> dict[str, Any]:
        return self._response(
            action,
            f"Узел {gid} отсутствует в наблюдаемой выборке.",
            {"gid": gid},
            status="not_found",
        )

    def health(self) -> dict[str, Any]:
        data = {
            "nodes": len(self.store.nodes),
            "edges": len(self.store.edges),
            "transactions": len(self.store.transactions),
            "role_rows": len(self.store.roles),
            "cluster_rows": len(self.store.clusters),
            "resilience_rows": len(self.store.resilience),
            "pipeline_ready": self.store.pipeline_ready,
            "data_dir": str(self.store.paths.data_dir),
            "output_dir": str(self.store.paths.output_dir),
        }
        state = "готовы" if self.store.pipeline_ready else "ещё не рассчитаны"
        return self._response("health", f"Данные загружены; роли {state}.", data)

    def node_profile(self, gid: str | int) -> dict[str, Any]:
        key = str(gid).strip()
        row = self.store.node_row(key)
        if row is None:
            return self._not_found("node_profile", key)

        role = str(row.get("role") or "не рассчитана")
        role_score = _float(row.get("role_score"), default=0.0)
        priority = _float(row.get("priority_score"), default=0.0)
        depth = _int(row.get("depth"), default=-1)
        out_degree = _int(_pick(row, "out_deg", "out_degree"), default=0)
        truncated = _bool(row.get("truncated_by_depth")) or (
            depth == 4 and out_degree == 0
        )

        classification = {
            "role": role,
            "role_label_ru": ROLE_LABELS.get(role, role),
            "role_score": role_score,
            "secondary_role": row.get("secondary_role"),
            "priority_score": priority,
            "evidence": row.get("evidence"),
            "uncertainty_reason": row.get("uncertainty_reason"),
        }
        flow_keys = (
            "in_deg",
            "out_deg",
            "in_degree",
            "out_degree",
            "in_tx",
            "out_tx",
            "in_kzt",
            "out_kzt",
            "pass_through",
            "retention",
        )
        graph_keys = (
            "pagerank",
            "pagerank_amount",
            "pagerank_count",
            "hits_authority",
            "hits_hub",
            "betweenness",
            "weighted_betweenness",
            "participation",
            "fragmentation_impact",
            "cluster_stability",
        )
        temporal_keys = (
            "fifo_0d",
            "fifo_1d",
            "fifo_2d",
            "median_fifo_lag",
            "max_in_sources_day",
            "max_out_targets_day",
            "repeat_route_count",
            "reciprocal_neighbors",
            "cycle_count",
            "temporal_cycle_count",
        )
        seed_keys = ("direct_seed_in", "seed_affinity", "n_seed_reachable")

        warnings: list[str] = []
        if _bool(row.get("is_seed")):
            warnings.append(
                "Для seed входящие потоки вне исходящего обхода не наблюдаются; баланс занижен."
            )
        if truncated:
            warnings.append(
                "Depth-4 узел правоцензурирован: отсутствие исходящих рёбер не доказывает оседание средств."
            )

        sampling = {
            "depth": depth,
            "is_seed": _bool(row.get("is_seed")),
            "truncated_by_depth": truncated,
            "p_continue": _pick(
                row,
                "p_continue",
                "continuation_probability",
                "depth4_continue_prob",
            ),
        }
        data = {
            "gid": key,
            "cluster_id": native(row.get("cluster_id")),
            "classification": classification,
            "flow": {key: native(row.get(key)) for key in flow_keys if key in row},
            "graph": {key: native(row.get(key)) for key in graph_keys if key in row},
            "temporal": {
                key: native(row.get(key)) for key in temporal_keys if key in row
            },
            "seed_connectivity": {
                key: native(row.get(key)) for key in seed_keys if key in row
            },
            "sampling": sampling,
        }
        return self._response(
            "node_profile",
            (
                f"{key}: роль «{ROLE_LABELS.get(role, role)}» "
                f"(уверенность {role_score:.3f}), приоритет {priority:.3f}."
            ),
            data,
            warnings=warnings,
        )

    def explain_priority(self, gid: str | int) -> dict[str, Any]:
        key = str(gid).strip()
        row = self.store.node_row(key)
        if row is None:
            return self._not_found("explain_priority", key)

        components = {
            column.removeprefix("priority_"): _float(row[column])
            for column in row
            if column.startswith("priority_")
            and column not in {"priority_score"}
            and row[column] is not None
        }
        signals = {
            name: native(row.get(name))
            for name in (
                "betweenness",
                "weighted_betweenness",
                "pagerank_amount",
                "in_deg",
                "out_deg",
                "in_kzt",
                "out_kzt",
                "fifo_1d",
                "direct_seed_in",
                "seed_affinity",
                "fragmentation_impact",
                "role_score",
                "cluster_stability",
            )
            if name in row
        }
        score = _float(row.get("priority_score"))
        evidence = str(row.get("evidence") or "")
        reason = evidence or (
            "Компоненты приоритета доступны после запуска аналитического pipeline."
        )
        warnings = []
        if not self.store.pipeline_ready:
            warnings.append("Ролевые и приоритетные scores ещё не рассчитаны.")
        if _bool(row.get("truncated_by_depth")):
            warnings.append("Приоритет не следует трактовать как уверенность в terminal-роли.")
        return self._response(
            "explain_priority",
            f"Приоритет узла {key}: {score:.3f}. {reason}",
            {
                "gid": key,
                "priority_score": score,
                "role": row.get("role"),
                "role_score": native(row.get("role_score")),
                "components": components,
                "numeric_signals": signals,
                "evidence": evidence,
            },
            warnings=warnings,
        )

    def neighbors(
        self,
        gid: str | int,
        direction: str = "both",
        limit: int = 20,
    ) -> dict[str, Any]:
        key = str(gid).strip()
        if not self.store.has_node(key):
            return self._not_found("neighbors", key)
        direction = direction.lower().strip()
        if direction not in {"in", "out", "both"}:
            return self._response(
                "neighbors",
                "direction должен быть in, out или both.",
                status="invalid_request",
            )
        limit = _bounded(limit, 1, 100)
        candidates: list[dict[str, Any]] = []
        if direction in {"out", "both"}:
            for edge in self.store.outgoing.get(key, []):
                candidates.append(self._neighbor_record(edge, "out", edge["dst"]))
        if direction in {"in", "both"}:
            for edge in self.store.incoming.get(key, []):
                candidates.append(self._neighbor_record(edge, "in", edge["src"]))
        candidates.sort(
            key=lambda item: (-_float(item["sum_kzt"]), item["direction"], item["gid"])
        )
        selected = candidates[:limit]
        return self._response(
            "neighbors",
            f"Для {key} найдено {len(candidates)} наблюдаемых связей; показано {len(selected)}.",
            {
                "gid": key,
                "direction": direction,
                "total": len(candidates),
                "neighbors": selected,
            },
        )

    def _neighbor_record(
        self, edge: dict[str, Any], direction: str, counterparty: str
    ) -> dict[str, Any]:
        row = self.store.node_row(counterparty) or {}
        return {
            "gid": counterparty,
            "direction": direction,
            "sum_kzt": edge["sum_kzt"],
            "n_tx": edge["n_tx"],
            "role": row.get("role"),
            "role_score": native(row.get("role_score")),
            "priority_score": native(row.get("priority_score")),
            "truncated_by_depth": _bool(row.get("truncated_by_depth")),
        }

    def trace_routes(
        self,
        gid: str | int,
        max_hops: int = 3,
        limit: int = 10,
        min_kzt: float = 0.0,
    ) -> dict[str, Any]:
        key = str(gid).strip()
        if not self.store.has_node(key):
            return self._not_found("trace_routes", key)
        max_hops = _bounded(max_hops, 1, 4)
        limit = _bounded(limit, 1, 50)
        min_kzt = max(0.0, _float(min_kzt))
        queue: deque[tuple[list[str], list[dict[str, Any]]]] = deque([([key], [])])
        routes: list[dict[str, Any]] = []
        explored = 0
        max_states = 10_000

        while queue and len(routes) < limit and explored < max_states:
            path, path_edges = queue.popleft()
            explored += 1
            last = path[-1]
            eligible = [
                edge
                for edge in self.store.outgoing.get(last, [])
                if edge["sum_kzt"] >= min_kzt and edge["dst"] not in path
            ]
            if not eligible or len(path_edges) >= max_hops:
                if path_edges:
                    routes.append(self._route_record(path, path_edges))
                continue
            for edge in eligible[:25]:
                next_path = path + [edge["dst"]]
                next_edges = path_edges + [edge]
                if len(next_edges) >= max_hops:
                    routes.append(self._route_record(next_path, next_edges))
                    if len(routes) >= limit:
                        break
                else:
                    queue.append((next_path, next_edges))

        routes.sort(
            key=lambda item: (-item["bottleneck_kzt"], -item["edge_sum_kzt"], item["path"])
        )
        routes = routes[:limit]
        warning = []
        if explored >= max_states:
            warning.append("Поиск остановлен по лимиту состояний; показана верхняя часть маршрутов.")
        return self._response(
            "trace_routes",
            f"Из {key} показано {len(routes)} исходящих маршрутов длиной до {max_hops} рёбер.",
            {
                "gid": key,
                "max_hops": max_hops,
                "min_kzt": min_kzt,
                "routes": routes,
                "explored_states": explored,
            },
            warnings=warning,
        )

    @staticmethod
    def _route_record(path: list[str], edges: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "path": path,
            "hops": len(edges),
            "bottleneck_kzt": min(edge["sum_kzt"] for edge in edges),
            "edge_sum_kzt": sum(edge["sum_kzt"] for edge in edges),
            "n_tx_total": sum(edge["n_tx"] for edge in edges),
            "edges": [dict(edge) for edge in edges],
        }

    def common_recipients(
        self, gids: Iterable[str | int], limit: int = 20
    ) -> dict[str, Any]:
        unique = list(dict.fromkeys(str(gid).strip() for gid in gids if str(gid).strip()))
        missing = [gid for gid in unique if not self.store.has_node(gid)]
        present = [gid for gid in unique if self.store.has_node(gid)]
        if len(present) < 2:
            return self._response(
                "common_recipients",
                "Нужно минимум два существующих gid.",
                {"provided": unique, "missing": missing},
                status="invalid_request",
            )
        limit = _bounded(limit, 1, 100)
        recipient_sources: dict[str, dict[str, dict[str, Any]]] = {}
        for source in present:
            for edge in self.store.outgoing.get(source, []):
                recipient_sources.setdefault(edge["dst"], {})[source] = edge
        matches: list[dict[str, Any]] = []
        for recipient, by_source in recipient_sources.items():
            if len(by_source) < 2:
                continue
            row = self.store.node_row(recipient) or {}
            matches.append(
                {
                    "gid": recipient,
                    "n_sources": len(by_source),
                    "sources": sorted(by_source),
                    "sum_kzt": sum(edge["sum_kzt"] for edge in by_source.values()),
                    "n_tx": sum(edge["n_tx"] for edge in by_source.values()),
                    "role": row.get("role"),
                    "priority_score": native(row.get("priority_score")),
                }
            )
        matches.sort(key=lambda item: (-item["n_sources"], -item["sum_kzt"], item["gid"]))
        selected = matches[:limit]
        return self._response(
            "common_recipients",
            f"Для {len(present)} источников найдено {len(matches)} общих прямых получателей.",
            {
                "sources": present,
                "missing_sources": missing,
                "recipients": selected,
                "total": len(matches),
            },
        )

    def compare_nodes(self, gids: Iterable[str | int]) -> dict[str, Any]:
        unique = list(dict.fromkeys(str(gid).strip() for gid in gids if str(gid).strip()))
        if len(unique) < 2:
            return self._response(
                "compare_nodes",
                "Для сравнения нужно минимум два gid.",
                {"provided": unique},
                status="invalid_request",
            )
        unique = unique[:10]
        missing = [gid for gid in unique if not self.store.has_node(gid)]
        comparable: list[dict[str, Any]] = []
        for gid in unique:
            row = self.store.node_row(gid)
            if row is None:
                continue
            comparable.append(
                {
                    key: native(row.get(key))
                    for key in (
                        "gid",
                        "role",
                        "role_score",
                        "secondary_role",
                        "priority_score",
                        "cluster_id",
                        "in_deg",
                        "out_deg",
                        "in_tx",
                        "out_tx",
                        "in_kzt",
                        "out_kzt",
                        "pass_through",
                        "fifo_1d",
                        "betweenness",
                        "seed_affinity",
                        "truncated_by_depth",
                        "p_continue",
                        "evidence",
                        "uncertainty_reason",
                    )
                    if key in row
                }
            )
        comparable.sort(
            key=lambda item: (-_float(item.get("priority_score")), str(item.get("gid")))
        )
        return self._response(
            "compare_nodes",
            f"Сопоставлено {len(comparable)} узлов по одним и тем же наблюдаемым метрикам.",
            {"nodes": comparable, "missing": missing},
        )

    def cluster_summary(self, cluster_id: str | int) -> dict[str, Any]:
        wanted = str(cluster_id).strip()
        members = self.store.cluster_members(wanted)
        summary: dict[str, Any] = {}
        if not self.store.clusters.empty and "cluster_id" in self.store.clusters.columns:
            matching = self.store.clusters[
                self.store.clusters["cluster_id"].astype("string").str.strip() == wanted
            ]
            if not matching.empty:
                summary = row_to_native(matching.iloc[0])
        if members.empty and not summary:
            return self._response(
                "cluster_summary",
                f"Кластер {wanted} отсутствует в результатах.",
                {"cluster_id": wanted},
                status="not_found",
            )

        if not members.empty:
            sortable = members.copy()
            if "priority_score" in sortable.columns:
                sortable["_priority"] = pd.to_numeric(
                    sortable["priority_score"], errors="coerce"
                ).fillna(-1)
                sortable = sortable.sort_values(["_priority", "gid"], ascending=[False, True])
            top_columns = [
                column
                for column in ("gid", "role", "role_score", "priority_score", "evidence")
                if column in sortable.columns
            ]
            top_members = _records(sortable[top_columns].head(10))
            role_counts = (
                Counter(members["role"].astype(str)) if "role" in members.columns else Counter()
            )
            summary.setdefault("n_nodes", len(members))
            summary["role_counts"] = dict(sorted(role_counts.items()))
            summary["top_members"] = top_members
        summary["cluster_id"] = wanted
        return self._response(
            "cluster_summary",
            f"Кластер {wanted}: {summary.get('n_nodes', len(members))} узлов; это навигационная гипотеза, не членство в группе.",
            summary,
        )

    def top_candidates(
        self, role: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        limit = _bounded(limit, 1, 100)
        frame = self.store.roles.copy()
        if role:
            role = role.strip().lower()
            if "role" not in frame.columns:
                return self._response(
                    "top_candidates",
                    "Роли ещё не рассчитаны.",
                    status="unavailable",
                )
            frame = frame[frame["role"].astype("string").str.lower() == role]
        if "priority_score" not in frame.columns:
            return self._response(
                "top_candidates",
                "Приоритеты ещё не рассчитаны; запустите аналитический pipeline.",
                status="unavailable",
            )
        frame["_priority"] = pd.to_numeric(frame["priority_score"], errors="coerce").fillna(-1)
        frame = frame.sort_values(["_priority", "gid"], ascending=[False, True])
        columns = [
            column
            for column in (
                "gid",
                "role",
                "role_score",
                "priority_score",
                "cluster_id",
                "evidence",
                "uncertainty_reason",
                "truncated_by_depth",
            )
            if column in frame.columns
        ]
        result = _records(frame[columns].head(limit))
        label = f" для роли {role}" if role else ""
        return self._response(
            "top_candidates",
            f"Показано {len(result)} кандидатов{label}, ранжированных по investigative priority, а не по виновности.",
            {"role_filter": role, "candidates": result, "total_matching": len(frame)},
        )

    def uncertain_nodes(self, limit: int = 20) -> dict[str, Any]:
        limit = _bounded(limit, 1, 100)
        frame = self.store.roles.copy()
        if frame.empty:
            return self._response(
                "uncertain_nodes", "Результаты ролей отсутствуют.", status="unavailable"
            )
        if not self.store.pipeline_ready and "truncated_by_depth" not in frame.columns:
            return self._response(
                "uncertain_nodes",
                "Ролевая неопределённость ещё не рассчитана; запустите аналитический pipeline.",
                status="unavailable",
            )
        truncated = (
            frame["truncated_by_depth"].map(_bool)
            if "truncated_by_depth" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        low_confidence = (
            pd.to_numeric(frame["role_score"], errors="coerce").fillna(1.0) < 0.6
            if "role_score" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        reason_present = (
            frame["uncertainty_reason"].astype("string").str.len().fillna(0) > 0
            if "uncertainty_reason" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        selected = frame[truncated | low_confidence | reason_present].copy()
        selected["_confidence"] = _numeric_column(selected, "role_score")
        selected["_priority"] = _numeric_column(selected, "priority_score")
        selected = selected.sort_values(
            ["_priority", "_confidence", "gid"], ascending=[False, True, True]
        )
        columns = [
            column
            for column in (
                "gid",
                "role",
                "role_score",
                "priority_score",
                "depth",
                "truncated_by_depth",
                "p_continue",
                "uncertainty_reason",
                "evidence",
            )
            if column in selected.columns
        ]
        result = _records(selected[columns].head(limit))
        return self._response(
            "uncertain_nodes",
            f"Показано {len(result)} приоритетных случаев с ролевой неопределённостью или цензурой.",
            {"nodes": result, "total_matching": len(selected)},
        )

    def resilience_summary(self, strategy: str | None = None) -> dict[str, Any]:
        frame = self.store.resilience.copy()
        if frame.empty:
            return self._response(
                "resilience_summary",
                "resilience.csv отсутствует; запустите аналитический pipeline.",
                status="unavailable",
            )
        strategy_column = "strategy" if "strategy" in frame.columns else None
        if strategy and strategy_column:
            frame = frame[
                frame[strategy_column].astype("string").str.lower() == strategy.lower().strip()
            ]
        order = [column for column in ("strategy", "n", "n_removed", "removed_count") if column in frame.columns]
        if order:
            frame = frame.sort_values(order)
        return self._response(
            "resilience_summary",
            f"Показано {len(frame)} сценариев удаления; падение связности описывает структурную уязвимость, не вину узлов.",
            {"strategy_filter": strategy, "scenarios": _records(frame)},
        )

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute a structured, allow-listed read-only action."""

        action = str(request.get("action", "")).strip().lower()
        if action == "health":
            return self.health()
        if action in {"node", "node_profile", "profile"}:
            return self.node_profile(request.get("gid", ""))
        if action in {"explain", "explain_priority", "priority"}:
            return self.explain_priority(request.get("gid", ""))
        if action == "neighbors":
            return self.neighbors(
                request.get("gid", ""),
                str(request.get("direction", "both")),
                _int(request.get("limit"), 20),
            )
        if action in {"routes", "trace_routes", "trace"}:
            return self.trace_routes(
                request.get("gid", ""),
                _int(request.get("max_hops"), 3),
                _int(request.get("limit"), 10),
                _float(request.get("min_kzt"), 0.0),
            )
        if action in {"common", "common_recipients"}:
            gids = request.get("gids", [])
            if isinstance(gids, str):
                gids = re.findall(r"\d+", gids)
            return self.common_recipients(gids, _int(request.get("limit"), 20))
        if action in {"compare", "compare_nodes"}:
            gids = request.get("gids", [])
            if isinstance(gids, str):
                gids = re.findall(r"\d+", gids)
            return self.compare_nodes(gids)
        if action in {"cluster", "cluster_summary"}:
            return self.cluster_summary(request.get("cluster_id", ""))
        if action in {"top", "top_candidates"}:
            return self.top_candidates(request.get("role"), _int(request.get("limit"), 20))
        if action in {"uncertain", "uncertain_nodes"}:
            return self.uncertain_nodes(_int(request.get("limit"), 20))
        if action in {"resilience", "resilience_summary"}:
            return self.resilience_summary(request.get("strategy"))
        return self._response(
            "unknown",
            "Неизвестное действие. Доступны: health, node_profile, explain_priority, neighbors, trace_routes, common_recipients, compare_nodes, cluster_summary, top_candidates, uncertain_nodes, resilience_summary.",
            {"requested_action": action},
            status="invalid_request",
        )

    def answer_offline(self, question: str) -> dict[str, Any]:
        """Small deterministic Russian/English router for an API-free assistant."""

        text = str(question or "").strip()
        lowered = text.lower()
        gids = re.findall(r"(?<!\d)\d{8,}(?!\d)", text)
        limit_match = re.search(r"(?:топ|top|limit)\s*[:=]?\s*(\d{1,3})", lowered)
        limit = _bounded(int(limit_match.group(1)), 1, 100) if limit_match else 20

        if not text:
            return self._response(
                "offline_query",
                "Введите вопрос или используйте структурированный action.",
                status="invalid_request",
            )
        if any(token in lowered for token in ("health", "готов", "статус данных")):
            return self.health()
        if any(token in lowered for token in ("устойчив", "resilien", "удален")):
            return self.resilience_summary()
        if any(token in lowered for token in ("неопредел", "цензур", "depth-4", "depth 4")):
            return self.uncertain_nodes(limit)
        if any(token in lowered for token in ("общ", "common recipient")) and len(gids) >= 2:
            return self.common_recipients(gids, limit)
        if any(token in lowered for token in ("сравн", "compare", "различ")) and len(gids) >= 2:
            return self.compare_nodes(gids)
        cluster_match = re.search(r"(?:кластер|cluster)\s*#?\s*(-?\d+)", lowered)
        if cluster_match:
            return self.cluster_summary(cluster_match.group(1))
        if any(token in lowered for token in ("маршрут", "route", "trace")) and gids:
            hops_match = re.search(r"(\d)\s*(?:hop|шаг|реб)", lowered)
            hops = int(hops_match.group(1)) if hops_match else 3
            return self.trace_routes(gids[0], hops, min(limit, 50))
        if any(token in lowered for token in ("сосед", "neighbor", "контрагент")) and gids:
            direction = "out" if "исход" in lowered else "in" if "вход" in lowered else "both"
            return self.neighbors(gids[0], direction, limit)
        if any(token in lowered for token in ("почему", "объяс", "priority", "приоритет")) and gids:
            return self.explain_priority(gids[0])
        if any(token in lowered for token in ("топ", "top", "кандидат", "приоритет")):
            role = next((role for role in ROLE_LABELS if role in lowered), None)
            if role is None:
                role = next(
                    (mapped for stem, mapped in ROLE_QUERY_STEMS.items() if stem in lowered),
                    None,
                )
            return self.top_candidates(role, limit)
        if gids:
            return self.node_profile(gids[0])
        return self._response(
            "offline_query",
            (
                "Не удалось однозначно выбрать инструмент. Укажите gid и действие: "
                "профиль, приоритет, соседи, маршрут; либо запросите топ, кластер, "
                "неопределённость или устойчивость."
            ),
            {"question": text},
            status="needs_clarification",
        )
