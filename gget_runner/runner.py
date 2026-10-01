"""Single-input stage workflow with conservative transitions and interruptible waits."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import threading
import time
from typing import Callable, TYPE_CHECKING

import cv2
import numpy as np

from .vision import Detection

if TYPE_CHECKING:
    from .loot import LootSnapshot


LABELS = {"prepare": "关卡准备", "sortie": "出击准备", "battle_intro": "战斗开始", "battle": "自动战斗中",
          "score": "分数结算", "experience": "经验结算", "reward": "奖励结算"}
NEXT = {"prepare": "sortie", "sortie": "battle_intro", "battle_intro": "score",
        "score": "experience", "experience": "reward", "reward": "battle_intro"}
START_PAGES = {"prepare", "sortie", "battle_intro", "battle"}


@dataclass(frozen=True)
class RunConfig:
    rounds: int = 1
    infinite: bool = False
    poll_interval: float = 1.0
    stable_frames: int = 2
    action_cooldown: float = 1.5
    transition_timeout: float = 60.0
    battle_timeout: float = 900.0
    diagnostics_dir: Path = Path("logs")

    def __post_init__(self):
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) or self.rounds < 1:
            raise ValueError("目标次数必须是正整数")
        if isinstance(self.stable_frames, bool) or not isinstance(self.stable_frames, int) or self.stable_frames < 2:
            raise ValueError("至少需要连续两帧确认页面")
        for name in ("poll_interval", "transition_timeout", "battle_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if not math.isfinite(self.action_cooldown) or self.action_cooldown < 0:
            raise ValueError("action_cooldown 不能是负数")


@dataclass(frozen=True)
class RunEvent:
    kind: str
    message: str = ""
    state: str = ""
    completed: int = 0
    frame: np.ndarray | None = None
    loot: LootSnapshot | None = None


class BotRunner:
    def __init__(self, adb, detector, config: RunConfig, on_event: Callable[[RunEvent], None], loot_reader=None):
        self.adb = adb
        self.detector = detector
        self.config = config
        self.on_event = on_event
        self.loot_reader = loot_reader
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self.completed = 0
        self._state = ""
        self._last_frame: np.ndarray | None = None
        self._last_detection: Detection | None = None
        self._pending_events: deque[tuple[str, str]] = deque()

    @property
    def running(self) -> bool:
        with self._condition:
            return self._running

    @property
    def paused(self) -> bool:
        return self._pause.is_set() and self.running

    def _event(self, kind: str, message: str = "", frame=None, loot=None) -> None:
        try:
            self.on_event(RunEvent(kind, message, self._state, self.completed, frame, loot))
        except Exception:
            # A closing GUI or observer failure must never leave a click loop alive.
            self._stop.set()
            raise

    def _claim(self) -> None:
        if self._running:
            raise RuntimeError("脚本已经在运行")
        self._stop.clear()
        self._pause.clear()
        self._running = True
        self.completed = 0
        self._state = ""
        self._last_frame = None
        self._last_detection = None
        self._pending_events.clear()

    def start(self) -> None:
        with self._condition:
            self._claim()
            self._thread = threading.Thread(target=self._run_main, name="gget-stage-runner", daemon=True)
            self._thread.start()

    def run(self) -> None:
        with self._condition:
            self._claim()
            self._thread = threading.current_thread()
        self._run_main()

    def stop(self) -> None:
        # Set before taking the action lock so a queued tap sees cancellation.
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    def pause(self) -> None:
        if not self._running or self._pause.is_set() or self._stop.is_set():
            return
        self._pause.set()
        with self._condition:
            self._pending_events.append(("paused", "已暂停；恢复后重新确认页面"))
            self._condition.notify_all()

    def resume(self) -> None:
        if not self.running or not self._pause.is_set() or self._stop.is_set():
            return
        with self._condition:
            self._pause.clear()
            self._pending_events.append(("resumed", "已恢复"))
            self._condition.notify_all()

    def join(self, timeout=None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _active(self) -> tuple[bool, float]:
        started = time.monotonic()
        waited = False
        with self._condition:
            while self._pending_events:
                self._event(*self._pending_events.popleft())
            while self._pause.is_set() and not self._stop.is_set():
                waited = True
                self._condition.wait()
                while self._pending_events:
                    self._event(*self._pending_events.popleft())
        return not self._stop.is_set(), time.monotonic() - started if waited else 0.0

    def _sleep(self) -> None:
        with self._condition:
            if not self._pause.is_set() and not self._stop.is_set():
                self._condition.wait(self.config.poll_interval)

    def _tap(self, detection: Detection) -> bool:
        if detection.state == "battle":
            raise RuntimeError("自动战斗页面禁止点击")
        if detection.tap is None:
            raise RuntimeError("识别页面没有安全点击位置")
        # Recheck immediately before input, even if capture happened seconds ago.
        if not self.adb.is_game_foreground():
            raise RuntimeError("游戏不在前台，已停止点击")
        with self._condition:
            if self._stop.is_set() or self._pause.is_set():
                return False
            self.adb.tap(*detection.tap)
        return True

    def _diagnostic(self, reason: str) -> str:
        directory = Path(self.config.diagnostics_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("error_%Y%m%d_%H%M%S_%f")
        frame = self._last_frame
        if frame is None:
            try:
                frame = self.adb.screenshot()
            except Exception:
                pass
        image_path = directory / f"{stem}.png"
        if frame is not None:
            success, encoded = cv2.imencode(".png", frame)
            if success:
                encoded.tofile(str(image_path))
        details = {"error": reason, "state": self._state, "completed": self.completed,
                   "detection": self._last_detection.details if self._last_detection else None,
                   "screenshot": image_path.name if image_path.exists() else None}
        report_path = directory / f"{stem}.json"
        report_path.write_text(json.dumps(details, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(image_path if image_path.exists() else report_path)

    def _run_main(self) -> None:
        expected = None
        phase_started = time.monotonic()
        last_action = -math.inf
        stable_state = None
        stable_count = 0
        battle_active = False
        result_message = "已停止"
        try:
            self._event("log", "开始识别关卡；请确保游戏已开启 AUTO")
            while not self._stop.is_set():
                active, pause_duration = self._active()
                if not active:
                    break
                if pause_duration:
                    phase_started += pause_duration
                    last_action += pause_duration
                    stable_state, stable_count = None, 0
                if not self.adb.is_game_foreground():
                    raise RuntimeError("游戏不在前台，请返回游戏后重新启动")
                frame = self.adb.screenshot()
                self._last_frame = frame
                self._event("frame", frame=frame)
                if self._stop.is_set() or self._pause.is_set():
                    stable_state, stable_count = None, 0
                    continue
                detection = self.detector.detect(frame)
                self._last_detection = detection
                state = detection.state
                if state != stable_state or state is None:
                    stable_state, stable_count = state, 1 if state is not None else 0
                else:
                    stable_count += 1
                now = time.monotonic()
                ready = state is not None and stable_count >= self.config.stable_frames
                if ready and expected is None and state not in START_PAGES:
                    raise RuntimeError("请停在准备、出击、TAP TO NEXT 或已开启 AUTO 的战斗页面后启动")
                if ready and state == "battle" and expected in {None, "battle_intro", "score"}:
                    if self._state != "battle" or not battle_active or expected != "score":
                        self._state = "battle"
                        self._event("state", "已确认：自动战斗中（等待结算，不发送输入）")
                        if self._stop.is_set() or self._pause.is_set():
                            stable_state, stable_count = None, 0
                            continue
                        battle_active = True
                        expected = "score"
                        phase_started = now
                        self._event("log", "已识别 TURN / AUTO 战斗标志，等待分数结算")
                permitted = ready and state != "battle" and (state in START_PAGES if expected is None else state == expected)
                if permitted and now - last_action >= self.config.action_cooldown:
                    self._state = state
                    self._event("state", f"已确认：{LABELS.get(state, state)}")
                    if self._stop.is_set() or self._pause.is_set():
                        stable_state, stable_count = None, 0
                        continue
                    if state == "reward":
                        if not battle_active:
                            raise RuntimeError("未确认本轮战斗开始，不能计入奖励")
                        # Only expected reward is accepted; stale reward cannot re-enter.
                        self.completed += 1
                        battle_active = False
                        expected = "battle_intro"
                        if self.loot_reader is not None:
                            try:
                                loot = self.loot_reader.read(frame)
                            except Exception as loot_error:
                                from .loot import LootSnapshot
                                loot = LootSnapshot(warnings=(f"本轮战利品识别失败：{loot_error}",))
                            if not loot.drops and not loot.warnings:
                                from .loot import LootSnapshot
                                loot = LootSnapshot(warnings=("本轮未能读取战利品，数量待确认",))
                            self._event("loot", loot=loot)
                            for warning in loot.warnings:
                                self._event("log", warning)
                        self._event("progress", f"已完成 {self.completed} 次")
                        if not self.config.infinite and self.completed >= self.config.rounds:
                            result_message = f"已完成目标 {self.completed} 次，停在奖励页面"
                            break
                        if self._stop.is_set() or self._pause.is_set():
                            # Resume must still perform repeat once, without recounting.
                            expected = "reward_repeat"
                            stable_state, stable_count = None, 0
                            continue
                    if not self._tap(detection):
                        if state == "reward":
                            expected = "reward_repeat"
                        stable_state, stable_count = None, 0
                        continue
                    self._event("log", f"已点击：{LABELS.get(state, state)}")
                    last_action = phase_started = time.monotonic()
                    expected = NEXT[state]
                    if state == "battle_intro":
                        battle_active = True
                    stable_state, stable_count = None, 0
                elif ready and expected == "reward_repeat" and state == "reward" and now - last_action >= self.config.action_cooldown:
                    if self._tap(detection):
                        self._event("log", "已点击：再次挑战")
                        expected = "battle_intro"
                        last_action = phase_started = time.monotonic()
                        stable_state, stable_count = None, 0
                # An intro that never went away is a failed transition. A
                # confirmed battle and its unrecognized animations get the
                # longer watchdog without resetting it on every combat frame.
                waiting_for_battle = expected == "score" and state not in {"prepare", "sortie", "battle_intro"}
                timeout = self.config.battle_timeout if waiting_for_battle else self.config.transition_timeout
                if time.monotonic() - phase_started > timeout:
                    if waiting_for_battle:
                        raise RuntimeError("等待战斗结算超时；请检查 AUTO、体力或游戏提示")
                    if expected == "score":
                        raise RuntimeError("战斗开始页面未正常切换；请检查游戏提示后人工处理")
                    target = LABELS.get(expected, expected) if expected else "可启动的关卡页面"
                    raise RuntimeError(f"等待{target}超时；遇到未知页面或弹窗，请人工处理")
                self._sleep()
        except Exception as exc:
            if not self._stop.is_set():
                result_message = "发生错误，已停止"
                try:
                    path = self._diagnostic(str(exc))
                    self._event("error", f"{exc}；诊断截图：{path}")
                except Exception as diagnostic_error:
                    try:
                        self._event("error", f"{exc}；诊断保存失败：{diagnostic_error}")
                    except Exception:
                        pass
        finally:
            with self._condition:
                self._running = False
                self._pause.clear()
                self._condition.notify_all()
            try:
                self._event("finished", result_message)
            except Exception:
                pass
