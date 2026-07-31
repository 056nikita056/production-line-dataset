from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .codex_runner import CodexRunner, Runner
from .db import Database
from .export_bundle import ExportService
from .queue import QueueRepository
from .routes.api import router as api_router
from .routes.web import router as web_router
from .scanner import Scanner
from .settings import Settings, load_settings
from .worker import Worker


ASSET_ROOT = Path(__file__).resolve().parent


class Services:
    def __init__(self, settings: Settings, runner: Runner | None = None):
        self.settings = settings
        self.db = Database(settings.path("database"))
        self.db.initialize()
        self.queue = QueueRepository(self.db)
        self.scanner = Scanner(settings, self.db, self.queue)
        self.runner = runner or CodexRunner(settings)
        self.worker = Worker(settings, self.db, self.queue, self.runner)
        self.exports = ExportService(settings, self.db, self.queue)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="label-worker")
        self._futures: set[Future[Any]] = set()
        self._future_lock = Lock()

    def recover(self) -> int:
        return self.queue.recover_processing()

    def _track(self, future: Future[Any]) -> None:
        with self._future_lock:
            self._futures.add(future)

        def done(completed: Future[Any]) -> None:
            with self._future_lock:
                self._futures.discard(completed)
            try:
                completed.result()
            except Exception as exc:
                logging.getLogger(__name__).exception("Фоновое задание завершилось ошибкой")
                self.db.record_error("background", "background_failure", str(exc))

        future.add_done_callback(done)

    def submit_run(self, run_id: str) -> None:
        self._track(self.executor.submit(self.worker.run, run_id))

    def submit_item(self, item_id: str, run_id: str | None) -> None:
        def process() -> None:
            self.worker.process_claimed(item_id)
            if run_id:
                self.queue.finalize_run(run_id)

        self._track(self.executor.submit(process))

    def executor_busy(self) -> bool:
        with self._future_lock:
            return any(not future.done() for future in self._futures)

    def agent_status(self) -> tuple[bool, str]:
        if isinstance(self.runner, CodexRunner):
            return self.runner.login_status()
        return True, "Тестовый runner готов"

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


def configure_logging(settings: Settings) -> None:
    log_path = settings.path("logs") / "labeling.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def create_app(
    settings: Settings | None = None,
    *,
    runner: Runner | None = None,
) -> FastAPI:
    active_settings = settings or load_settings()
    configure_logging(active_settings)
    service_container = Services(active_settings, runner=runner)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        recovered = service_container.recover()
        if recovered:
            logging.getLogger(__name__).warning(
                "Восстановлено зависших заданий: %s", recovered
            )
        yield
        service_container.shutdown()

    application = FastAPI(
        title="Предразметка кадров производственной линии",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.services = service_container
    application.state.templates = Jinja2Templates(
        directory=str(ASSET_ROOT / "templates")
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(ASSET_ROOT / "static")),
        name="static",
    )
    application.include_router(api_router)
    application.include_router(web_router)
    return application


app = create_app()
