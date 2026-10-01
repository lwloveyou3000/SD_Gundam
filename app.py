"""Desktop entry point and optional diagnostic/validation commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
DEFAULT_ADB = r"F:\leidian\LDPlayer9\adb.exe"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="G 世纪永恒 ADB 挂机工具")
    result.add_argument("--check", action="store_true", help="只截图识别，不点击游戏")
    result.add_argument("--run", action="store_true", help="从命令行启动挂机；默认启动界面")
    result.add_argument("--adb", default=DEFAULT_ADB, help="ADB 程序路径")
    result.add_argument("--device", default="", help="ADB 设备编号")
    result.add_argument("--rounds", type=int, default=1, help="目标完成次数")
    result.add_argument("--infinite", action="store_true", help="无限循环")
    result.add_argument("--trace-transitions", action="store_true", help=argparse.SUPPRESS)
    return result


def command_line(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    import cv2

    from gget_runner.adb import AdbClient
    from gget_runner.runner import BotRunner, RunConfig
    from gget_runner.vision import ScreenDetector

    devices = AdbClient.list_devices(args.adb)
    connected = [device for device in devices if device.state == "device"]
    serial = args.device
    if not serial:
        if len(connected) != 1:
            raise RuntimeError("请连接一个模拟器，或用 --device 指定设备编号。")
        serial = connected[0].serial
    adb = AdbClient(args.adb, serial)
    detector = ScreenDetector(ROOT / "assets" / "profiles" / "default")
    if args.check:
        frame = adb.screenshot()
        result = detector.detect(frame)
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        screenshot_path = log_dir / "screen-check.png"
        cv2.imwrite(str(screenshot_path), frame)
        print(json.dumps({"device": serial, "game_foreground": adb.is_game_foreground(),
                          "state": result.state, "confidence": result.confidence,
                          "tap": result.tap, "details": result.details,
                          "screenshot": str(screenshot_path)}, ensure_ascii=False, indent=2))
        return 0 if result.state else 2

    config = RunConfig(rounds=args.rounds, infinite=args.infinite, diagnostics_dir=ROOT / "logs")
    event_records = []
    errors = []
    trace_counts = {}

    def receive(event) -> None:
        if event.kind == "frame":
            if args.trace_transitions and event.state in {"sortie", "reward"}:
                key = (event.completed, event.state)
                index = trace_counts.get(key, 0)
                if index < 14:
                    trace_dir = ROOT / "logs" / "transition-frames"
                    trace_dir.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(trace_dir / f"{event.completed}-{event.state}-{index:02d}.png"), event.frame)
                    trace_counts[key] = index + 1
            return
        item = {"time": time.strftime("%H:%M:%S"), "kind": event.kind,
                "message": event.message, "state": event.state, "completed": event.completed}
        event_records.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if event.kind == "error":
            errors.append(event.message)

    runner = BotRunner(adb, detector, config, receive)
    signal.signal(signal.SIGINT, lambda *_: runner.stop())
    runner.run()
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    report_path = log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
    report_path.write_text(json.dumps({"completed": runner.completed, "target": args.rounds,
                                       "infinite": args.infinite, "errors": errors,
                                       "events": event_records}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if errors else 0


def main() -> int:
    args = parser().parse_args()
    if args.check and args.run:
        parser().error("--check 与 --run 不能同时使用")
    try:
        if args.check or args.run:
            return command_line(args)
        from gget_runner.ui import launch_ui
        launch_ui(ROOT)
        return 0
    except Exception as error:
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if args.check or args.run:
            print(str(error), file=sys.stderr)
        else:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", f"{error}\n\n详细记录：{log_dir / 'startup-error.log'}", parent=root)
            root.destroy()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
