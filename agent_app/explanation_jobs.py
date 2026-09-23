"""Explicit, resumable API narration jobs and immutable CSV export snapshots.

No API call happens on upload, status, export, or server startup. Roles, scores,
ranks, and baseline files are never changed by model output. GIDs stay local.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import csv
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from threading import Event, RLock, Thread
import time
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from .config import get_ai_settings
from .datasets import DatasetError, EXPORT_NAMES
from . import structured_explanations as provider


TEXT_FIELDS = ("evidence", "why", "explanation", "priority_explanation", "evidence_json",
               "alternative_explanation", "limitations", "analyst_next_step")
EXTRA_FIELDS = (*TEXT_FIELDS, "explanation_source", "explanation_model",
                "explanation_status", "explanation_error", "explanation_error_code")
FINAL_STATES = {"idle", "complete", "partial", "cancelled", "failed"}
BATCH_WORKERS = 3
MAX_CONSECUTIVE_FAILURES = 3
VALIDATION_CODES = frozenset({"INVALID_DECISION_TRACE", "ROLE_DECISION_MISMATCH", "INVALID_ROLE",
                             "MISSING_SCORES", "MISSING_PRIORITY_COMPONENTS", "UNSUPPORTED_PRIORITY_FORMULA",
                             "INVALID_STRUCTURED_EXPLANATION", "INVALID_EVIDENCE_ITEMS", "INVALID_FACT_REFERENCE",
                             "INSUFFICIENT_GROUNDED_EVIDENCE", "INVALID_LIMITATIONS"})
SAFE_EXCEPTION_TYPES = frozenset({"ModelBehaviorError", "AgentsException", "MaxTurnsExceeded", "UserError",
                                  "ValidationError", "TypeError", "ValueError", "RuntimeError",
                                  "APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionError",
                                  "AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError",
                                  "BadRequestError", "UnprocessableEntityError", "InternalServerError", "APIError"})
DIAGNOSTIC_FIELDS = frozenset({"role_summary", "priority_summary", "explanation", "priority_explanation",
                               "alternative_explanation", "analyst_next_step", "evidence_interpretation",
                               "evidence_items.interpretation", "limitations", "prose"})
DIAGNOSTIC_RULES = frozenset({"string_required", "length_out_of_bounds", "numeric_literal_forbidden",
                              "word_limit_exceeded", "unknown_fact_reference", "invalid_fact_placeholder",
                              "insufficient_fact_references"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(_json(value), encoding="utf-8")
    temporary.replace(path)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    # Do not infer numbers: IDs and all immutable numeric cells retain their exact
    # original string representation, including integers above JavaScript 2^53.
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def _csv_safe(value: str) -> str:
    # Apply only to generated prose, never IDs or numeric scoring fields.
    return "'" + value if value.lstrip(" \ufeff").startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def _result_valid(result: Any) -> bool:
    if not isinstance(result, dict) or any(not isinstance(result.get(name), str) or not result[name].strip() for name in TEXT_FIELDS):
        return False
    if any(provider.narrative_word_count(result[name]) > provider.MAX_NARRATIVE_WORDS
           or "{{" in result[name] or "}}" in result[name] for name in ("evidence", "why")):
        return False
    try:
        return isinstance(json.loads(result["evidence_json"]), (dict, list))
    except (ValueError, TypeError):
        return False


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(number) for key, number in value.items()
            if key in {"requests", "input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
            and isinstance(number, (int, float)) and not isinstance(number, bool) and 0 <= number < 1e12}


def _public_error(error: Exception) -> tuple[str, bool, bool]:
    """Return safe message, stop-job flag, and transient retry flag."""
    code = getattr(error, "status_code", None)
    messages = {401: "OpenAI: ключ не принят. Проверьте OPENAI_API_KEY в .env.",
                403: "OpenAI: доступ к API или модели запрещён.",
                404: "OpenAI: модель недоступна. Проверьте OPENAI_MODEL.",
                429: "OpenAI: исчерпан баланс или лимит запросов. Проверьте аккаунт перед продолжением."}
    if code in messages:
        return messages[code], True, False
    if code in {400, 422}:
        return "OpenAI: модель не приняла формат запроса. Проверьте совместимость модели.", True, False
    transient = code in {408, 500, 502, 503, 504} or type(error).__name__ in {"APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionError"}
    if transient:
        return "OpenAI: временная ошибка соединения или сервиса. Можно продолжить позже.", False, True
    return "Ответ API не прошёл проверку структуры или фактов. Объяснение не сохранено; можно повторить.", False, False


def _diagnostic_code(error: Exception) -> str:
    # Never return arbitrary provider exception text, including request bodies,
    # credentials, URLs, or an attacker-controlled exception class name.
    if isinstance(error, ValueError) and str(error) in VALIDATION_CODES:
        return str(error)
    name = type(error).__name__
    return name if name in SAFE_EXCEPTION_TYPES else "UNEXPECTED_ERROR"


def _safe_diagnostic(error: Exception) -> dict:
    """Expose structural validation coordinates, never rejected prose or values."""
    raw = getattr(error, "diagnostic", None)
    if not isinstance(error, ValueError) or not isinstance(raw, dict):
        return {}
    field, rule = raw.get("field"), raw.get("rule")
    if not isinstance(field, str) or field not in DIAGNOSTIC_FIELDS or not isinstance(rule, str) or rule not in DIAGNOSTIC_RULES:
        return {}
    safe = {"field": field, "rule": rule}
    length = raw.get("length")
    if isinstance(length, int) and not isinstance(length, bool) and 0 <= length <= 100_000:
        safe["length"] = length
    return safe


class NarrativeManager:
    """One explicit API job at a time; successful nodes are persisted and reused."""

    def __init__(self, datasets):
        self.datasets = datasets
        self.root = Path(datasets.root)
        self.directory = self.root / "runtime" / "narratives"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._active: str | None = None
        self._states: dict[str, dict] = {}
        self._results: dict[str, dict | None] = {}
        self._threads: dict[str, Thread] = {}

    def _dataset(self, run_id: str) -> dict:
        with self._lock:
            if run_id in self._states:
                # Keep DatasetManager as authority for run existence/readiness.
                self.datasets.context(run_id)
                return self._states[run_id]
            engine, explanations = self.datasets.context(run_id)
            paths = {name: self.datasets.asset(run_id, name) for name in EXPORT_NAMES}
            digest = sha256()
            for name, path in paths.items():
                digest.update(name.encode())
                digest.update(path.read_bytes())
            columns, rows = _read_csv(paths["nodes_roles.csv"])
            _, top = _read_csv(paths["top_nodes.csv"])
            ids = [row["gid"] for row in rows]
            if len(ids) != len(set(ids)):
                raise DatasetError("В nodes_roles.csv повторяются gid; сначала пересчитайте анализ.", 409)
            payloads = {}
            for gid in ids:
                raw = explanations.payload(gid)
                if raw is None:
                    raise DatasetError("Признаки для объяснения узла отсутствуют. Пересчитайте анализ.", 409)
                try:
                    payloads[gid] = provider.prepare_payload(raw)
                except (ValueError, TypeError, KeyError) as error:
                    raise DatasetError("Сохранённые признаки или правила не подходят для проверки объяснений. Пересчитайте анализ.", 409) from error
            # Features/thresholds are part of identity too, not only CSV rows.
            digest.update(_json(payloads).encode("utf-8"))
            directory = self.directory / digest.hexdigest()
            (directory / "cache").mkdir(parents=True, exist_ok=True)
            (directory / "exports").mkdir(exist_ok=True)
            top_ids = list(dict.fromkeys(row["gid"] for row in top if row["gid"] in payloads))
            state = {"run_id": run_id, "directory": directory, "paths": paths, "payloads": payloads,
                     "ids": ids, "top_ids": top_ids, "selected": ids.copy(), "scope": "all",
                     "status": "idle", "message": "Объяснения API ещё не запущены.", "model": "",
                     "errors": {}, "error_codes": {}, "usage": {}, "last_error_code": "",
                     "consecutive_failures": 0, "cancel": Event(), "stop": Event(),
                     "inflight": 0, "revision": 0, "snapshot": None, "keys": {}}
            manifest = directory / "job.json"
            if manifest.is_file():
                try:
                    saved = json.loads(manifest.read_text(encoding="utf-8"))
                    if not isinstance(saved, dict):
                        raise ValueError()
                    for key in ("model", "scope", "usage", "errors", "error_codes", "last_error_code"):
                        if isinstance(saved.get(key), type(state[key])):
                            state[key] = saved[key]
                    state["selected"] = [gid for gid in saved.get("selected", ids) if gid in payloads]
                    state["status"] = saved.get("status") if saved.get("status") in FINAL_STATES else "partial"
                    state["message"] = "Сохранённый прогресс восстановлен. Продолжение запускается только по кнопке."
                except (OSError, ValueError, TypeError):
                    pass
            self._states[run_id] = state
            return state

    def _key(self, state: dict, gid: str, model: str) -> str:
        # Prepared payloads are immutable within this dataset snapshot. Polling
        # must not serialize thousands of fact catalogs again every two seconds.
        identity = (gid, model, provider.PROMPT_VERSION)
        if identity not in state["keys"]:
            state["keys"][identity] = sha256(_json({"payload": state["payloads"][gid], "model": model,
                                                   "prompt_version": provider.PROMPT_VERSION}).encode("utf-8")).hexdigest()
        return state["keys"][identity]

    def _cached(self, state: dict, gid: str, model: str) -> dict | None:
        key = self._key(state, gid, model)
        if key not in self._results:
            record = None
            path = state["directory"] / "cache" / f"{key}.json"
            try:
                if path.is_file() and path.stat().st_size < 256 * 1024:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                    if (candidate.get("key") == key and candidate.get("model") == model
                            and candidate.get("prompt_version") == provider.PROMPT_VERSION
                            and _result_valid(candidate.get("result"))):
                        record = candidate
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            self._results[key] = record
        return self._results[key]

    def _save(self, state: dict) -> None:
        _atomic_json(state["directory"] / "job.json", {key: state[key] for key in
                     ("selected", "scope", "status", "message", "model", "errors", "error_codes",
                      "last_error_code", "usage")})

    def _model(self, state: dict) -> str:
        return state["model"] if state["status"] == "running" else get_ai_settings(self.root).model

    def _view(self, state: dict) -> dict:
        model = self._model(state)
        selected = state["selected"]
        completed = sum(self._cached(state, gid, model) is not None for gid in selected)
        failed = sum(self._key(state, gid, model) in state["errors"] and self._cached(state, gid, model) is None for gid in selected)
        total = len(selected)
        generated = sum(self._cached(state, gid, model) is not None for gid in state["ids"])
        status = state["status"]
        if status != "running" and total and completed == total:
            status = "complete"
        elif status == "complete" and completed < total:
            status = "partial"
        settings = get_ai_settings(self.root)
        return {"run_id": state["run_id"], "status": status, "total": total,
                "completed": completed, "failed": failed, "pending": total - completed - failed,
                "available_nodes": len(state["ids"]), "generated_count": generated,
                "scope": state["scope"], "model": model, "message": state["message"],
                "usage": dict(state["usage"]), "can_resume": status != "running" and completed < total,
                "usage_kind": "recorded_usage_not_billing_estimate",
                "usage_note": "requests — попытки вызова; токены — только сообщённое API использование, включая отклонённые ответы. Это не счёт за API.",
                "last_error_code": state["last_error_code"],
                "configured": settings.configured, "sdk_available": provider.SDK_AVAILABLE,
                "ready": settings.configured and provider.SDK_AVAILABLE,
                "cancel_requested": state["cancel"].is_set(), "active_run": self._active,
                "workers": BATCH_WORKERS, "inflight": state["inflight"]}

    def status(self, run_id: str = "default") -> dict:
        with self._lock:
            return self._view(self._dataset(run_id))

    def _settings(self):
        settings = get_ai_settings(self.root)
        if not settings.configured:
            raise DatasetError("Для генерации нужен OPENAI_API_KEY в .env. Локальное объяснение не подменяет ответ API.", 503)
        if not provider.SDK_AVAILABLE:
            raise DatasetError("OpenAI SDK недоступен. Установите requirements-ai.txt и перезапустите сервер.", 503)
        return settings

    def start(self, run_id: str, scope: str = "all", limit: int | None = None) -> dict:
        if scope not in {"all", "top"}:
            raise DatasetError("scope должен быть all или top.")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
            raise DatasetError("limit должен быть положительным целым числом.")
        with self._lock:
            state = self._dataset(run_id)
            if self._active is not None:
                raise DatasetError("Другой запрос API уже выполняется. Дождитесь завершения или остановите его.", 409)
            settings = self._settings()
            selected = state["top_ids"].copy()
            if scope == "all":
                selected += [gid for gid in state["ids"] if gid not in set(selected)]
            if limit is not None:
                selected = selected[:limit]
            state.update(selected=selected, scope=scope, model=settings.model, status="running",
                         message="Генерация через OpenAI; сначала приоритетные узлы. Это отдельный этап, он может занять больше 5 минут.")
            state["cancel"].clear()
            state["stop"].clear()
            state["consecutive_failures"] = 0
            state["last_error_code"] = ""
            for gid in selected:
                state["errors"].pop(self._key(state, gid, settings.model), None)
                state["error_codes"].pop(self._key(state, gid, settings.model), None)
            state["revision"] += 1
            self._active = run_id
            self._save(state)
            thread = Thread(target=self._work, args=(state, settings), name="node-api-explanations", daemon=True)
            self._threads[run_id] = thread
            result = self._view(state)
            try:
                thread.start()
            except Exception:
                self._active = None
                state.update(status="failed", message="Не удалось запустить генерацию; повторите позже.")
                self._save(state)
                raise DatasetError(state["message"], 503)
            return result

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            state = self._dataset(run_id)
            if state["status"] == "running":
                state["cancel"].set()
                state["message"] = "Остановка после текущего запроса API. Готовые объяснения сохранены."
                self._save(state)
            return self._view(state)

    def _generate(self, state: dict, gid: str, settings) -> tuple[dict | None, bool]:
        key = self._key(state, gid, settings.model)
        with self._lock:
            cached = self._cached(state, gid, settings.model)
            if cached is not None:
                return cached["result"], False
        for attempt in range(2):
            usage_recorded = False
            try:
                with self._lock:
                    # Count attempts independently of provider-reported usage.
                    # A timeout can be billed remotely without usage reaching us.
                    state["usage"]["requests"] = state["usage"].get("requests", 0) + 1
                    self._save(state)
                result = asyncio.run(provider.generate_structured_explanation(state["payloads"][gid], settings))
                with self._lock:
                    self._record_usage(state, result.get("usage") if isinstance(result, dict) else None)
                    usage_recorded = True
                if not _result_valid(result):
                    raise ValueError("INVALID_STRUCTURED_EXPLANATION")
                result = {**{name: result[name] for name in TEXT_FIELDS}, "usage": _usage(result.get("usage"))}
                record = {"key": key, "model": settings.model, "prompt_version": provider.PROMPT_VERSION,
                          "result": result}
                with self._lock:
                    _atomic_json(state["directory"] / "cache" / f"{key}.json", record)
                    self._results[key] = record
                    state["errors"].pop(key, None)
                    state["error_codes"].pop(key, None)
                    state["consecutive_failures"] = 0
                    state["revision"] += 1
                    self._save(state)
                return result, False
            except Exception as error:
                message, stop, transient = _public_error(error)
                diagnostic = _diagnostic_code(error)
                message += f" Код: {diagnostic}."
                detail = _safe_diagnostic(error)
                if detail:
                    message += f" Поле: {detail['field']}; правило: {detail['rule']}"
                    if "length" in detail:
                        message += f"; длина: {detail['length']}"
                    message += "."
                with self._lock:
                    if not usage_recorded:
                        self._record_usage(state, getattr(error, "usage", None))
                    state["last_error_code"] = diagnostic
                    self._save(state)
                if transient and attempt == 0 and not state["cancel"].is_set() and not state["stop"].is_set():
                    time.sleep(0.5)
                    continue
                with self._lock:
                    state["errors"][key] = message
                    state["error_codes"][key] = diagnostic
                    if not state["stop"].is_set():
                        state["message"] = message
                    if not stop and not state["stop"].is_set():
                        # Count an exhausted transient request too: a disconnected
                        # server must not attempt every node twice indefinitely.
                        state["consecutive_failures"] += 1
                        if state["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                            stop = True
                            state["message"] = message + " Пакет остановлен после трёх последовательных неудач, чтобы не расходовать API дальше."
                    if stop:
                        state["stop"].set()
                    state["revision"] += 1
                    self._save(state)
                return None, stop
        return None, False

    @staticmethod
    def _record_usage(state: dict, usage: Any) -> None:
        for name, value in _usage(usage).items():
            if name != "requests":
                state["usage"][name] = state["usage"].get(name, 0) + value

    def _work(self, state: dict, settings) -> None:
        stopped = False
        try:
            remaining = iter(state["selected"])
            exhausted = False
            # Only three requests may be in flight; nothing is prequeued. A stop
            # or systemic provider failure prevents any further submissions.
            with ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix="narrative-api") as pool:
                futures = {}
                seen = set()
                while True:
                    while not exhausted and len(futures) < BATCH_WORKERS and not state["cancel"].is_set() and not state["stop"].is_set():
                        if state["consecutive_failures"] and futures:
                            # Drain the current wave after an exhausted failure;
                            # three bad initial responses never fan out to 2248.
                            break
                        try:
                            gid = next(remaining)
                        except StopIteration:
                            exhausted = True
                            break
                        with self._lock:
                            key = self._key(state, gid, settings.model)
                            if key in seen or self._cached(state, gid, settings.model) is not None:
                                continue
                            seen.add(key)
                            futures[pool.submit(self._generate, state, gid, settings)] = gid
                            state["inflight"] = len(futures)
                    if not futures:
                        break
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        _, stop = future.result()
                        stopped = stopped or stop
                        del futures[future]
                    with self._lock:
                        state["inflight"] = len(futures)
            with self._lock:
                done = sum(self._cached(state, gid, settings.model) is not None for gid in state["selected"])
                if done == len(state["selected"]):
                    state.update(status="complete", message="Объяснения выбранных узлов готовы и включены в экспорт CSV.")
                elif state["cancel"].is_set():
                    state.update(status="cancelled", message="Генерация остановлена. Готовые узлы сохранены; можно продолжить.")
                else:
                    state["status"] = "partial" if done else "failed"
                    if not stopped:
                        state["message"] += " Готовые узлы сохранены; можно повторить оставшиеся."
        except Exception:
            with self._lock:
                state.update(status="failed", message="Генерация прервана. Сохранённые объяснения доступны; можно продолжить.")
        finally:
            with self._lock:
                state["inflight"] = 0
                self._active = None
                self._save(state)

    def explain_node(self, run_id: str, gid: str) -> tuple[dict, int]:
        with self._lock:
            state = self._dataset(run_id)
            if gid not in state["payloads"]:
                return {"status": "not_found", "message": "Узел не найден."}, 404
            model = self._model(state)
            cached = self._cached(state, gid, model)
            if cached:
                return self._node_reply(state, gid, cached["result"], model, cached=True), 200
            if self._active is not None:
                return {"status": "busy", "message": "Генерация API уже выполняется. Дождитесь завершения."}, 409
            settings = self._settings()
            state["stop"].clear()
            state["cancel"].clear()
            state["consecutive_failures"] = 0
            state["inflight"] = 1
            self._active = run_id
        try:
            result, _ = self._generate(state, gid, settings)
            if result is None:
                with self._lock:
                    message = state["errors"][self._key(state, gid, settings.model)]
                return {"status": "error", "api_error": True, "mode": "openai", "message": message,
                        "error_code": state["error_codes"].get(self._key(state, gid, settings.model), "UNEXPECTED_ERROR")}, 502
            return self._node_reply(state, gid, result, settings.model, cached=False), 200
        finally:
            with self._lock:
                state["inflight"] = 0
                self._active = None

    @staticmethod
    def _node_reply(state: dict, gid: str, result: dict, model: str, *, cached: bool) -> dict:
        payload = state["payloads"][gid]
        return {**result, "status": "ok", "gid": gid, "role": payload.get("assigned_role"),
                "role_score": payload.get("role_score"), "mode": "openai", "model": model,
                "cached": cached, "explanation_source": "openai"}

    def _snapshot(self, state: dict) -> Path:
        model = self._model(state)
        signature = (model, state["revision"], provider.PROMPT_VERSION)
        if state.get("snapshot_signature") == signature and state.get("snapshot") is not None:
            return state["snapshot"]
        destination = state["directory"] / "exports" / uuid4().hex
        destination.mkdir()
        for filename in EXPORT_NAMES:
            source = state["paths"][filename]
            if filename == "clusters.csv":
                (destination / filename).write_bytes(source.read_bytes())
                continue
            columns, rows = _read_csv(source)
            if "evidence" in columns and "evidence_rule" not in columns:
                columns.append("evidence_rule")
            if "why" in columns and "why_rule" not in columns:
                columns.append("why_rule")
            columns += [name for name in EXTRA_FIELDS if name not in columns]
            for row in rows:
                gid = row["gid"]
                if "evidence" in row:
                    row["evidence_rule"] = row["evidence"]
                if "why" in row:
                    row["why_rule"] = row["why"]
                cached = self._cached(state, gid, model)
                error = state["errors"].get(self._key(state, gid, model))
                if cached:
                    for name in TEXT_FIELDS:
                        row[name] = _csv_safe(cached["result"][name])
                    row.update(explanation_source="openai", explanation_model=model,
                               explanation_status="complete", explanation_error="", explanation_error_code="")
                else:
                    for name in TEXT_FIELDS:
                        row.setdefault(name, "")
                    row.update(explanation_source="error" if error else "not_generated", explanation_model=model,
                               explanation_status="error" if error else "not_generated", explanation_error=error or "",
                               explanation_error_code=state["error_codes"].get(self._key(state, gid, model), ""))
            with (destination / filename).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
        _atomic_json(state["directory"] / "published.json", {"snapshot": destination.name, "model": model,
                                                              "revision": state["revision"]})
        state["snapshot"] = destination
        state["snapshot_signature"] = signature
        return destination

    def export_file(self, run_id: str, filename: str) -> Path:
        if filename not in EXPORT_NAMES:
            raise DatasetError("Файл не найден.", 404)
        with self._lock:
            return self._snapshot(self._dataset(run_id)) / filename

    def archive(self, run_id: str) -> bytes:
        with self._lock:
            snapshot = self._snapshot(self._dataset(run_id))
            buffer = BytesIO()
            with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
                for filename in EXPORT_NAMES:
                    archive.write(snapshot / filename, arcname=filename)
            return buffer.getvalue()
