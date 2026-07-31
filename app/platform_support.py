from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    key: str
    label: str
    architecture: str
    supported: bool
    native: bool


def detect_platform(
    *,
    system_name: str | None = None,
    release: str | None = None,
    architecture: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> PlatformInfo:
    system = (system_name or platform.system()).strip()
    active_release = (release or platform.release()).lower()
    active_architecture = architecture or platform.machine() or "unknown"
    active_environment = environment if environment is not None else os.environ

    if system == "Darwin":
        return PlatformInfo(
            key="macos",
            label="macOS (нативно)",
            architecture=active_architecture,
            supported=True,
            native=True,
        )
    if system == "Linux":
        is_wsl = (
            "microsoft" in active_release
            or bool(active_environment.get("WSL_INTEROP"))
            or bool(active_environment.get("WSL_DISTRO_NAME"))
        )
        return PlatformInfo(
            key="windows_wsl2" if is_wsl else "linux",
            label="Windows + WSL2" if is_wsl else "Linux (нативно)",
            architecture=active_architecture,
            supported=True,
            native=not is_wsl,
        )
    if system == "Windows":
        return PlatformInfo(
            key="windows",
            label="Windows (нативно)",
            architecture=active_architecture,
            supported=True,
            native=True,
        )
    return PlatformInfo(
        key="unsupported",
        label=system or "Неизвестная ОС",
        architecture=active_architecture,
        supported=False,
        native=False,
    )
