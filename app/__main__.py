from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .codex_runner import CodexRunner, FakeCodexRunner
from .db import Database
from .export_bundle import ExportError, ExportService
from .legacy_import import LegacyImporter
from .queue import QueueRepository
from .scanner import Scanner
from .settings import Settings, load_settings
from .worker import Worker


def components(settings: Settings, *, fake: bool = False) -> dict[str, Any]:
    db = Database(settings.path("database"))
    db.initialize()
    queue = QueueRepository(db)
    runner = FakeCodexRunner() if fake else CodexRunner(settings)
    return {
        "db": db,
        "queue": queue,
        "scanner": Scanner(settings, db, queue),
        "runner": runner,
        "worker": Worker(settings, db, queue, runner),
        "exports": ExportService(settings, db, queue),
        "legacy": LegacyImporter(settings, db, queue),
    }


def doctor(settings: Settings) -> tuple[bool, dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check(
        "Python",
        sys.version_info >= (3, 11),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    for name in (
        "incoming",
        "processing",
        "completed",
        "review",
        "failed",
        "exports",
        "legacy",
        "logs",
    ):
        path = settings.path(name)
        writable = path.is_dir() and os_access_writable(path)
        check(f"Каталог {name}", writable, "доступен" if writable else "недоступен")
    try:
        db = Database(settings.path("database"))
        db.initialize()
        row = db.fetch_one("SELECT 1 AS ok")
        check("SQLite", bool(row and row["ok"] == 1), "база открывается")
    except Exception as exc:
        check("SQLite", False, str(exc))
    for name, path in (
        ("Prompt", settings.prompt_path),
        ("JSON Schema", settings.schema_path),
        ("Классы", settings.classes_path),
    ):
        check(name, path.is_file() and path.stat().st_size > 0, path.name)
    try:
        json.loads(settings.schema_path.read_text(encoding="utf-8"))
        check("JSON Schema parse", True, "валидный JSON")
    except (OSError, json.JSONDecodeError) as exc:
        check("JSON Schema parse", False, str(exc))
    codex = CodexRunner(settings)
    executable = codex.executable_path()
    check("Codex CLI", bool(executable), executable or "не найден")
    logged_in, detail = codex.login_status()
    check("Codex login", logged_in, detail)
    return all(item["ok"] for item in checks), {"ok": all(item["ok"] for item in checks), "checks": checks}


def os_access_writable(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK | os.X_OK)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Локальная предразметка кадров производственной линии",
    )
    parser.add_argument("--config", help="путь к app.yaml относительно корня модуля")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="запустить локальный веб-интерфейс")
    subparsers.add_parser("scan", help="просканировать data/incoming")
    process = subparsers.add_parser("process", help="обработать pending-кадры")
    process.add_argument(
        "--fake",
        action="store_true",
        help="детерминированный runner только для локального smoke-теста",
    )
    subparsers.add_parser("export", help="создать ZIP из approved-кадров")
    importer = subparsers.add_parser("import-legacy", help="импортировать ZIP или папку")
    importer.add_argument("path", type=Path)
    subparsers.add_parser("doctor", help="проверить установку и авторизацию")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.config)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Ошибка конфигурации: {exc}"}, ensure_ascii=False))
        return 2

    if args.command == "serve":
        import uvicorn
        from .main import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.app.host,
            port=settings.app.port,
            log_level="info",
        )
        return 0

    services = components(settings, fake=getattr(args, "fake", False))
    if args.command == "scan":
        print(json.dumps(services["scanner"].scan().to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "process":
        recovered = services["queue"].recover_processing()
        if not args.fake:
            logged_in, detail = services["runner"].login_status()
            if not logged_in:
                print(json.dumps({"ok": False, "error": detail}, ensure_ascii=False, indent=2))
                return 3
        run = services["queue"].create_run()
        if run["total_items"] == 0:
            services["queue"].finalize_run(run["id"])
            print(json.dumps({"ok": True, "message": "Нет pending-кадров", "recovered": recovered}, ensure_ascii=False, indent=2))
            return 0
        result = services["worker"].run(run["id"])
        print(json.dumps({"ok": True, "run": result, "recovered": recovered}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        try:
            result = services["exports"].create()
        except ExportError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 4
        result["download_path"] = result.pop("zip_path")
        print(json.dumps({"ok": True, "export": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "import-legacy":
        try:
            result = services["legacy"].import_path(args.path)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 5
        print(json.dumps({"ok": True, "import": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        ok, report = doctor(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

