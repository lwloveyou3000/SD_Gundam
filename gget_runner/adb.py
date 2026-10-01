"""Small, bounded ADB operations; never manage or restart the ADB server."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess

import cv2
import numpy as np


GAME_PACKAGE = "com.bandainamcoent.gget_WW"


class AdbError(RuntimeError):
    """An ADB operation failed or returned unusable output."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""


def _command(adb_path: str | Path, arguments: list[str], timeout: float = 15.0) -> bytes:
    try:
        result = subprocess.run(
            [str(adb_path), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdbError(f"ADB 执行失败：{exc}") from exc
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        raise AdbError(f"ADB 返回错误 ({result.returncode})：{reason[:500]}")
    return result.stdout


class AdbClient:
    def __init__(self, adb_path: str | Path, serial: str):
        self.adb_path = Path(adb_path)
        self.serial = serial.strip()
        if not self.serial:
            raise ValueError("请选择 ADB 设备")

    @staticmethod
    def list_devices(adb_path: str | Path) -> list[Device]:
        output = _command(adb_path, ["devices", "-l"]).decode("utf-8", errors="replace")
        devices = []
        for line in output.splitlines():
            fields = line.strip().split()
            if len(fields) < 2 or fields[0] == "List" or fields[0].startswith("*"):
                continue
            model = next((part[6:] for part in fields[2:] if part.startswith("model:")), "")
            devices.append(Device(fields[0], fields[1], model))
        return devices

    def _run(self, arguments: list[str], timeout: float = 15.0) -> bytes:
        return _command(self.adb_path, ["-s", self.serial, *arguments], timeout)

    def screenshot(self) -> np.ndarray:
        data = self._run(["exec-out", "screencap", "-p"])
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            raise AdbError("ADB 截图不是有效的 PNG 图片")
        return frame

    def tap(self, x: int, y: int) -> None:
        if isinstance(x, bool) or isinstance(y, bool) or x < 0 or y < 0:
            raise ValueError("点击坐标必须是非负像素坐标")
        self._run(["shell", "input", "tap", str(int(x)), str(int(y))], timeout=10.0)

    def is_game_foreground(self) -> bool:
        output = self._run(["shell", "dumpsys", "window", "windows"], timeout=10.0)
        text = output.decode("utf-8", errors="replace")
        # Prefer current focus. A system dialog over the game must prevent input.
        focus = re.search(r"mCurrentFocus\s*=\s*([^\r\n]+)", text)
        if focus is None:
            focus = re.search(r"mFocusedApp\s*=\s*([^\r\n]+)", text)
        if focus is None:
            return False
        return re.search(r"(?<![\w.])" + re.escape(GAME_PACKAGE) + r"(?=/|\s|\})", focus.group(1)) is not None
