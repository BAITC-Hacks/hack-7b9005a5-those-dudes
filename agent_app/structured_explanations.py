"""API-authored interpretations with server-owned evidence and numeric values.

This module imports without the optional SDK/Pydantic dependencies. No API call
occurs on import, preparation, or validation. Numeric grounding is structural;
the interpretation still requires human review and is not a proof of truth.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
from typing import Any

from .config import AISettings

PROMPT_PATH = Path(__file__).parent / "docs" / "structured_explanation.md"
PROMPT_VERSION = "structured-evidence-v6-human-200-words:" + hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:16]
MAX_NARRATIVE_WORDS = 200
FACT_PLACEHOLDER = re.compile(r"\{\{([a-zA-Z0-9_.]+)\}\}")
SDK_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("agents", "openai", "pydantic"))
ROLES = ("consolidator", "transit", "distributor", "terminal", "coordinator", "peripheral")
PRIORITY_WEIGHTS = {"connectivity": .30, "exposure": .22, "seed": .18, "temporal": .15, "role": .10, "uncertainty": .05}
PRIORITY_FORMULA = "ECDF(0.30*K+0.22*E+0.18*S+0.15*T+0.10*X+0.05*U)"
ROLE_SCORE_FORMULA = "observability*(0.45*profile+0.30*min(1,margin/0.25)+0.25*score_perturbation_stability)"
# Server-owned semantics mirror features.py, clustering.py, and scoring.py.
# These definitions are not read from uploaded or previously generated prose.
METRIC_DEFINITIONS = {
    "in_deg": "Number of unique observed incoming counterparties, not transaction count.",
    "out_deg": "Number of unique observed outgoing counterparties, not transaction count.",
    "in_tx": "Count of observed incoming transactions within the supplied window.",
    "out_tx": "Count of observed outgoing transactions within the supplied window.",
    "in_kzt": "Sum of this node's observed incoming transfers in KZT; not full account inflow.",
    "out_kzt": "Sum of this node's observed outgoing transfers in KZT; not full account outflow.",
    "in_flow_share": "in_kzt/(in_kzt+out_kzt), or zero when denominator is zero. Node-local share, NEVER share of network turnover.",
    "out_flow_share": "out_kzt/(in_kzt+out_kzt), or zero when denominator is zero. Node-local share, NEVER share of network turnover.",
    "pass_through": "out_kzt/in_kzt when incoming amount is positive; may exceed one; no temporal ordering is implied.",
    "retention": "max(in_kzt-out_kzt,0)/in_kzt when positive inflow, else zero; observed amount imbalance, not an account balance.",
    "balance_similarity": "min(in_kzt,out_kzt)/max(in_kzt,out_kzt) when both positive, otherwise zero; comparable aggregate volumes, not timing.",
    "fifo_0d": "Greedy FIFO matched amount on the same date divided by min(in_kzt,out_kzt), zero without both flows; not necessarily fraction of all inflow.",
    "fifo_1d": "Greedy FIFO matched amount within same/next date divided by min(in_kzt,out_kzt), zero without both flows; not necessarily fraction of all inflow.",
    "median_fifo_lag": "Amount-weighted median lag of FIFO matches allowing up to two days; daily precision only.",
    "depth": "Shortest observed crawl depth from the seeds; outgoing-only crawl ends at depth four.",
    "is_seed": "Flag for a crawl origin, not a behavioral classification.",
    "truncated_by_depth": "True when depth equals four and observed out-degree is zero; outgoing flows are right-censored.",
    "p_continue": "Inbound-feature logistic estimate of an outgoing edge, trained on observed depths one to three; not guilt or role probability.",
    "max_in_sources_day": "Largest number of distinct incoming counterparties on a date; does not establish coordinated control.",
    "max_out_targets_day": "Largest number of distinct outgoing counterparties on a date; does not establish coordinated control.",
    "active_in_days": "Count of dates with observed incoming transactions.",
    "active_out_days": "Count of dates with observed outgoing transactions.",
    "repeat_route_count": "Count of source-node-target pairs occurring on at least two date pairs with same/next-day order and amount ratio between one-half and two.",
    "reciprocal_neighbors": "Number of counterparties with observed edges in both directions; not a measure of offsetting amounts.",
    "cycle_count": "Number of structural directed cycles of length two through six containing this node.",
    "temporal_cycle_count": "Number of those cycles admitting nondecreasing daily dates within seven days; not proof of circulation of the same money.",
    "direct_seed_in": "Number of distinct seeds with a direct observed edge to this node.",
    "n_seed_reachable": "Number of seeds from which this node can be reached by an outgoing route of at most four hops; a seed reaches itself.",
    "seed_affinity": "Sum over seeds of exp(-0.7*distance) for outgoing routes of at most four hops, including a seed's self-distance.",
    "pagerank_amount": "Directed PageRank weighted by edge KZT sum; relative structural centrality, not flow share.",
    "pagerank_count": "Directed PageRank weighted by edge transaction count; relative structural centrality, not flow share.",
    "hits_authority": "Mean of amount-weighted and count-weighted HITS authority scores.",
    "hits_hub": "Mean of amount-weighted and count-weighted HITS hub scores.",
    "betweenness": "Normalized directed shortest-path betweenness with unweighted edge lengths.",
    "weighted_betweenness": "Normalized directed betweenness using edge distance inverse to log1p(amount/5000); not direct money-flow tracing.",
    "brokerage": "Mean of unweighted and amount-distance directed betweenness; an observed structural connector signal.",
    "participation": "One minus sum of squared incident-amount shares across counterparties' clusters, combining incoming and outgoing amounts.",
    "fragmentation_impact": "After removing this node, count outside the remaining largest piece of its original undirected component, excluding the removed node.",
    "cluster_stability": "Mean Jaccard similarity between this node's medoid cluster membership and its memberships across clustering runs.",
    "observability": "Heuristic role-confidence multiplier reflecting censoring/isolation; not observed fraction of true transactions.",
    "role_stability": "Fraction of score-perturbation runs retaining this role with fixed eligibility gates; not transaction-resampling or priority-rank stability.",
    "priority_raw": "Weighted sum of six priority components before the final ECDF rank; not the published priority_score.",
}
PRIORITY_COMPONENT_DEFINITIONS = {
    "connectivity": "0.40*rank(brokerage)+0.30*rank(fragmentation_impact)+0.30*rank(participation). Structural connectivity signal.",
    "exposure": "0.35*rank(in_kzt+out_kzt)+0.25*rank(in_tx+out_tx)+0.40*max(PageRank/HITS ranks). A composite exposure/influence score, not turnover alone.",
    "seed": "0.55*rank(seed_affinity)+0.30*rank(direct_seed_in)+0.15*rank(n_seed_reachable). Observed seed connectivity.",
    "temporal": "0.35*fifo_1d*rank(min(in_kzt,out_kzt))+0.25*max(daily fan-in/fan-out ranks)+0.20*max(repeated route/edge ranks)+0.20*max(reciprocal/temporal-cycle ranks). Temporal/motif signal.",
    "role": "Maximum raw non-peripheral role profile BEFORE eligibility filtering. May differ from selected role profile; NOT role confidence.",
    "uncertainty": "(1-role_score)*priority.exposure. Additional value of reviewing an uncertain role with exposure; low value can result from low exposure OR higher role confidence. NOT priority/rank stability.",
}
METRICS = frozenset((
    "depth", "is_seed", "truncated_by_depth", "p_continue", "in_deg", "out_deg",
    "in_tx", "out_tx", "in_kzt", "out_kzt", "pass_through", "retention",
    "balance_similarity", "in_flow_share", "out_flow_share", "fifo_0d", "fifo_1d",
    "max_in_sources_day", "max_out_targets_day", "repeat_route_count",
    "reciprocal_neighbors", "temporal_cycle_count", "direct_seed_in", "seed_affinity",
    "pagerank_amount", "pagerank_count", "hits_authority", "hits_hub", "brokerage",
    "betweenness", "participation", "cluster_stability", "observability", "role_stability",
    "p_brokerage", "p_seed_affinity", "p_participation", "p_pagerank_amount",
    "p_pagerank_count", "p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle",
    "priority_raw", "weighted_betweenness", "fragmentation_impact", "cycle_count",
    "n_seed_reachable", "active_in_days", "active_out_days", "median_fifo_lag",
))
CONDITIONS = frozenset((
    "is_seed", "in_deg", "out_deg", "in_flow_share", "out_flow_share", "in_kzt",
    "max(fifo_1d,balance_similarity)", "depth<4 OR (censored AND p_continue<=threshold)",
    "signals >= rank gate", "max(p_brokerage,p_participation)", "score_coordinator",
))
LABELS = {
    "in_deg": "входы", "out_deg": "выходы", "in_kzt": "вход", "out_kzt": "выход",
    "in_tx": "вход. переводы", "out_tx": "исход. переводы", "fifo_0d": "FIFO день",
    "fifo_1d": "FIFO след. день", "role_score": "уверенность", "priority_score": "приоритет",
    "depth": "глубина", "p_continue": "продолжение", "direct_seed_in": "прямые seed",
    "connectivity": "связность", "exposure": "экспозиция", "seed": "seed-связи",
    "temporal": "время", "role": "ролевой профиль", "uncertainty": "неопределённость",
}
BASE_CAVEAT = "Гипотеза для проверки аналитиком, не вывод о виновности. Приоритет не равен уверенности роли."
SAMPLING_CAVEAT = "Исходящий обход и порог 5 000 KZT скрывают часть потоков; наблюдаемый баланс не является полным."
DEPTH_CAVEAT = "Depth-4: исходящие потоки цензурированы; нулевой наблюдаемый выход не доказывает terminal."
SEED_CAVEAT = "Для seed внешние входящие потоки могут отсутствовать в выборке."
DAILY_CAVEAT = "Дневные даты не определяют порядок внутри дня; FIFO не доказывает движение тех же денег."
FLOW_SHARE_CAVEAT = "in_flow_share и out_flow_share — доли во входящем плюс исходящем обороте самого узла, не доли всей сети."
PRIORITY_SEMANTICS_CAVEAT = "Компонент uncertainty=(1-role_score)×exposure не измеряет стабильность приоритета; низкое значение может отражать низкую экспозицию или более высокую уверенность роли."


def _number(value: Any) -> float | int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value if math.isfinite(value) else None


def _unit(key: str) -> str:
    if key.endswith("_kzt"):
        return "KZT"
    if key in {"in_deg", "out_deg", "in_tx", "out_tx", "depth", "max_in_sources_day", "max_out_targets_day", "repeat_route_count", "reciprocal_neighbors", "temporal_cycle_count", "direct_seed_in", "cycle_count", "n_seed_reachable", "active_in_days", "active_out_days"}:
        return "count"
    if key == "median_fifo_lag":
        return "days"
    return "score_or_ratio"


def _trace(raw: Any, assigned_role: str) -> dict:
    if not isinstance(raw, dict) or raw.get("available") is not True:
        return {"available": False}
    roles = {}
    for role in ROLES:
        original = raw.get("roles", {}).get(role, {})
        if not isinstance(original, dict) or not isinstance(original.get("eligible"), bool):
            raise ValueError("INVALID_DECISION_TRACE")
        profile = _number(original.get("profile_score"))
        if profile is None:
            raise ValueError("INVALID_DECISION_TRACE")
        checks = []
        for item in original.get("conditions", []):
            if not isinstance(item, dict) or item.get("condition") not in CONDITIONS or item.get("operator") not in {"=", ">", ">=", "rule"} or not isinstance(item.get("passed"), bool):
                raise ValueError("INVALID_DECISION_TRACE")
            observed = item.get("observed")
            if isinstance(observed, dict):
                observed = {key: value for key, value in observed.items() if key in {"depth", "p_continue", "censored"} and (isinstance(value, bool) or _number(value) is not None)}
            elif not isinstance(observed, bool) and _number(observed) is None:
                raise ValueError("INVALID_DECISION_TRACE")
            threshold = item.get("threshold")
            if not isinstance(threshold, bool) and _number(threshold) is None:
                raise ValueError("INVALID_DECISION_TRACE")
            checks.append({"condition": item["condition"], "observed": observed, "operator": item["operator"], "threshold": threshold, "passed": item["passed"]})
        if original["eligible"] != all(item["passed"] for item in checks):
            raise ValueError("INVALID_DECISION_TRACE")
        roles[role] = {"eligible": original["eligible"], "profile_score": profile, "conditions": checks}
    order = raw.get("role_order", list(ROLES))
    if not isinstance(order, list) or len(order) != len(ROLES) or set(order) != set(ROLES):
        raise ValueError("INVALID_DECISION_TRACE")
    eligible = [role for role in order if roles[role]["eligible"]]
    if not eligible or max(eligible, key=lambda role: roles[role]["profile_score"]) != assigned_role:
        raise ValueError("ROLE_DECISION_MISMATCH")
    return {"available": True, "selection_rule": "maximum profile_score among eligible roles; ties follow saved role order", "role_order": order, "roles": roles, "role_score_formula": ROLE_SCORE_FORMULA}


def prepare_payload(payload: dict) -> dict:
    """Allowlist graph facts; never forward identifiers or saved free-text fields."""
    if not isinstance(payload, dict) or payload.get("assigned_role") not in ROLES:
        raise ValueError("INVALID_ROLE")
    role = payload["assigned_role"]
    metrics = {key: numeric for key, value in payload.get("metrics", {}).items() if key in METRICS and (numeric := _number(value)) is not None}
    role_score, priority_score = _number(payload.get("role_score")), _number(payload.get("priority_score"))
    if role_score is None or priority_score is None:
        raise ValueError("MISSING_SCORES")
    components = {key: value for key in PRIORITY_WEIGHTS if (value := _number(payload.get("priority_components", {}).get(key))) is not None}
    if not components:
        raise ValueError("MISSING_PRIORITY_COMPONENTS")
    weights = payload.get("priority_weights", PRIORITY_WEIGHTS)
    if any(weights.get(key) != value for key, value in PRIORITY_WEIGHTS.items()) or payload.get("priority_formula", PRIORITY_FORMULA) != PRIORITY_FORMULA:
        raise ValueError("UNSUPPORTED_PRIORITY_FORMULA")
    sampling = {"max_depth": 4, "outgoing_only": True, "minimum_observed_transfer_kzt": 5000, "daily_dates_only": True}
    if isinstance(payload.get("sampling"), dict):
        for key in ("date_start", "date_end"):
            value = payload["sampling"].get(key)
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                sampling[key] = value
    trace = _trace(payload.get("decision_trace"), role)
    facts = {}

    def add(fact_id: str, key: str, value: Any, unit: str | None = None) -> None:
        facts[fact_id] = {"value": value, "unit": unit or _unit(key), "label": LABELS.get(key, key)}

    for key, value in metrics.items():
        add(f"metric.{key}", key, value)
    add("score.role_score", "role_score", role_score)
    add("score.priority_score", "priority_score", priority_score)
    for key, value in components.items():
        add(f"priority.{key}", key, value)
        add(f"priority_contribution.{key}", key, value * PRIORITY_WEIGHTS[key])
        facts[f"priority_contribution.{key}"]["label"] = f"вклад «{LABELS.get(key, key)}»"
    for candidate, definition in trace.get("roles", {}).items():
        add(f"role.{candidate}.profile_score", "profile_score", definition["profile_score"])
        facts[f"role.{candidate}.profile_score"]["label"] = f"профиль {candidate}"
        add(f"role.{candidate}.eligible", "eligible", definition["eligible"], "boolean")
        facts[f"role.{candidate}.eligible"]["label"] = f"допуск {candidate}"
        for index, check in enumerate(definition["conditions"]):
            prefix = f"gate.{candidate}.{index}"
            for field in ("observed", "threshold", "passed"):
                value = check[field]
                if isinstance(value, dict):
                    for subkey, subvalue in value.items():
                        add(f"{prefix}.{field}.{subkey}", subkey, subvalue, "boolean" if isinstance(subvalue, bool) else None)
                else:
                    add(f"{prefix}.{field}", check["condition"], value, "boolean" if isinstance(value, bool) else None)
                    field_label = {"observed": "наблюдается", "threshold": "порог", "passed": "условие выполнено"}[field]
                    facts[f"{prefix}.{field}"]["label"] = f"{LABELS.get(check['condition'], check['condition'])} ({field_label})"
    definitions = {key: METRIC_DEFINITIONS.get(key, "ECDF rank among strictly positive values of its underlying signal, with nonpositive values kept zero; not a probability.") for key in metrics}
    return {"schema_version": PROMPT_VERSION, "assigned_role": role, "secondary_role": payload.get("secondary_role") if payload.get("secondary_role") in ROLES else None, "role_score": role_score, "priority_score": priority_score, "metrics": metrics, "metric_definitions": definitions, "decision_trace": trace, "priority_components": components, "priority_component_definitions": PRIORITY_COMPONENT_DEFINITIONS.copy(), "priority_weights": PRIORITY_WEIGHTS.copy(), "priority_formula": PRIORITY_FORMULA, "sampling": sampling, "fact_catalog": facts}


def _output_schema(prepared: dict):
    # Deliberately lazy: the ordinary local pipeline has no SDK dependency.
    from pydantic import BaseModel, ConfigDict, Field
    from typing import Literal

    FactId = Literal[tuple(sorted(prepared["fact_catalog"]))]
    AssignedRole = Literal[prepared["assigned_role"]]

    class EvidenceItem(BaseModel):
        model_config = ConfigDict(extra="forbid")
        fact_ids: list[FactId] = Field(min_length=1, max_length=5)
        interpretation: str
        kind: Literal["support", "counterevidence", "priority"]

    class Explanation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        assigned_role: AssignedRole
        role_summary: str
        priority_summary: str
        explanation: str = Field(description="Russian human-readable evidence paragraph, 60–120 words, numerical facts as {{fact_id}} placeholders; exported to evidence.")
        priority_explanation: str = Field(description="Russian human-readable why paragraph, 60–120 words, numerical facts as {{fact_id}} placeholders; exported to why.")
        alternative_explanation: str
        evidence_items: list[EvidenceItem] = Field(min_length=2, max_length=6)
        limitations: list[str] = Field(min_length=1, max_length=6)
        analyst_next_step: str

    EvidenceItem.model_rebuild(_types_namespace={"Literal": Literal, "FactId": FactId})
    Explanation.model_rebuild(_types_namespace={"EvidenceItem": EvidenceItem, "AssignedRole": AssignedRole})
    return Explanation


def _invalid_prose(field: str, rule: str, length: int | None = None) -> ValueError:
    error = ValueError("INVALID_STRUCTURED_EXPLANATION")
    error.diagnostic = {"field": field, "rule": rule}
    if length is not None:
        error.diagnostic["length"] = length
    return error


def _prose(value: Any, minimum: int, maximum: int, field: str = "prose") -> str:
    if not isinstance(value, str):
        raise _invalid_prose(field, "string_required")
    clean = " ".join(value.split())
    if not minimum <= len(clean) <= maximum:
        raise _invalid_prose(field, "length_out_of_bounds", len(clean))
    # A schema-known metric token is not an authored numeric quantity. Unknown
    # tokens and numeric values remain forbidden; do not weaken fact grounding.
    digit_checked = re.sub(r"\b(?:fifo_0d|fifo_1d)\b", "known_metric", clean)
    if re.search(r"\d", digit_checked):
        raise _invalid_prose(field, "numeric_literal_forbidden")
    return clean


def _format_value(fact: dict) -> str:
    value = fact["value"]
    if isinstance(value, bool):
        number = "да" if value else "нет"
    elif fact["unit"] == "KZT":
        number = f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    elif fact["unit"] == "count" and float(value).is_integer():
        number = f"{value:,.0f}".replace(",", " ")
    else:
        # Fixed decimal notation keeps tiny centrality values nonzero and makes
        # amounts/counts readable; evidence_json retains the original precision.
        number = format(Decimal(f"{value:.6g}"), "f")
    unit = " KZT" if fact["unit"] == "KZT" else (" дн." if fact["unit"] == "days" else "")
    return f"{number}{unit}"


def _format_fact(fact: dict) -> str:
    return f"{fact['label']}={_format_value(fact)}"


def narrative_word_count(value: str) -> int:
    # Count final whitespace-separated words, including grouped numeric values.
    # This conservative rule is shared with cache/export acceptance.
    return len(value.split())


def _narrative(value: Any, prepared: dict, field: str) -> str:
    """Render API-authored sentences, never invent values or truncate prose."""
    if not isinstance(value, str):
        raise _invalid_prose(field, "string_required")
    clean = " ".join(value.split())
    refs = set(FACT_PLACEHOLDER.findall(clean))
    catalog = prepared["fact_catalog"]
    if not refs <= catalog.keys():
        raise _invalid_prose(field, "unknown_fact_reference")
    unnumbered = FACT_PLACEHOLDER.sub("значение", clean)
    if "{" in unnumbered or "}" in unnumbered:
        raise _invalid_prose(field, "invalid_fact_placeholder")
    _prose(unnumbered, 40, 12000, field)
    if field == "explanation":
        metric_refs = {ref for ref in refs if ref.startswith("metric.") and _number(catalog[ref]["value"]) is not None}
        if len(metric_refs) < 2:
            raise _invalid_prose(field, "insufficient_fact_references")
        caveat = "Это гипотеза о роли для проверки аналитиком, а не вывод о виновности. Выборка не отражает все денежные потоки."
        if prepared["metrics"].get("depth") == 4 or prepared["metrics"].get("truncated_by_depth"):
            caveat += " На границе обхода отсутствие исходящих переводов не доказывает прекращение движения средств."
    else:
        if not any(ref.startswith(("priority.", "priority_contribution.")) for ref in refs):
            raise _invalid_prose(field, "insufficient_fact_references")
        caveat = "Приоритет показывает очередность аналитической проверки, а не вероятность виновности или уверенность в роли. Ненаблюдаемые потоки могут изменить вывод."
    rendered = FACT_PLACEHOLDER.sub(lambda match: _format_value(catalog[match[1]]), clean)
    rendered = f"{rendered} {caveat}"
    words = narrative_word_count(rendered)
    if words > MAX_NARRATIVE_WORDS:
        raise _invalid_prose(field, "word_limit_exceeded", words)
    return rendered


def validate_structured_output(prepared: dict, output: Any, usage: dict | None = None) -> dict:
    """Reject invalid references; hydrate fact values solely from the local catalog."""
    if hasattr(output, "model_dump"):
        output = output.model_dump()
    expected = {"assigned_role", "role_summary", "priority_summary", "explanation", "priority_explanation", "alternative_explanation", "evidence_items", "limitations", "analyst_next_step"}
    if not isinstance(output, dict) or set(output) != expected or output.get("assigned_role") != prepared["assigned_role"]:
        raise ValueError("INVALID_STRUCTURED_EXPLANATION")
    prose = {key: _prose(output.get(key), low, high, key) for key, low, high in (
        ("role_summary", 12, 160), ("priority_summary", 12, 160),
        ("alternative_explanation", 30, 1400), ("analyst_next_step", 20, 700),
    )}
    items = output.get("evidence_items")
    if not isinstance(items, list) or not 2 <= len(items) <= 6:
        raise ValueError("INVALID_EVIDENCE_ITEMS")
    catalog = prepared["fact_catalog"]
    evidence, referenced, kinds = [], set(), set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"fact_ids", "interpretation", "kind"}:
            raise ValueError("INVALID_EVIDENCE_ITEMS")
        refs, kind = item["fact_ids"], item["kind"]
        if not isinstance(refs, list):
            error = ValueError("INVALID_FACT_REFERENCE")
            error.diagnostic = {"field": "evidence_items.fact_ids", "rule": "list_required"}
            raise error
        if any(not isinstance(ref, str) or ref not in catalog for ref in refs):
            error = ValueError("INVALID_FACT_REFERENCE")
            error.diagnostic = {"field": "evidence_items.fact_ids", "rule": "unknown_fact_reference"}
            raise error
        refs = list(dict.fromkeys(refs))
        if not 1 <= len(refs) <= 5:
            error = ValueError("INVALID_FACT_REFERENCE")
            error.diagnostic = {"field": "evidence_items.fact_ids", "rule": "length_out_of_bounds", "length": len(refs)}
            raise error
        if kind not in {"support", "counterevidence", "priority"}:
            raise ValueError("INVALID_EVIDENCE_ITEMS")
        interpretation = _prose(item["interpretation"], 20, 700, "evidence_items.interpretation")
        evidence.append({"kind": kind, "facts": [{"fact_id": ref, **catalog[ref]} for ref in refs], "interpretation": interpretation})
        referenced.update(refs)
        kinds.add(kind)
    numeric_metrics = {ref for ref in referenced if ref.startswith("metric.") and _number(catalog[ref]["value"]) is not None}
    if len(numeric_metrics) < 2 or not {"support", "priority"}.issubset(kinds) or not any(ref.startswith(("priority.", "priority_contribution.")) for ref in referenced):
        raise ValueError("INSUFFICIENT_GROUNDED_EVIDENCE")
    limitations = output.get("limitations")
    if not isinstance(limitations, list) or not 1 <= len(limitations) <= 6:
        raise ValueError("INVALID_LIMITATIONS")
    limitations = [_prose(item, 20, 700, "limitations") for item in limitations]
    required = [BASE_CAVEAT, SAMPLING_CAVEAT, DAILY_CAVEAT]
    if "in_flow_share" in prepared["metrics"] or "out_flow_share" in prepared["metrics"]:
        required.append(FLOW_SHARE_CAVEAT)
    required.append(PRIORITY_SEMANTICS_CAVEAT)
    if prepared["metrics"].get("depth") == 4 or prepared["metrics"].get("truncated_by_depth"):
        required.append(DEPTH_CAVEAT)
    if prepared["metrics"].get("is_seed"):
        required.append(SEED_CAVEAT)
    if not prepared["decision_trace"].get("available"):
        required.append("Полная трасса правил недоступна; причины допуска ролей не верифицированы.")
    for caveat in required:
        if caveat not in limitations:
            limitations.append(caveat)
    support = [fact for item in evidence if item["kind"] != "priority" for fact in item["facts"] if _number(fact["value"]) is not None]
    priority = [fact for item in evidence if item["kind"] == "priority" for fact in item["facts"] if _number(fact["value"]) is not None]
    if not support or not priority:
        raise ValueError("INSUFFICIENT_GROUNDED_EVIDENCE")
    for field in ("explanation", "priority_explanation"):
        prose[field] = _narrative(output.get(field), prepared, field)
        # Keep an audit of all inline references, even if the model did not
        # duplicate a catalog-known reference in its evidence_items array.
        inline_refs = list(dict.fromkeys(FACT_PLACEHOLDER.findall(output[field])))
        evidence.append({"kind": "support" if field == "explanation" else "priority",
                         "source": field, "facts": [{"fact_id": ref, **catalog[ref]} for ref in inline_refs],
                         "interpretation": prose[field]})
    role_block = "\n".join(f"{'; '.join(_format_fact(fact) for fact in item['facts'])}: {item['interpretation']}" for item in evidence if item["kind"] != "priority")
    priority_block = "\n".join(f"{'; '.join(_format_fact(fact) for fact in item['facts'])}: {item['interpretation']}" for item in evidence if item["kind"] == "priority")
    score_line = f"Роль: {prepared['assigned_role']}; role_score={prepared['role_score']:.5g}."
    priority_line = f"priority_score={prepared['priority_score']:.5g}; {PRIORITY_FORMULA}."
    usage = {key: int(value) for key in ("input_tokens", "output_tokens", "total_tokens") if isinstance((value := (usage or {}).get(key)), int) and not isinstance(value, bool) and value >= 0}
    structured = {"assigned_role": prepared["assigned_role"], **prose, "evidence_items": evidence, "limitations": limitations, "validation": {"known_fact_references": True, "numeric_values_server_owned": True, "role_unchanged": True, "semantic_truth_verified": False}}
    return {"evidence": prose["explanation"], "why": prose["priority_explanation"], "explanation": f"{score_line}\n{prose['explanation']}\n\n{role_block}\n\n{' '.join(required)}", "priority_explanation": f"{priority_line}\n{prose['priority_explanation']}\n\n{priority_block}\n\n{PRIORITY_SEMANTICS_CAVEAT} {BASE_CAVEAT}", "alternative_explanation": prose["alternative_explanation"], "evidence_json": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), "limitations": " ".join(limitations), "analyst_next_step": prose["analyst_next_step"], "structured": structured, "usage": usage}


async def _run_agent(prepared: dict, settings: AISettings) -> tuple[Any, dict]:
    from agents import Agent, Runner, RunConfig, ModelSettings, OpenAIResponsesModel
    from openai import AsyncOpenAI

    async with AsyncOpenAI(api_key=settings.api_key, base_url="https://api.openai.com/v1", timeout=settings.timeout_seconds, max_retries=0) as client:
        agent = Agent(name="HackAlem Structured Evidence Writer", instructions=PROMPT_PATH.read_text(encoding="utf-8"), model=OpenAIResponsesModel(model=settings.model, openai_client=client), model_settings=ModelSettings(store=False, max_tokens=3200), tools=[], output_type=_output_schema(prepared))
        result = await asyncio.wait_for(Runner.run(agent, json.dumps(prepared, ensure_ascii=False, separators=(",", ":")), run_config=RunConfig(tracing_disabled=True), max_turns=1), timeout=settings.timeout_seconds)
    actual_usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    usage = {key: getattr(actual_usage, key) for key in ("input_tokens", "output_tokens", "total_tokens") if actual_usage is not None and hasattr(actual_usage, key)}
    return result.final_output, usage


async def generate_structured_explanation(payload: dict, settings: AISettings) -> dict:
    """Generate one explanation; provider exceptions are sanitized by the caller."""
    if not settings.configured:
        raise RuntimeError("API_KEY_NOT_CONFIGURED")
    if not SDK_AVAILABLE:
        raise RuntimeError("SDK_UNAVAILABLE")
    prepared = prepare_payload(payload)
    output, usage = await _run_agent(prepared, settings)
    try:
        return validate_structured_output(prepared, output, usage)
    except ValueError as error:
        # A rejected response may still use paid tokens; report known usage even
        # when no explanation is accepted. Never attach raw provider text.
        error.usage = {key: value for key in ("input_tokens", "output_tokens", "total_tokens") if isinstance((value := usage.get(key)), int) and not isinstance(value, bool) and value >= 0}
        raise
