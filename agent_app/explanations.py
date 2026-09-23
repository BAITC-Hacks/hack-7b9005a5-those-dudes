"""Node-scoped role explanations over immutable pipeline evidence."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
from threading import Lock
from typing import Any

from . import agent
from .config import get_ai_settings
from .engine import ANALYST_CAVEAT, ROLE_LABELS
from .store import _read_csv, row_to_native

ROLES = tuple(ROLE_LABELS)
METRICS = (
    "depth", "is_seed", "truncated_by_depth", "p_continue", "in_deg", "out_deg",
    "in_tx", "out_tx", "in_kzt", "out_kzt", "pass_through", "retention",
    "balance_similarity", "in_flow_share", "out_flow_share", "fifo_0d", "fifo_1d",
    "max_in_sources_day", "max_out_targets_day", "repeat_route_count",
    "reciprocal_neighbors", "temporal_cycle_count", "direct_seed_in", "seed_affinity",
    "pagerank_amount", "pagerank_count", "hits_authority", "hits_hub", "brokerage",
    "betweenness", "participation", "cluster_stability", "observability", "role_stability",
    "p_brokerage", "p_seed_affinity", "p_participation", "p_pagerank_amount",
    "p_pagerank_count", "p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle",
    "weighted_betweenness", "fragmentation_impact", "cycle_count", "n_seed_reachable",
    "active_in_days", "active_out_days", "median_fifo_lag", "priority_raw",
)

PRIORITY_WEIGHTS = {"connectivity": .30, "exposure": .22, "seed": .18,
                    "temporal": .15, "role": .10, "uncertainty": .05}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


from money_graph.rules import decision_trace


class RoleExplanationService:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.store.paths.root
        output = engine.store.paths.output_dir
        features = _read_csv(output / "features.csv")
        self.features = {str(row["gid"]): row_to_native(row) for _, row in features.iterrows()} if "gid" in features else {}
        path = output / "thresholds.json"
        self.thresholds = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.cache: OrderedDict[str, str] = OrderedDict()
        self.lock = Lock()
        dates = engine.store.transactions["date"]
        self.sampling = {
            "date_start": str(dates.min().date()) if len(dates) else None,
            "date_end": str(dates.max().date()) if len(dates) else None,
            "max_depth": 4, "outgoing_only": True,
            "minimum_observed_transfer_kzt": 5000, "daily_dates_only": True,
        }

    def status(self) -> dict:
        settings = get_ai_settings(self.root)
        return {"configured": settings.configured, "sdk_available": agent.SDK_AVAILABLE,
                "ready": settings.configured and agent.SDK_AVAILABLE, "model": settings.model,
                "provider": "OpenAI", "scope": "selected_node_metrics_only"}

    def payload(self, gid: str) -> dict | None:
        row = self.engine.store.node_row(gid)
        if row is None:
            return None
        metrics = {**self.features.get(gid, {}), **row}
        # Identifiers, neighbors and individual transactions never enter the model prompt.
        return {"assigned_role": row.get("role"), "role_score": _number(row.get("role_score")),
                "secondary_role": row.get("secondary_role"), "priority_score": _number(row.get("priority_score")),
                "evidence": row.get("evidence"), "uncertainty_reason": row.get("uncertainty_reason"),
                "metrics": {key: value for key in METRICS if (value := _number(metrics.get(key))) is not None},
                "decision_trace": decision_trace(metrics, self.thresholds),
                "priority_components": {key: value for key in PRIORITY_WEIGHTS
                                        if (value := _number(metrics.get("priority_" + key))) is not None},
                "priority_weights": PRIORITY_WEIGHTS.copy(),
                "priority_formula": self.thresholds.get("priority_formula"),
                "sampling": self.sampling.copy()}

    @staticmethod
    def local_explanation(payload: dict) -> str:
        role = payload["assigned_role"]
        score = payload["role_score"]
        m = payload["metrics"]
        fmt = lambda value: "нет данных" if value is None else f"{value:,.4f}".rstrip("0").rstrip(".").replace(",", " ")
        paragraphs = [f"Назначена роль «{ROLE_LABELS.get(role, role)}» ({role}); уверенность роли {fmt(score)}. Это эвристическая оценка, не вероятность виновности.",
                      f"Наблюдаемые входы: {fmt(m.get('in_deg'))} источников, {fmt(m.get('in_kzt'))} KZT; выходы: {fmt(m.get('out_deg'))} получателей, {fmt(m.get('out_kzt'))} KZT."]
        trace = payload["decision_trace"]
        if trace["available"]:
            candidates = trace["roles"]
            eligible = [f"{name}={fmt(item['profile_score'])}" for name, item in candidates.items() if item["eligible"]]
            paragraphs.append("Допущенные по правилам роли и профильные оценки: " + "; ".join(eligible) + ". Выбрана максимальная оценка среди допущенных ролей.")
            if role in candidates and candidates[role]["conditions"]:
                checks = candidates[role]["conditions"]
                paragraphs.append("Условия выбранной роли: " + "; ".join(f"{item['condition']}: {item['observed']} {item['operator']} {item['threshold']} — {'выполнено' if item['passed'] else 'не выполнено'}" for item in checks) + ".")
        if payload.get("evidence"):
            paragraphs.append("Сохранённое основание: " + str(payload["evidence"]))
        if payload.get("secondary_role"):
            alternative = payload["secondary_role"]
            eligible = trace.get("roles", {}).get(alternative, {}).get("eligible")
            paragraphs.append(f"Альтернатива: {alternative}" + ("; условия допуска не пройдены." if eligible is False else "."))
            if eligible is False:
                failed = [item for item in trace["roles"][alternative]["conditions"] if not item["passed"]]
                paragraphs.append("Невыполненные условия альтернативы: " + "; ".join(f"{item['condition']}: наблюдается {item['observed']}, требуется {item['operator']} {item['threshold']}" for item in failed) + ".")
        if payload.get("uncertainty_reason"):
            paragraphs.append("Неопределённость: " + str(payload["uncertainty_reason"]))
        if m.get("depth") == 4 or m.get("truncated_by_depth"):
            paragraphs.append("Depth-4: исходящие потоки цензурированы; нулевой наблюдаемый выход не доказывает terminal.")
        if m.get("is_seed"):
            paragraphs.append("Для seed внешние входящие потоки могут отсутствовать в выборке.")
        paragraphs.append("Следующий шаг: проверить полноту входов и выходов и соответствие временных маршрутов выбранной роли. " + ANALYST_CAVEAT)
        return "\n\n".join(paragraphs)

    def explain(self, gid: str) -> tuple[dict, int]:
        payload = self.payload(gid)
        if payload is None:
            return {"status": "not_found", "message": "Узел не найден."}, 404
        if payload["assigned_role"] not in ROLES:
            return {"status": "unavailable", "message": "Сначала рассчитайте роли pipeline."}, 409
        settings = get_ai_settings(self.root)
        result = {"status": "ok", "gid": gid, "role": payload["assigned_role"], "role_score": payload["role_score"],
                  "mode": "offline", "explanation": self.local_explanation(payload), "cached": False}
        if not settings.configured:
            return {**result, "notice": "Локальное объяснение. Добавьте OPENAI_API_KEY в .env для генерации через OpenAI."}, 200
        if not agent.SDK_AVAILABLE:
            return {**result, "notice": "Локальное объяснение. Установите requirements-ai.txt для подключения OpenAI."}, 200
        if not self.lock.acquire(blocking=False):
            return {"status": "busy", "message": "Другое объяснение ещё готовится. Повторите через несколько секунд."}, 429
        try:
            prompt = (Path(__file__).parent / "docs" / "role_explanation.md").read_text(encoding="utf-8")
            key = hashlib.sha256(json.dumps([payload, settings.model, prompt], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if key in self.cache:
                self.cache.move_to_end(key)
                return {**result, "mode": "openai", "model": settings.model, "explanation": self.cache[key], "cached": True}, 200
            try:
                answer = asyncio.run(agent.generate_role_explanation(payload, settings))
            except Exception as exc:
                # Provider exception text can contain credentials or request data.
                # Expose a mapped error only; never log the exception or secret.
                code = getattr(exc, "status_code", None)
                reason = {401: "ключ не принят", 403: "доступ запрещён", 404: "модель недоступна", 429: "превышен лимит запросов или баланса"}.get(code, "запрос не завершился; проверьте соединение и настройки")
                return {**result, "notice": f"OpenAI: {reason}. Показано локальное объяснение.", "api_error": True}, 200
            self.cache[key] = answer
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return {**result, "mode": "openai", "model": settings.model, "explanation": answer}, 200
        finally:
            self.lock.release()
