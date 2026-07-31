from __future__ import annotations

import os
import sys

import pytest

from app.platform_support import detect_platform
from app.process_control import (
    PosixProcessController,
    WindowsProcessController,
    create_process_controller,
)


def test_detects_native_macos_linux_and_windows():
    macos = detect_platform(
        system_name="Darwin",
        release="25.0",
        architecture="arm64",
        environment={},
    )
    linux = detect_platform(
        system_name="Linux",
        release="6.8.0",
        architecture="x86_64",
        environment={},
    )
    windows = detect_platform(
        system_name="Windows",
        release="11",
        architecture="AMD64",
        environment={},
    )

    assert (macos.key, macos.native, macos.supported) == ("macos", True, True)
    assert (linux.key, linux.native, linux.supported) == ("linux", True, True)
    assert (windows.key, windows.native, windows.supported) == (
        "windows",
        True,
        True,
    )


def test_detects_wsl_as_additional_windows_mode():
    result = detect_platform(
        system_name="Linux",
        release="5.15.0-microsoft-standard-WSL2",
        architecture="x86_64",
        environment={"WSL_DISTRO_NAME": "Ubuntu"},
    )

    assert result.key == "windows_wsl2"
    assert result.supported
    assert not result.native


def test_unknown_platform_is_not_supported():
    result = detect_platform(
        system_name="Plan9",
        release="1",
        architecture="mips",
        environment={},
    )

    assert not result.supported
    assert result.key == "unsupported"


def test_process_controller_factory():
    assert isinstance(create_process_controller("posix"), PosixProcessController)
    assert isinstance(create_process_controller("nt"), WindowsProcessController)
    with pytest.raises(RuntimeError, match="Неподдерживаемая"):
        create_process_controller("other")


def test_platform_specific_process_options():
    assert PosixProcessController().popen_options() == {
        "start_new_session": True
    }
    assert WindowsProcessController().popen_options()["creationflags"] != 0


def test_active_controller_terminates_timed_out_process(tmp_path):
    controller = create_process_controller()
    controller.termination_grace_seconds = 1
    execution = controller.execute(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert execution.timed_out
    assert execution.returncode != 0
    if os.name == "nt":
        assert controller.platform_name == "windows"
    else:
        assert controller.platform_name == "posix"
