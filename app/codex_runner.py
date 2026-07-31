from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .models import RunnerResult
from .process_control import ProcessController, create_process_controller
from .settings import Settings


class Runner(Protocol):
    def run(
        self,
        image_path: Path,
        raw_response_path: Path,
        stderr_path: Path,
    ) -> RunnerResult: ...


class CodexRunner:
    def __init__(
        self,
        settings: Settings,
        process_controller: ProcessController | None = None,
    ):
        self.settings = settings
        self.process_controller = process_controller or create_process_controller()

    def executable_path(self) -> str | None:
        configured = self.settings.codex.executable
        if Path(configured).is_absolute():
            return configured if Path(configured).is_file() else None
        return shutil.which(configured)

    def login_status(self) -> tuple[bool, str]:
        executable = self.executable_path()
        if not executable:
            return False, "Codex CLI не найден в PATH"
        try:
            result = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Не удалось проверить авторизацию Codex: {exc}"
        output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        return result.returncode == 0, output or f"codex login status: код {result.returncode}"

    def prompt_text(self) -> str:
        prompt = self.settings.prompt_path.read_text(encoding="utf-8").rstrip()
        annotation = self.settings.annotation
        if not annotation.line_use_camera_template or not annotation.line_template:
            return prompt
        points = " → ".join(f"({x}, {y})" for x, y in annotation.line_template)
        if annotation.line_expected_count == 0:
            calibration = f"""

КАЛИБРОВКА ФИКСИРОВАННОЙ КАМЕРЫ CAM7-REFT:

- Не создавай объекты класса `line`: положение линии постоянно и уже задано
  конфигурацией камеры.
- Эталонная зона центрального синего конвейера в координатах 0..1000:
  {points}.
- Используй эту зону только как ориентир для точного отделения лотков от конвейера.
  Контуры лотков должны идти по внешнему краю пластика и не включать поверхность линии.
- Левый стол с курицей, пол, правая металлическая установка, люди, кабели и соседнее
  оборудование не являются объектами разметки.
- Если реальное изображение камеры смещено относительно эталонной зоны более чем
  примерно на {annotation.line_shift_tolerance} единиц, не создавай `line`, но установи
  `needs_review=true` и добавь `camera_or_line_shift`.
"""
            return prompt + calibration.rstrip()
        expected = (
            f"Ровно {annotation.line_expected_count} объект `line`."
            if annotation.line_expected_count is not None
            else "Количество объектов `line` определяется по изображению."
        )
        calibration = f"""

КАЛИБРОВКА ФИКСИРОВАННОЙ КАМЕРЫ CAM7-REFT:

- {expected}
- Эталонный внешний контур центрального синего конвейера в координатах 0..1000:
  {points}.
- Ставь вершины `line` на реальные внешние границы конвейера, ориентируясь на эталон.
  Не размечай как `line` левый стол с курицей, пол, правую металлическую установку,
  людей, кабели и соседнее оборудование.
- Если реальная граница смещена от эталона более чем примерно на
  {annotation.line_shift_tolerance} единиц, обведи фактический контур,
  установи `needs_review=true` и добавь `camera_or_line_shift`.
"""
        return prompt + calibration.rstrip()

    def build_command(
        self,
        image_path: Path,
        raw_response_path: Path,
    ) -> list[str]:
        executable = self.executable_path()
        if not executable:
            raise FileNotFoundError("Codex CLI не найден. Установите Codex и выполните codex login")
        command = [
            executable,
            "--ask-for-approval",
            self.settings.codex.approval_policy,
            "exec",
            "--config",
            f'approval_policy="{self.settings.codex.approval_policy}"',
        ]
        if self.settings.codex.ephemeral:
            command.append("--ephemeral")
        command.extend(
            [
                "--skip-git-repo-check",
                "--cd",
                str(self.settings.root),
                "--image",
                str(image_path.resolve(strict=True)),
                "--sandbox",
                self.settings.codex.sandbox,
                "--output-schema",
                str(self.settings.schema_path),
                "--output-last-message",
                str(raw_response_path),
                self.prompt_text(),
            ]
        )
        return command

    def run(
        self,
        image_path: Path,
        raw_response_path: Path,
        stderr_path: Path,
    ) -> RunnerResult:
        raw_response_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            command = self.build_command(image_path, raw_response_path)
            execution = self.process_controller.execute(
                command,
                cwd=self.settings.root,
                timeout_seconds=self.settings.worker.timeout_seconds,
            )
            duration = round((time.monotonic() - started) * 1000)
            stderr_path.write_text(execution.stderr, encoding="utf-8")
            return RunnerResult(
                exit_code=124 if execution.timed_out else execution.returncode,
                duration_ms=duration,
                stdout=execution.stdout,
                stderr=execution.stderr,
                timed_out=execution.timed_out,
                error="Истёк таймаут Codex" if execution.timed_out else None,
            )
        except OSError as exc:
            duration = round((time.monotonic() - started) * 1000)
            message = str(exc)
            stderr_path.write_text(message, encoding="utf-8")
            return RunnerResult(
                exit_code=127,
                duration_ms=duration,
                stderr=message,
                error=message,
            )


class FakeCodexRunner:
    """Детерминированный runner только для тестов и локальной диагностики."""

    def __init__(self, payload: dict[str, object] | None = None, *, exit_code: int = 0):
        self.payload = payload
        self.exit_code = exit_code
        self.calls = 0

    def run(
        self,
        image_path: Path,
        raw_response_path: Path,
        stderr_path: Path,
    ) -> RunnerResult:
        from PIL import Image

        self.calls += 1
        started = time.monotonic()
        with Image.open(image_path) as image:
            width, height = image.size
        payload = self.payload or {
            "image_width": width,
            "image_height": height,
            "objects": [
                {
                    "class_id": 0,
                    "class_name": "tray_filled",
                    "polygon": [
                        {"x": 200, "y": 200},
                        {"x": 400, "y": 200},
                        {"x": 400, "y": 400},
                        {"x": 200, "y": 400},
                    ],
                    "occluded": False,
                    "visible_fraction": 1.0,
                }
            ],
            "needs_review": False,
            "review_reasons": [],
        }
        raw_response_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        if self.exit_code == 0:
            raw_response_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        stderr_path.write_text("" if self.exit_code == 0 else "fake failure", encoding="utf-8")
        return RunnerResult(
            exit_code=self.exit_code,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout="",
            stderr="" if self.exit_code == 0 else "fake failure",
            error=None if self.exit_code == 0 else "fake failure",
        )
