"""Local, immutable dataset runs behind the upload and download interface.

Uploads never replace the supplied example. Each page is pinned to a random run
identifier, and a run is published only after analysis, validation and rendering
all succeed. Neither this module nor its worker calls an external AI service.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from threading import RLock, Thread
import time
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from .engine import InvestigationEngine
from .explanations import RoleExplanationService
from .store import GraphDataStore


EXPORT_NAMES = ("nodes_roles.csv", "clusters.csv", "top_nodes.csv")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
RUN_TIMEOUT_SECONDS = 300
TABLE_COLUMNS = {
    "nodes": {"gid", "depth", "is_seed"},
    "edges": {"src", "dst", "sum_kzt", "n_tx", "depth"},
    "transactions": {"src", "dst", "date", "sum_kzt"},
}
ROW_LIMITS = {"nodes": 10_000, "edges": 30_000, "transactions": 100_000}
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


class DatasetError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preflight(files: dict[str, bytes]) -> dict[str, int]:
    """Bound metadata and decoded size before any dataframe is allocated."""
    if set(files) != set(TABLE_COLUMNS):
        raise DatasetError("Нужны ровно три файла: nodes, edges и transactions в формате Parquet.")
    if any(not isinstance(value, bytes) or not value for value in files.values()):
        raise DatasetError("Все три файла должны быть непустыми файлами Parquet.")
    if sum(map(len, files.values())) > MAX_TOTAL_BYTES or any(len(value) > MAX_FILE_BYTES for value in files.values()):
        raise DatasetError("Лимит загрузки: 20 МиБ на файл и 60 МиБ на набор.", 413)
    counts: dict[str, int] = {}
    total_decoded = 0
    for name, payload in files.items():
        if len(payload) < 12 or payload[:4] != b"PAR1" or payload[-4:] != b"PAR1":
            raise DatasetError(f"{name}: файл не является поддерживаемым Parquet.")
        footer_size = struct.unpack("<I", payload[-8:-4])[0]
        if not 0 < footer_size <= min(len(payload) - 12, 4 * 1024 * 1024):
            raise DatasetError(f"{name}: некорректные или слишком большие метаданные Parquet.")
        try:
            with pq.ParquetFile(pa.BufferReader(payload), thrift_string_size_limit=1_000_000,
                                thrift_container_size_limit=100_000) as parquet:
                metadata = parquet.metadata
                if metadata.num_columns > 32 or metadata.num_row_groups > 1024:
                    raise DatasetError(f"{name}: слишком много колонок или групп строк.")
                columns = parquet.schema_arrow.names
                if len(columns) != len(set(columns)):
                    raise DatasetError(f"{name}: имена колонок должны быть уникальными.")
                missing = TABLE_COLUMNS[name] - set(columns)
                if missing:
                    raise DatasetError(f"{name}: отсутствуют колонки {', '.join(sorted(missing))}.")
                rows = metadata.num_rows
                if not 0 < rows <= ROW_LIMITS[name]:
                    raise DatasetError(f"{name}: допустимо от 1 до {ROW_LIMITS[name]:,} строк.")
                total_decoded += sum(metadata.row_group(index).total_byte_size for index in range(metadata.num_row_groups))
                if total_decoded > MAX_UNCOMPRESSED_BYTES:
                    raise DatasetError("Данные после распаковки превышают лимит 128 МиБ.", 413)
                counts[name] = rows
        except DatasetError:
            raise
        except Exception as error:
            raise DatasetError(f"{name}: не удалось прочитать метаданные Parquet; проверьте файл.") from error
    return counts


class DatasetManager:
    def __init__(self, root: str | Path, default_engine, default_explanations, static_dir: str | Path):
        self.root = Path(root).resolve()
        self.static_dir = Path(static_dir).resolve()
        self.runs_dir = self.root / "runtime" / "datasets"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.default_engine = default_engine
        self.default_explanations = default_explanations
        self._lock = RLock()
        self._active: str | None = None
        self._runs: dict[str, dict[str, Any]] = {}
        self._contexts: OrderedDict[str, tuple] = OrderedDict()
        self._restore()

    def _directory(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise DatasetError("Набор данных не найден.", 404)
        path = self.runs_dir / run_id
        if path.resolve().parent != self.runs_dir.resolve() or path.is_symlink():
            raise DatasetError("Набор данных не найден.", 404)
        return path

    def _save(self, run_id: str) -> None:
        destination = self._directory(run_id) / "manifest.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._runs[run_id], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _restore(self) -> None:
        for directory in self.runs_dir.iterdir():
            if not directory.is_dir() or directory.is_symlink() or RUN_ID.fullmatch(directory.name) is None:
                continue
            manifest = directory / "manifest.json"
            try:
                if manifest.stat().st_size > 64 * 1024:
                    continue
                record = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("run_id") != directory.name:
                    continue
                if record.get("status") not in {"queued", "running", "ready", "failed"}:
                    continue
                self._runs[directory.name] = record
                if record["status"] in {"queued", "running"}:
                    message = "Сервер был перезапущен во время расчёта. Загрузите файлы ещё раз."
                    record.update(status="failed", phase="interrupted", error=message, message=message)
                    self._save(directory.name)
                elif record["status"] == "ready" and not self._outputs_exist(directory):
                    message = "Не все файлы расчёта доступны. Загрузите набор повторно."
                    record.update(status="failed", phase="missing_files", error=message, message=message)
                    self._save(directory.name)
            except (OSError, ValueError, TypeError):
                continue

    @staticmethod
    def _outputs_exist(directory: Path) -> bool:
        paths = [directory / "input" / f"{name}.parquet" for name in TABLE_COLUMNS]
        paths += [directory / "output" / name for name in EXPORT_NAMES]
        paths += [directory / "public" / "dashboard_data.js"]
        return all(path.is_file() for path in paths)

    def current(self) -> dict[str, Any]:
        engine = self.default_engine
        store = engine.store if engine is not None else None
        ready = bool(store is not None and store.pipeline_ready
                     and (self.static_dir / "dashboard_data.js").is_file()
                     and all((store.paths.output_dir / name).is_file() for name in EXPORT_NAMES))
        counts = {name: len(getattr(store, name)) for name in TABLE_COLUMNS} if store is not None else {}
        return {"run_id": "default", "status": "ready" if ready else "unavailable",
                "phase": "complete" if ready else "unavailable", "counts": counts,
                "dashboard_url": "/dashboard?run=default", "status_url": "/api/datasets/default", "exports": list(EXPORT_NAMES),
                "message": "Исходный набор данных" if ready else "Готовый исходный набор недоступен; загрузите три файла."}

    def status(self, run_id: str) -> dict[str, Any]:
        if run_id == "default":
            return self.current()
        self._directory(run_id)
        with self._lock:
            if run_id not in self._runs:
                raise DatasetError("Набор данных не найден.", 404)
            # JSON round-trip makes the result independent of mutable nested state.
            return json.loads(json.dumps(self._runs[run_id]))

    def submit(self, files: dict[str, bytes]) -> dict[str, Any]:
        with self._lock:
            if self._active is not None:
                raise DatasetError("Другой набор сейчас обрабатывается. Дождитесь завершения и повторите загрузку.", 409)
            counts = _preflight(files)
            run_id = uuid4().hex
            directory = self._directory(run_id)
            input_dir = directory / "input"
            input_dir.mkdir(parents=True, exist_ok=False)
            for name, payload in files.items():
                (input_dir / f"{name}.parquet").write_bytes(payload)
            self._runs[run_id] = {
                "run_id": run_id, "status": "queued", "phase": "queued", "counts": counts,
                "created_at": _utc_now(), "message": "Файлы приняты; подготовка расчёта.",
                "dashboard_url": f"/dashboard?run={run_id}", "status_url": f"/api/datasets/{run_id}", "exports": list(EXPORT_NAMES),
            }
            self._save(run_id)
            self._active = run_id
            result = self.status(run_id)
            try:
                Thread(target=self._process, args=(run_id,), name=f"dataset-{run_id[:8]}", daemon=True).start()
            except Exception:
                self._active = None
                message = "Не удалось запустить расчёт. Повторите загрузку."
                self._runs[run_id].update(status="failed", phase="failed", error=message, message=message)
                self._save(run_id)
                raise DatasetError("Не удалось запустить расчёт. Повторите загрузку.", 503)
            return result

    def _update(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            if fields.get("status") == "failed" and "error" in fields:
                fields.setdefault("message", fields["error"])
            self._runs[run_id].update(fields)
            self._save(run_id)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join([str(self.root / "src"), str(self.root), environment.get("PYTHONPATH", "")])
        environment["PYTHONIOENCODING"] = "utf-8"
        # The analysis is data-only and needs no API credentials in its process.
        environment.pop("OPENAI_API_KEY", None)
        return environment

    def _run_command(self, command: list[str], directory: Path, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, RUN_TIMEOUT_SECONDS)
        # Local diagnostics are outside the web root and are never returned verbatim.
        log_path = directory / "processing.log"
        with log_path.open("ab") as log:
            completed = subprocess.run(command, cwd=self.root, env=self._environment(),
                                       stdout=log, stderr=subprocess.STDOUT, timeout=remaining,
                                       check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            message = "Не удалось выполнить анализ. Проверьте согласованность таблиц и формат значений."
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 4096))
                tail = log.read().decode("utf-8", errors="replace")
            # Only known validation diagnostics are suitable for the UI. Do not
            # expose a traceback, arbitrary file content, paths or credentials.
            for line in reversed(tail.splitlines()):
                if line.startswith("ValueError:"):
                    detail = line.partition(":")[2].strip()
                    safe_starts = ("nodes.parquet", "edges.parquet", "transactions.parquet",
                                   "Отрицательные", "n_tx ", "В рёбрах", "gid ", "depth ",
                                   "is_seed ", "sum_kzt ", "date ", "src ", "dst ")
                    if detail.startswith(safe_starts):
                        message = "Ошибка входных данных: " + " ".join(detail.split())[:300]
                    break
            raise DatasetError(message, 422)

    def _process(self, run_id: str) -> None:
        directory = self._directory(run_id)
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        started = time.monotonic()
        try:
            self._update(run_id, status="running", phase="analysis", message="Проверка данных, расчёт ролей, кластеров и приоритета.")
            self._run_command([sys.executable, str(self.root / "run_pipeline.py"), "--data", str(directory / "input"),
                               "--out", str(directory / "output"), "--validate"], directory, deadline)
            self._update(run_id, phase="dashboard", message="Подготовка графа и таблиц интерфейса.")
            self._run_command([sys.executable, str(self.root / "dashboard" / "build_dashboard.py"),
                               "--data", str(directory / "input"), "--outputs", str(directory / "output"),
                               "--dist", str(directory / "public")], directory, deadline)
            if not self._outputs_exist(directory):
                raise DatasetError("Расчёт завершился без всех обязательных файлов; набор не опубликован.", 500)
            context = self._load_context(run_id)
            with self._lock:
                self._cache(run_id, context)
                self._update(run_id, status="ready", phase="complete", message="Анализ готов. Можно открыть интерфейс и скачать три CSV.",
                             completed_at=_utc_now(), elapsed_seconds=round(time.monotonic() - started, 2))
        except subprocess.TimeoutExpired:
            self._update(run_id, status="failed", phase="timeout", error="Расчёт превысил лимит 5 минут и остановлен. Уменьшите набор или запустите расширенный расчёт локально.")
        except DatasetError as error:
            self._update(run_id, status="failed", phase="failed", error=str(error))
        except Exception:
            self._update(run_id, status="failed", phase="failed", error="Не удалось обработать набор. Исходные данные не изменены. Проверьте формат и повторите загрузку.")
        finally:
            with self._lock:
                if self._active == run_id:
                    self._active = None

    def _load_context(self, run_id: str) -> tuple:
        directory = self._directory(run_id)
        engine = InvestigationEngine(GraphDataStore(root=self.root, data_dir=directory / "input",
                                                   output_dir=directory / "output", strict_outputs=True))
        return engine, RoleExplanationService(engine)

    def _cache(self, run_id: str, context: tuple) -> None:
        self._contexts[run_id] = context
        self._contexts.move_to_end(run_id)
        while len(self._contexts) > 3:
            self._contexts.popitem(last=False)

    def context(self, run_id: str = "default") -> tuple:
        state = self.status(run_id)
        if state["status"] != "ready":
            raise DatasetError("Этот набор ещё не готов для просмотра.", 409)
        if run_id == "default":
            return self.default_engine, self.default_explanations
        with self._lock:
            if run_id not in self._contexts:
                self._cache(run_id, self._load_context(run_id))
            self._contexts.move_to_end(run_id)
            return self._contexts[run_id]

    def asset(self, run_id: str, filename: str) -> Path:
        if filename not in (*EXPORT_NAMES, "dashboard_data.js"):
            raise DatasetError("Файл не найден.", 404)
        if self.status(run_id)["status"] != "ready":
            raise DatasetError("Выгрузки доступны только после успешного расчёта.", 409)
        if run_id == "default":
            base = self.static_dir if filename == "dashboard_data.js" else self.default_engine.store.paths.output_dir
        else:
            base = self._directory(run_id) / ("public" if filename == "dashboard_data.js" else "output")
        path = base / filename
        if not path.is_file() or path.resolve().parent != base.resolve():
            raise DatasetError("Файл расчёта не найден. Загрузите набор повторно.", 404)
        return path

    def archive(self, run_id: str) -> bytes:
        paths = [self.asset(run_id, name) for name in EXPORT_NAMES]
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, arcname=path.name)
        return buffer.getvalue()
