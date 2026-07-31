from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ProcessExecution:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class ProcessController(Protocol):
    platform_name: str

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessExecution: ...


class BaseProcessController:
    platform_name = "unknown"
    termination_grace_seconds = 5

    def popen_options(self) -> dict[str, object]:
        return {}

    def request_stop(self, process: subprocess.Popen[str]) -> None:
        process.terminate()

    def force_stop(self, process: subprocess.Popen[str]) -> None:
        process.kill()

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessExecution:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            **self.popen_options(),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self.request_stop(process)
            try:
                stdout, stderr = process.communicate(
                    timeout=self.termination_grace_seconds
                )
            except subprocess.TimeoutExpired:
                self.force_stop(process)
                stdout, stderr = process.communicate()
        return ProcessExecution(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )


class PosixProcessController(BaseProcessController):
    platform_name = "posix"

    def popen_options(self) -> dict[str, object]:
        return {"start_new_session": True}

    def request_stop(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def force_stop(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class WindowsProcessController(BaseProcessController):
    platform_name = "windows"

    def popen_options(self) -> dict[str, object]:
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": creation_flag}

    def request_stop(self, process: subprocess.Popen[str]) -> None:
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is None:
            process.terminate()
            return
        try:
            process.send_signal(ctrl_break)
        except (OSError, ValueError):
            process.terminate()

    def force_stop(self, process: subprocess.Popen[str]) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.termination_grace_seconds,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def create_process_controller(
    platform_name: str | None = None,
) -> ProcessController:
    active_platform = platform_name or os.name
    if active_platform == "nt":
        return WindowsProcessController()
    if active_platform == "posix":
        return PosixProcessController()
    raise RuntimeError(f"Неподдерживаемая платформа процессов: {active_platform}")
