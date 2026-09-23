"""CLI and local HTTP interface for the HackAlem investigation assistant."""

from __future__ import annotations

import argparse
import asyncio
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:  # Support `python agent_app/main.py`.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent_app.agent import run_sdk_query
    from agent_app.engine import InvestigationEngine
    from agent_app.store import GraphDataStore
    from agent_app.explanations import RoleExplanationService
    from agent_app.datasets import DatasetError, DatasetManager, EXPORT_NAMES
    from agent_app.explanation_jobs import NarrativeManager
else:
    from .agent import run_sdk_query
    from .engine import InvestigationEngine
    from .store import GraphDataStore
    from .explanations import RoleExplanationService
    from .datasets import DatasetError, DatasetManager, EXPORT_NAMES
    from .explanation_jobs import NarrativeManager


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _configure_console() -> None:
    """Use UTF-8 for Russian output even in legacy Windows terminals."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


class AssistantHandler(SimpleHTTPRequestHandler):
    """Local analysis, dataset upload and snapshot-specific dashboard API."""

    engine: InvestigationEngine
    explanations: RoleExplanationService
    datasets: DatasetManager | None = None
    narratives: NarrativeManager | None = None

    def send_head(self):
        original_path = self.path
        route = urlparse(self.path).path
        if self.datasets is not None:
            if route in {"/", "/index.html"}:
                self.path = "/upload.html"
            elif route == "/dashboard":
                self.path = "/index.html"
        # Even when a custom static directory is supplied, never serve .env,
        # dotfiles, or links escaping the selected public directory.
        try:
            relative = Path(self.translate_path(self.path)).resolve().relative_to(Path(self.directory).resolve())
            if any(part.startswith(".") for part in relative.parts):
                raise ValueError("private file")
        except ValueError:
            self.path = original_path
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        try:
            return super().send_head()
        finally:
            self.path = original_path

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str, *, download: bool = False) -> None:
        try:
            source = path.open("rb")
        except OSError:
            self._send_json({"status": "not_found", "message": "Файл результата не найден."}, 404)
            return
        with source:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download:
                # Names come only from the manager's fixed export allowlist.
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            shutil.copyfileobj(source, self.wfile)

    def _run_id(self) -> str:
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        selected = query.get("run", ["default"])
        if len(selected) != 1:
            raise DatasetError("Укажите один набор данных.", 400)
        return selected[0]

    def _context(self):
        selected = self._run_id()
        if self.datasets is not None:
            return self.datasets.context(selected)
        if selected != "default":
            raise DatasetError("Набор данных не найден.", 404)
        return self.engine, self.explanations

    def _dataset_get(self, path: str) -> None:
        if self.datasets is None:
            raise DatasetError("Загрузка наборов данных недоступна.", 503)
        if path == "/api/datasets/current":
            self._send_json(self.datasets.current())
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[3]:
            self._send_json(self.datasets.status(parts[3]))
        elif len(parts) == 5 and parts[4] == "dashboard_data.js":
            self._send_file(self.datasets.asset(parts[3], parts[4]), "text/javascript; charset=utf-8")
        elif len(parts) == 6 and parts[4] == "exports":
            modes = parse_qs(urlparse(self.path).query).get("mode", ["extended"])
            if len(modes) != 1 or modes[0] not in {"extended", "contest"}:
                raise DatasetError("Неизвестный режим выгрузки.", 400)
            contest = modes[0] == "contest"
            if parts[5] == "results.zip":
                payload = (self.datasets.competition_archive(parts[3]) if contest else
                           self.narratives.archive(parts[3]) if self.narratives else self.datasets.archive(parts[3]))
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", 'attachment; filename="hackalem_results.zip"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            else:
                if parts[5] not in EXPORT_NAMES:
                    raise DatasetError("Файл выгрузки не найден.", 404)
                path = (self.datasets.competition_asset(parts[3], parts[5]) if contest else
                        self.narratives.export_file(parts[3], parts[5]) if self.narratives
                        else self.datasets.asset(parts[3], parts[5]))
                self._send_file(path, "text/csv; charset=utf-8", download=True)
        else:
            raise DatasetError("Ресурс набора данных не найден.", 404)

    def _same_origin_action(self, action: str) -> bool:
        origin = self.headers.get("Origin")
        return (self.headers.get("X-HackAlem-Action") == action
                and self.headers.get("Sec-Fetch-Site") != "cross-site"
                and (not origin or origin == f"http://{self.headers.get('Host')}"))

    def _upload(self) -> None:
        if self.datasets is None:
            raise DatasetError("Загрузка наборов данных недоступна.", 503)
        if not self._same_origin_action("upload"):
            raise DatasetError("Загружайте файлы со стартовой страницы локальной панели.", 403)
        if self.headers.get_content_type() != "multipart/form-data":
            raise DatasetError("Ожидается форма с тремя файлами Parquet.", 400)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 60 * 1024 * 1024 + 65536:
            self.close_connection = True
            raise DatasetError("Размер загрузки должен быть не более 60 МБ (20 МБ на файл).", 413)
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise DatasetError("Загрузите файлы обычной формой браузера.", 400)
        content_type = self.headers.get("Content-Type", "")
        if len(content_type) > 512 or "\r" in content_type or "\n" in content_type:
            raise DatasetError("Некорректный формат формы.", 400)
        body = self.rfile.read(length)
        if len(body) != length:
            raise DatasetError("Загрузка прервана. Выберите файлы и повторите попытку.", 400)
        message = BytesParser(policy=policy.default).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii", errors="replace")
            + b"\r\n\r\n" + body
        )
        if not message.is_multipart() or message.defects:
            raise DatasetError("Некорректная форма загрузки.", 400)
        parts = list(message.iter_parts())
        if len(parts) != 3:
            raise DatasetError("Выберите ровно три файла: nodes, edges и transactions.", 400)
        files = {}
        for part in parts:
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if (part.is_multipart() or part.defects or part.get_content_disposition() != "form-data"
                    or name not in {"nodes", "edges", "transactions"} or name in files
                    or not filename or not filename.lower().endswith(".parquet")):
                raise DatasetError("Каждый раздел должен содержать один файл .parquet.", 400)
            files[name] = part.get_payload(decode=True) or b""
        result = self.datasets.submit(files)
        self._send_json(result, HTTPStatus.ACCEPTED)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            if path == "/api/explanations/status":
                if not self.narratives:
                    raise DatasetError("Генерация объяснений недоступна.", 503)
                _, explanations = self._context()
                self._send_json({**explanations.status(), **self.narratives.status(self._run_id())})
                return
            if path.startswith("/api/datasets/") or path == "/api/datasets":
                self._dataset_get(path)
                return
            if path in {"/health", "/api/ai-status", "/api/capabilities"}:
                engine, explanations = self._context()
        except DatasetError as exc:
            self._send_json({"status": "invalid_request", "message": str(exc)}, exc.status)
            return
        if path == "/health":
            self._send_json(engine.health())
            return
        if path == "/api/ai-status":
            self._send_json(explanations.status())
            return
        if path == "/api/capabilities":
            self._send_json(
                {
                    "status": "ok",
                    "default_mode": "offline",
                    "actions": [
                        "node_profile",
                        "explain_priority",
                        "neighbors",
                        "trace_routes",
                        "common_recipients",
                        "compare_nodes",
                        "cluster_summary",
                        "top_candidates",
                        "uncertain_nodes",
                        "resilience_summary",
                    ],
                    "read_only": False,
                    "analysis_actions_read_only": True,
                    "dataset_upload": self.datasets is not None,
                    "csv_explanations": self.narratives is not None,
                    "external_enrichment": False,
                    "role_explanations": explanations.status(),
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/api/datasets":
            try:
                self._upload()
            except DatasetError as exc:
                self.close_connection = True
                self._send_json({"status": "invalid_request", "message": str(exc)}, exc.status)
            except (OSError, ValueError):
                self.close_connection = True
                self._send_json({"status": "invalid_request", "message": "Не удалось принять файлы. Проверьте формат и свободное место."}, 400)
            return
        batch_paths = {"/api/explanations/start", "/api/explanations/cancel"}
        if path not in {"/api/query", "/api/action", "/api/explain-node", *batch_paths}:
            self._send_json({"status": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if path == "/api/explain-node" or path in batch_paths:
            # A custom same-origin header prevents unrelated sites from triggering
            # paid generations against this localhost service. No CORS is granted.
            action = "explain-batch" if path in batch_paths else "explain-node"
            if (not self._same_origin_action(action)
                    or self.headers.get_content_type() != "application/json"
                    ):
                self._send_json({"status": "forbidden", "message": "Откройте карточку узла в локальном dashboard."}, 403)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("invalid content length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                {"status": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            engine, explanations = self._context()
            if path in batch_paths:
                if not self.narratives:
                    raise DatasetError("Генерация объяснений недоступна.", 503)
                if path.endswith("/start"):
                    if body.get("consent") is not True:
                        raise DatasetError("Подтвердите отправку обезличенных метрик и использование платного API.", 400)
                    result = self.narratives.start(self._run_id(), scope=body.get("scope", "all"), limit=body.get("limit"))
                    self._send_json(result, 202)
                else:
                    self._send_json(self.narratives.cancel(self._run_id()))
                return
        except DatasetError as exc:
            self._send_json({"status": "invalid_request", "message": str(exc)}, exc.status)
            return

        if path == "/api/explain-node":
            gid = body.get("gid")
            if (not isinstance(gid, str) or not gid.isascii()
                    or not gid.removeprefix("-").isdigit() or len(gid) > 32):
                self._send_json({"status": "invalid_request", "message": "gid должен быть строковым идентификатором узла."}, 400)
                return
            if self.narratives:
                try:
                    result, code = self.narratives.explain_node(self._run_id(), gid)
                except DatasetError as exc:
                    self._send_json({"status": "invalid_request", "message": str(exc)}, exc.status)
                    return
            else:
                result, code = explanations.explain(gid)
            self._send_json(result, code)
            return
        if path == "/api/action":
            result = engine.execute(body)
        else:
            # General chat remains local. Only the explicit node explanation
            # endpoint above can invoke the configured external model.
            result = engine.answer_offline(str(body.get("question", "")))
        code = HTTPStatus.OK if result.get("status") not in {"invalid_request"} else HTTPStatus.BAD_REQUEST
        self._send_json(result, code)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[agent-http] {self.address_string()} {format % args}")


def build_engine(args: argparse.Namespace, *, strict_outputs: bool = False) -> InvestigationEngine:
    store = GraphDataStore(
        root=args.root,
        data_dir=args.data,
        output_dir=args.output,
        strict_outputs=strict_outputs,
    )
    return InvestigationEngine(store)


def serve(args: argparse.Namespace) -> None:
    try:
        engine = build_engine(args)
    except FileNotFoundError:
        # The upload page must also work on a fresh copy without bundled data.
        engine = None
    static_dir = Path(args.static_dir).resolve() if args.static_dir else Path(__file__).resolve().parents[1] / "dashboard" / "dist"
    if not static_dir.exists():
        raise FileNotFoundError(f"Static directory does not exist: {static_dir}")

    class ProjectHandler(AssistantHandler):
        pass

    ProjectHandler.engine = engine
    ProjectHandler.explanations = RoleExplanationService(engine) if engine is not None else None
    ProjectHandler.datasets = DatasetManager(
        root=Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1],
        default_engine=engine,
        default_explanations=ProjectHandler.explanations,
        static_dir=static_dir,
    )
    ProjectHandler.narratives = NarrativeManager(ProjectHandler.datasets)
    handler = lambda *handler_args, **kwargs: ProjectHandler(  # noqa: E731
        *handler_args, directory=str(static_dir), **kwargs
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"HackAlem assistant: http://{args.host}:{args.port}")
    print(f"Health: http://{args.host}:{args.port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HackAlem explainable graph assistant")
    parser.add_argument("--root", default=None, help="Project root (auto-detected by default)")
    parser.add_argument("--data", default=None, help="Directory containing source parquet files")
    parser.add_argument("--output", default=None, help="Directory containing pipeline CSV outputs")
    subparsers = parser.add_subparsers(dest="command")

    query = subparsers.add_parser("query", help="Ask a question")
    query.add_argument("question")
    query.add_argument("--mode", choices=("offline", "sdk"), default="offline")

    action = subparsers.add_parser("action", help="Run one structured read-only action")
    action.add_argument("action_name")
    action.add_argument("--gid")
    action.add_argument("--gids", nargs="*")
    action.add_argument("--cluster-id")
    action.add_argument("--role")
    action.add_argument("--strategy")
    action.add_argument("--direction", choices=("in", "out", "both"), default="both")
    action.add_argument("--limit", type=int, default=20)
    action.add_argument("--max-hops", type=int, default=3)
    action.add_argument("--min-kzt", type=float, default=0.0)

    server = subparsers.add_parser("serve", help="Run the local read-only HTTP API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=int(os.getenv("PORT", "8770")))
    server.add_argument("--static-dir", default=None, help="Optional directory to serve at /")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "serve":
        serve(args)
        return 0
    if args.command == "query":
        if args.mode == "sdk":
            store = GraphDataStore(
                root=args.root,
                data_dir=args.data,
                output_dir=args.output,
                strict_outputs=True,
            )
            _print_json(await run_sdk_query(args.question, store=store))
        else:
            _print_json(build_engine(args).answer_offline(args.question))
        return 0
    if args.command == "action":
        request = {
            "action": args.action_name,
            "gid": args.gid,
            "gids": args.gids,
            "cluster_id": args.cluster_id,
            "role": args.role,
            "strategy": args.strategy,
            "direction": args.direction,
            "limit": args.limit,
            "max_hops": args.max_hops,
            "min_kzt": args.min_kzt,
        }
        _print_json(build_engine(args).execute(request))
        return 0
    _print_json(build_engine(args).health())
    return 0


def main() -> int:
    _configure_console()
    parser = make_parser()
    args = parser.parse_args()
    if args.command is None and os.environ.get("PORT"):
        args.command = "serve"
        args.host = "0.0.0.0"
        args.port = int(os.environ["PORT"])
        args.static_dir = None
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
