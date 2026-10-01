"""Small, validated preferences file for the desktop interface."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile


DEFAULT_ADB_PATH = r"F:\leidian\LDPlayer9\adb.exe"
MAX_ROUNDS = 999999


@dataclass(frozen=True)
class AppSettings:
    adb_path: str = DEFAULT_ADB_PATH
    device: str = ""
    rounds: int = 1
    infinite: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.adb_path, str) or not self.adb_path.strip() or len(self.adb_path) > 4096
                or any(char in self.adb_path for char in "\r\n\x00")):
            raise ValueError("请填写有效的 ADB 程序路径")
        if not isinstance(self.device, str) or len(self.device) > 512 or any(char in self.device for char in "\r\n\x00"):
            raise ValueError("设备编号无效")
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) or not 1 <= self.rounds <= MAX_ROUNDS:
            raise ValueError(f"完成次数必须为 1 至 {MAX_ROUNDS} 的整数")
        if not isinstance(self.infinite, bool):
            raise ValueError("无限模式设置无效")


def load_settings(path: str | Path) -> AppSettings:
    """Recover valid fields independently; a damaged file never prevents launch."""
    defaults = AppSettings()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    values = asdict(defaults)
    for name in values:
        if name not in data:
            continue
        candidate = dict(values, **{name: data[name]})
        try:
            AppSettings(**candidate)
        except (TypeError, ValueError):
            continue
        values = candidate
    values["adb_path"] = values["adb_path"].strip()
    values["device"] = values["device"].strip()
    return AppSettings(**values)


def save_settings(path: str | Path, settings: AppSettings) -> None:
    """Replace preferences atomically so interrupted writes preserve the old file."""
    settings = AppSettings(**asdict(settings))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(asdict(settings), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
