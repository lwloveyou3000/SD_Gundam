from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import math
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np
import pytest

from gget_runner.adb import AdbClient, AdbError, AdbTimeoutError, GAME_PACKAGE
from gget_runner.runner import BotRunner, RunConfig
from gget_runner.vision import Detection, ScreenDetector


PAGES = [None, "prepare", "sortie", "battle_intro", "score", "experience", "reward", "battle"]
TAPS = {state: (index * 10, index * 10) for index, state in enumerate(PAGES) if state}


class FakeAdb:
    def __init__(self, pages, foreground=True):
        self.pages = list(pages)
        self.position = 0
        self.taps = []
        self.foreground = foreground
        self.checks = 0

    def is_game_foreground(self):
        self.checks += 1
        return self.foreground

    def screenshot(self):
        page = self.pages[min(self.position, len(self.pages) - 1)]
        self.position += 1
        return np.full((18, 32, 3), PAGES.index(page), dtype=np.uint8)

    def tap(self, x, y):
        self.taps.append((x, y))


class FakeDetector:
    def detect(self, frame):
        state = PAGES[int(frame[0, 0, 0])]
        return Detection(state, 0.99 if state else 0.0, TAPS.get(state), {"state": state})


def repeated(*states, copies=2):
    return [state for state in states for _ in range(copies)]


def cycle(start=True):
    beginning = repeated("prepare", "sortie") if start else []
    return beginning + repeated("battle_intro") + repeated(None, copies=3) + repeated("score", "experience", "reward")


def config(tmp_path, **changes):
    return replace(RunConfig(poll_interval=0.002, action_cooldown=0.0,
                             transition_timeout=0.2, battle_timeout=0.4,
                             diagnostics_dir=tmp_path), **changes)


def make_runner(tmp_path, pages, callback=None, loot_reader=None, **changes):
    adb = FakeAdb(pages)
    events = []

    def on_event(event):
        events.append(event)
        if callback:
            callback(event)

    runner = BotRunner(adb, FakeDetector(), config(tmp_path, **changes), on_event, loot_reader=loot_reader)
    return runner, adb, events


def test_one_round_stops_at_reward_without_repeat(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle())
    runner.run()
    assert adb.taps == [TAPS[s] for s in ("prepare", "sortie", "battle_intro", "score", "experience")]
    assert runner.completed == 1 and not runner.running
    assert [e.completed for e in events if e.kind == "progress"] == [1]
    assert events[-1].kind == "finished"
    assert "目标" in events[-1].message
    assert not any(e.kind == "error" for e in events)


def test_two_rounds_stale_reward_and_intro_do_not_double_click(tmp_path):
    pages = cycle() + repeated("reward", copies=5) + repeated("battle_intro", copies=5) + repeated(None) + repeated("score", "experience", "reward")
    runner, adb, events = make_runner(tmp_path, pages, rounds=2)
    runner.run()
    assert runner.completed == 2
    assert adb.taps.count(TAPS["reward"]) == 1
    assert adb.taps.count(TAPS["battle_intro"]) == 2
    assert [e.completed for e in events if e.kind == "progress"] == [1, 2]
    assert not any(e.kind == "error" for e in events)


class FakeLootReader:
    def __init__(self, snapshot=None, error=None):
        from gget_runner.loot import LootDrop, LootSnapshot
        self.snapshot = snapshot if snapshot is not None else LootSnapshot((
            LootDrop("coin", "金币", 2000, b""),
            LootDrop("item-01", "道具01", 2, b""),
        ))
        self.error = error
        self.reads = []

    def read(self, frame):
        self.reads.append(PAGES[int(frame[0, 0, 0])])
        if self.error:
            raise self.error
        return self.snapshot


def test_final_reward_is_read_before_target_finishes(tmp_path):
    reader = FakeLootReader()
    runner, adb, events = make_runner(tmp_path, cycle(), loot_reader=reader)
    runner.run()
    assert reader.reads == ["reward"]
    loot_events = [event for event in events if event.kind == "loot"]
    assert len(loot_events) == 1 and loot_events[0].completed == 1
    assert loot_events[0].loot is reader.snapshot
    assert next(i for i, event in enumerate(events) if event.kind == "loot") < next(
        i for i, event in enumerate(events) if event.kind == "finished")
    assert TAPS["reward"] not in adb.taps


def test_stale_rewards_only_count_each_battle_once(tmp_path):
    from gget_runner.loot import LootLedger
    reader, ledger = FakeLootReader(), LootLedger()

    def receive(event):
        if event.kind == "loot":
            assert ledger.record_round(event.completed, event.loot)

    pages = cycle() + repeated("reward", copies=6) + cycle(False)
    runner, _, events = make_runner(tmp_path, pages, receive, loot_reader=reader, rounds=2)
    runner.run()
    assert reader.reads == ["reward", "reward"]
    assert [event.completed for event in events if event.kind == "loot"] == [1, 2]
    assert ledger.rounds == 2 and ledger.coin_total == 4000
    assert {row.item_id: row.total_quantity for row in ledger.rows} == {"coin": 4000, "item-01": 4}


def test_loot_read_failure_marks_round_and_allows_next_battle(tmp_path):
    reader = FakeLootReader(error=RuntimeError("OCR unavailable"))
    runner, adb, events = make_runner(tmp_path, cycle() + cycle(False), loot_reader=reader, rounds=2)
    runner.run()
    assert runner.completed == 2 and adb.taps.count(TAPS["reward"]) == 1
    loot_events = [event for event in events if event.kind == "loot"]
    assert len(loot_events) == 2
    assert all(event.loot.drops == () and "OCR unavailable" in event.loot.warnings[0]
               for event in loot_events)
    assert not any(event.kind == "error" for event in events)


def test_empty_loot_read_is_marked_as_unconfirmed(tmp_path):
    from gget_runner.loot import LootLedger, LootSnapshot
    reader = FakeLootReader(snapshot=LootSnapshot())
    runner, _, events = make_runner(tmp_path, cycle(), loot_reader=reader)
    runner.run()
    loot = next(event.loot for event in events if event.kind == "loot")
    ledger = LootLedger()
    ledger.record_round(1, loot)
    assert ledger.rounds == 1 and ledger.warning_rounds == 1
    assert ledger.coin_total == 0 and loot.warnings


def test_stop_on_loot_does_not_repeat_but_retains_earned_items(tmp_path):
    from gget_runner.loot import LootLedger
    reader, ledger = FakeLootReader(), LootLedger()
    runner = None

    def receive(event):
        if event.kind == "loot":
            ledger.record_round(event.completed, event.loot)
            runner.stop()

    runner, adb, events = make_runner(tmp_path, cycle(), receive, loot_reader=reader, infinite=True)
    runner.run()
    assert runner.completed == ledger.rounds == 1
    assert ledger.coin_total == 2000 and TAPS["reward"] not in adb.taps
    assert events[-1].message == "已停止"


def test_infinite_stops_only_on_explicit_request(tmp_path):
    runner = None

    def callback(event):
        if event.kind == "progress" and event.completed == 3:
            runner.stop()

    runner, adb, events = make_runner(tmp_path, cycle() + cycle(False) + cycle(False), callback, infinite=True)
    runner.run()
    assert runner.completed == 3
    assert adb.taps.count(TAPS["reward"]) == 2
    assert events[-1].message == "已停止"
    assert not any(e.kind == "error" for e in events)


@pytest.mark.parametrize("start", ["prepare", "sortie", "battle_intro"])
def test_supported_start_pages(tmp_path, start):
    pages = cycle()
    pages = pages[pages.index(start):]
    runner, _, events = make_runner(tmp_path, pages)
    runner.run()
    assert runner.completed == 1
    assert not any(e.kind == "error" for e in events)


@pytest.mark.parametrize("start", ["reward", "score", "experience"])
def test_unsupported_start_never_counts_or_clicks(tmp_path, start):
    runner, adb, events = make_runner(tmp_path, repeated(start))
    runner.run()
    assert runner.completed == 0 and adb.taps == []
    assert any(e.kind == "error" for e in events)
    assert len(list(tmp_path.glob("*.png"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_only_consecutive_stable_detections_trigger_action(tmp_path):
    pages = ["prepare", None, "prepare", "sortie", "prepare", "prepare"] + cycle()[2:]
    runner, adb, events = make_runner(tmp_path, pages)
    runner.run()
    assert adb.taps.count(TAPS["prepare"]) == 1
    assert runner.completed == 1
    state_frames = [index for index, event in enumerate(events) if event.kind == "state"]
    assert sum(event.kind == "frame" for event in events[:state_frames[0]]) == 6


def test_stale_page_is_not_clicked_again_and_times_out(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("sortie"), transition_timeout=0.025)
    runner.run()
    assert adb.taps == [TAPS["sortie"]]
    assert any(e.kind == "error" and "超时" in e.message for e in events)


def test_unknown_transition_times_out_without_clicking(tmp_path):
    runner, adb, events = make_runner(tmp_path, [None], transition_timeout=0.015)
    runner.run()
    assert adb.taps == []
    assert any(e.kind == "error" and "未知页面" in e.message for e in events)


def test_battle_has_longer_watchdog_and_no_input(tmp_path):
    pages = repeated("battle_intro") + repeated(None, copies=70) + repeated("score", "experience", "reward")
    runner, adb, events = make_runner(tmp_path, pages, transition_timeout=0.08, battle_timeout=0.7)
    runner.run()
    assert runner.completed == 1
    assert adb.taps == [TAPS[s] for s in ("battle_intro", "score", "experience")]
    assert not any(e.kind == "error" for e in events)


def test_battle_timeout_saves_snapshot(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("battle_intro") + [None], battle_timeout=0.02)
    runner.run()
    assert adb.taps == [TAPS["battle_intro"]]
    assert any(e.kind == "error" and "战斗结算超时" in e.message for e in events)
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["completed"] == 0 and report["screenshot"]


def test_sortie_can_skip_intro_only_after_confirmed_battle(tmp_path):
    pages = repeated("sortie", "battle") + repeated(None, copies=4) + repeated("score", "experience", "reward")
    runner, adb, events = make_runner(tmp_path, pages)
    runner.run()
    assert runner.completed == 1
    assert adb.taps == [TAPS[s] for s in ("sortie", "score", "experience")]
    assert any(e.kind == "state" and e.state == "battle" for e in events)
    assert not any(e.kind == "error" for e in events)


def test_repeat_can_skip_intro_after_confirmed_battle(tmp_path):
    pages = cycle() + repeated("reward", copies=4) + repeated("battle", copies=6) + repeated("score", "experience", "reward")
    runner, adb, events = make_runner(tmp_path, pages, rounds=2)
    runner.run()
    assert runner.completed == 2 and adb.taps.count(TAPS["reward"]) == 1
    assert adb.taps.count(TAPS["battle_intro"]) == 1
    assert TAPS["battle"] not in adb.taps
    assert [e.completed for e in events if e.kind == "progress"] == [1, 2]
    assert not any(e.kind == "error" for e in events)


def test_initial_confirmed_auto_battle_can_finish_once(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("battle", copies=7) + repeated(None) + repeated("score", "experience", "reward"))
    runner.run()
    assert runner.completed == 1
    assert adb.taps == [TAPS[s] for s in ("score", "experience")]
    assert [e.completed for e in events if e.kind == "progress"] == [1]
    assert not any(e.kind == "error" for e in events)


def test_recognized_battle_and_animation_use_long_watchdog(tmp_path):
    pages = repeated("sortie") + repeated("battle", copies=70) + repeated(None, copies=70) + repeated("score", "experience", "reward")
    runner, adb, events = make_runner(tmp_path, pages, transition_timeout=0.08, battle_timeout=0.9)
    runner.run()
    assert runner.completed == 1 and TAPS["battle"] not in adb.taps
    assert sum(e.kind == "state" and e.state == "battle" for e in events) == 1
    assert not any(e.kind == "error" for e in events)


def test_repeated_recognized_battle_does_not_reset_watchdog(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("battle"), transition_timeout=0.02, battle_timeout=0.06)
    runner.run()
    assert runner.completed == 0 and adb.taps == []
    assert any(e.kind == "error" and "战斗结算超时" in e.message for e in events)


def test_unknown_after_sortie_cannot_infer_battle(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("sortie") + [None], transition_timeout=0.03, battle_timeout=5)
    runner.run()
    assert runner.completed == 0 and adb.taps == [TAPS["sortie"]]
    assert not any(e.kind == "state" and e.state == "battle" for e in events)
    assert any(e.kind == "error" and "超时" in e.message for e in events)


def test_battle_cannot_skip_sortie_confirmation_from_prepare(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("prepare", "battle"), transition_timeout=0.03)
    runner.run()
    assert runner.completed == 0 and adb.taps == [TAPS["prepare"]]
    assert any(e.kind == "error" for e in events)


def test_stale_intro_uses_transition_timeout_not_battle_timeout(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("battle_intro"), transition_timeout=0.06, battle_timeout=10)
    before = time.monotonic()
    runner.run()
    assert time.monotonic() - before < 1
    assert adb.taps == [TAPS["battle_intro"]]
    assert any(e.kind == "error" and "未正常切换" in e.message for e in events)


def test_stop_between_confirming_and_input_prevents_tap(tmp_path):
    runner = None

    def callback(event):
        if event.kind == "state":
            runner.stop()

    runner, adb, events = make_runner(tmp_path, cycle(), callback)
    runner.run()
    assert adb.taps == [] and not runner.running
    assert events[-1].kind == "finished"


def test_pause_prevents_input_and_resume_resets_watchdog(tmp_path):
    runner = None
    did_pause = False
    paused_signal = threading.Event()
    callback_threads = []

    def callback(event):
        nonlocal did_pause
        callback_threads.append(threading.get_ident())
        if event.kind == "state" and not did_pause:
            did_pause = True
            runner.pause()
        if event.kind == "paused":
            paused_signal.set()

    runner, adb, events = make_runner(tmp_path, cycle(), callback, transition_timeout=0.03)
    runner.start()
    assert paused_signal.wait(1.0)
    time.sleep(0.06)
    assert runner.paused and adb.taps == []
    runner.resume()
    runner.join(1.0)
    assert not runner.running and runner.completed == 1
    assert set(callback_threads) == {runner._thread.ident}
    assert not any(e.kind == "error" for e in events)


def test_stop_while_paused_exits_promptly(tmp_path):
    runner = None
    signal = threading.Event()

    def callback(event):
        if event.kind == "state":
            runner.pause()
        if event.kind == "paused":
            signal.set()

    runner, adb, events = make_runner(tmp_path, cycle(), callback)
    runner.start()
    assert signal.wait(1.0)
    runner.stop()
    runner.join(0.3)
    assert not runner.running and adb.taps == []
    assert events[-1].kind == "finished"


def test_pause_after_reward_count_resumes_repeat_without_recount(tmp_path):
    runner = None
    signal = threading.Event()

    def callback(event):
        if event.kind == "progress" and event.completed == 1:
            runner.pause()
        if event.kind == "paused":
            signal.set()

    runner, adb, events = make_runner(tmp_path, cycle() + repeated("reward", copies=4) + cycle(False), callback, rounds=2)
    runner.start()
    assert signal.wait(1.0)
    assert runner.completed == 1 and TAPS["reward"] not in adb.taps
    runner.resume()
    runner.join(1.0)
    assert runner.completed == 2 and adb.taps.count(TAPS["reward"]) == 1
    assert not any(e.kind == "error" for e in events)


def test_pause_on_initial_battle_confirmation_resumes_without_input(tmp_path):
    runner = None
    signal = threading.Event()
    paused_once = False

    def callback(event):
        nonlocal paused_once
        if event.kind == "state" and event.state == "battle" and not paused_once:
            paused_once = True
            runner.pause()
        if event.kind == "paused":
            signal.set()

    runner, adb, events = make_runner(tmp_path, repeated("battle", copies=6) + repeated("score", "experience", "reward"), callback)
    runner.start()
    assert signal.wait(1)
    assert adb.taps == []
    runner.resume()
    runner.join(1)
    assert runner.completed == 1 and TAPS["battle"] not in adb.taps
    assert not any(e.kind == "error" for e in events)


def test_foreground_loss_before_tap_prevents_input(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle())
    original = runner.on_event

    def callback(event):
        original(event)
        if event.kind == "state":
            adb.foreground = False

    runner.on_event = callback
    runner.run()
    assert adb.taps == []
    assert any(e.kind == "error" and "前台" in e.message for e in events)


def scripted_focus(adb, outcomes):
    answers = iter(outcomes)
    def check():
        adb.checks += 1
        outcome = next(answers, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    adb.is_game_foreground = check


def test_foreground_polling_is_reduced_but_every_tap_is_checked(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle())
    runner.run()
    assert runner.completed == 1
    assert adb.checks == len(adb.taps) + 1
    assert not any(event.kind == 'error' for event in events)


def test_foreground_timeout_recovers_without_using_the_old_prepare_frame(tmp_path):
    pages = repeated('prepare', 'battle', 'score', 'experience', 'reward')
    runner, adb, events = make_runner(tmp_path, pages)
    scripted_focus(adb, [True, AdbTimeoutError('window timeout'), True])
    runner.run()
    assert runner.completed == 1
    assert adb.taps == [TAPS['score'], TAPS['experience']]
    assert any('前台检查超时' in event.message for event in events)
    assert not any(event.kind == 'error' for event in events)


def test_consecutive_foreground_timeouts_stop_after_three_checks(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle())
    scripted_focus(adb, [AdbTimeoutError('window timeout')] * 3)
    runner.run()
    assert adb.checks == 3 and not adb.taps and runner.completed == 0
    assert any(event.kind == 'error' and '连续3次' in event.message for event in events)


def test_reward_repeat_foreground_timeout_does_not_count_rewards_twice(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle() + repeated('reward', copies=4) + cycle(False), rounds=2)
    # Initial check, five first-round taps, then the repeat's focus check fails.
    scripted_focus(adb, [True] * 6 + [AdbTimeoutError('window timeout'), True])
    runner.run()
    assert runner.completed == 2 and adb.taps.count(TAPS['reward']) == 1
    assert [event.completed for event in events if event.kind == 'progress'] == [1, 2]
    assert not any(event.kind == 'error' for event in events)


def test_stop_during_foreground_timeout_recovery_sends_no_input(tmp_path):
    runner = None
    def callback(event):
        if '前台检查超时' in event.message:
            runner.stop()
    runner, adb, events = make_runner(tmp_path, cycle(), callback, poll_interval=10)
    scripted_focus(adb, [AdbTimeoutError('window timeout')])
    runner.run()
    assert adb.checks == 1 and not adb.taps
    assert events[-1].message == '已停止'
    assert not any(event.kind == 'error' for event in events)


def test_pause_during_foreground_recovery_resumes_with_a_new_check(tmp_path):
    signal = threading.Event()
    runner = None
    def callback(event):
        if '前台检查超时' in event.message:
            runner.pause()
        elif event.kind == 'paused':
            signal.set()
    runner, adb, events = make_runner(tmp_path, cycle(), callback)
    scripted_focus(adb, [AdbTimeoutError('window timeout'), True])
    runner.start()
    assert signal.wait(1) and runner.paused and not adb.taps
    runner.resume()
    runner.join(1)
    assert not runner.running and runner.completed == 1
    assert not any(event.kind == 'error' for event in events)


def test_foreground_recovery_does_not_consume_the_page_watchdog(tmp_path, monkeypatch):
    from gget_runner import runner as runner_module
    clock = [1000.0]
    monkeypatch.setattr(runner_module.time, 'monotonic', lambda: clock[0])
    runner, adb, events = make_runner(tmp_path, cycle(), transition_timeout=.05, battle_timeout=.05)
    runner._sleep = lambda: clock.__setitem__(0, clock[0] + .001)
    def check():
        adb.checks += 1
        if adb.checks == 1:
            clock[0] += 10
            raise AdbTimeoutError('window timeout')
        return True
    adb.is_game_foreground = check
    runner.run()
    assert runner.completed == 1
    assert not any(event.kind == 'error' for event in events)


@pytest.mark.parametrize('outcome', [False, AdbTimeoutError('window timeout')])
def test_stop_while_foreground_query_is_in_flight_does_not_report_an_error(tmp_path, outcome):
    runner, adb, events = make_runner(tmp_path, cycle())
    def check():
        adb.checks += 1
        runner.stop()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    adb.is_game_foreground = check
    runner.run()
    assert not adb.taps and events[-1].message == '已停止'
    assert not any(event.kind == 'error' for event in events)


def test_second_reward_repeat_timeout_recovers_before_watchdog_check(tmp_path, monkeypatch):
    from gget_runner import runner as runner_module
    clock = [1000.0]
    monkeypatch.setattr(runner_module.time, 'monotonic', lambda: clock[0])
    pages = cycle() + repeated('reward', copies=6) + cycle(False)
    runner, adb, events = make_runner(tmp_path, pages, rounds=2, transition_timeout=.05, battle_timeout=.05)
    runner._sleep = lambda: clock.__setitem__(0, clock[0] + .001)
    def check():
        adb.checks += 1
        if adb.checks in (7, 9):
            clock[0] += 10
            raise AdbTimeoutError('window timeout')
        return True
    adb.is_game_foreground = check
    runner.run()
    assert runner.completed == 2 and adb.taps.count(TAPS['reward']) == 1
    assert [event.completed for event in events if event.kind == 'progress'] == [1, 2]
    assert not any(event.kind == 'error' for event in events)


def test_pause_during_a_slow_foreground_query_preserves_the_watchdog(tmp_path, monkeypatch):
    from gget_runner import runner as runner_module
    clock = [1000.0]
    monkeypatch.setattr(runner_module.time, 'monotonic', lambda: clock[0])
    signal = threading.Event()
    def callback(event):
        if event.kind == 'paused':
            signal.set()
    runner, adb, events = make_runner(tmp_path, cycle(), callback, transition_timeout=.05, battle_timeout=.05)
    runner._sleep = lambda: clock.__setitem__(0, clock[0] + .001)
    def check():
        adb.checks += 1
        if adb.checks == 1:
            clock[0] += 10
            runner.pause()
        return True
    adb.is_game_foreground = check
    runner.start()
    assert signal.wait(1) and runner.paused and not adb.taps
    clock[0] += 100
    runner.resume()
    runner.join(1)
    assert not runner.running and runner.completed == 1
    assert not any(event.kind == 'error' for event in events)


def test_input_timeout_is_never_retried(tmp_path):
    runner, adb, events = make_runner(tmp_path, cycle())
    def tap(x, y):
        adb.taps.append((x, y))
        raise AdbTimeoutError('input outcome unknown')
    adb.tap = tap
    runner.run()
    assert adb.taps == [TAPS['prepare']]
    assert any(event.kind == 'error' and 'input outcome unknown' in event.message for event in events)


def test_start_twice_rejected_and_stop_interrupts_long_poll(tmp_path):
    runner, _, _ = make_runner(tmp_path, [None], poll_interval=10)
    runner.start()
    with pytest.raises(RuntimeError):
        runner.start()
    runner.stop()
    runner.join(0.3)
    assert not runner.running


def test_stop_during_capture_never_dispatches_a_pending_tap(tmp_path):
    runner, adb, events = make_runner(tmp_path, repeated("prepare"))
    entered = threading.Event()
    release = threading.Event()
    original = adb.screenshot

    def screenshot():
        entered.set()
        assert release.wait(1)
        return original()

    adb.screenshot = screenshot
    runner.start()
    assert entered.wait(1)
    runner.stop()
    release.set()
    runner.join(1)
    assert adb.taps == [] and not runner.running
    assert not any(e.kind == "error" for e in events)


def write_profile(tmp_path, method="gray", duplicated=False):
    rng = np.random.default_rng(412)
    patches = [rng.integers(0, 255, (12, 24, 3), dtype=np.uint8) for _ in range(3)]
    for index, patch in enumerate(patches):
        assert cv2.imwrite(str(tmp_path / f"a{index}.png"), patch)
    states = []
    for name, second in [("prepare", 1), ("sortie", 1 if duplicated else 2)]:
        states.append({"name": name, "tap": [0.75, 0.8], "templates": [
            {"name": "header", "path": "a0.png", "search_roi": [0.05, 0.05, 0.4, 0.4], "threshold": 0.78, "method": method},
            {"name": "button", "path": f"a{second}.png", "search_roi": [0.5, 0.5, 0.95, 0.95], "threshold": 0.78, "method": method},
        ]})
    (tmp_path / "profile.json").write_text(json.dumps({"schema_version": 1, "reference_size": [160, 90], "states": states}), encoding="utf-8")
    frame = rng.integers(0, 120, (90, 160, 3), dtype=np.uint8)
    frame[10:22, 15:39] = patches[0]
    frame[60:72, 100:124] = patches[1]
    return frame, patches


def test_detector_all_anchors_required_and_device_coordinates(tmp_path):
    frame, _ = write_profile(tmp_path)
    detector = ScreenDetector(tmp_path)
    result = detector.detect(cv2.resize(frame, (320, 180), interpolation=cv2.INTER_NEAREST))
    assert result.state == "prepare" and result.tap == (240, 144)
    assert result.confidence > 0.98
    frame[60:72, 100:124] = 0
    unknown = detector.detect(frame)
    assert unknown.state is None and unknown.tap is None


def test_detector_ignores_stage_art_outside_anchors(tmp_path):
    frame, _ = write_profile(tmp_path)
    detector = ScreenDetector(tmp_path)
    frame[30:45, :] = np.random.default_rng(73).integers(0, 255, (15, 160, 3), dtype=np.uint8)
    assert detector.detect(frame).state == "prepare"


@pytest.mark.parametrize("method", ["gray", "edge"])
def test_dimmed_modal_background_never_becomes_an_action(tmp_path, method):
    frame, _ = write_profile(tmp_path, method=method)
    result = ScreenDetector(tmp_path).detect((frame.astype(np.float32) * 0.5).astype(np.uint8))
    assert result.state is None and result.tap is None


def test_anchor_diagnostics_separate_shape_from_appearance_rejection(tmp_path):
    frame, _ = write_profile(tmp_path)
    detector = ScreenDetector(tmp_path)
    result = detector.detect(frame)
    anchor = next(m for m in result.details["matches"] if m["state"] == "prepare")["anchors"][0]
    assert anchor["correlation"] > 0.99
    assert anchor["contrast"] > 0.99 and anchor["luminance"] > 0.99
    assert anchor["contrast_ratio"] == pytest.approx(1.0, abs=0.001)
    assert anchor["mean_delta"] == pytest.approx(0.0, abs=0.001)
    dimmed = detector.detect((frame.astype(np.float32) * 0.5).astype(np.uint8))
    anchor = next(m for m in dimmed.details["matches"] if m["state"] == "prepare")["anchors"][0]
    assert anchor["raw_best"]["correlation"] > 0.99
    assert anchor["raw_best"]["contrast"] == pytest.approx(0.5, abs=0.02)
    assert anchor["raw_best"]["score"] < anchor["threshold"]
    assert anchor["score"] == pytest.approx(min(anchor["correlation"], anchor["contrast"], anchor["luminance"]), abs=0.001)


def pulse_profile(tmp_path, enabled=True):
    frame, _ = write_profile(tmp_path)
    path = tmp_path / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["states"][0]["name"] = "battle_intro"
    profile["states"][0]["templates"][1]["allow_pulse"] = enabled
    path.write_text(json.dumps(profile), encoding="utf-8")
    return frame


@pytest.mark.parametrize("brightness", [0.45, 0.65, 1.0])
def test_intro_label_pulse_keeps_shape_check_and_strict_companion(tmp_path, brightness):
    frame = pulse_profile(tmp_path)
    frame[60:72, 100:124] = (frame[60:72, 100:124].astype(np.float32) * brightness + (12 if brightness < 1 else 0)).astype(np.uint8)
    result = ScreenDetector(tmp_path).detect(frame)
    assert result.state == "battle_intro", result.details
    anchors = next(match for match in result.details["matches"] if match["state"] == "battle_intro")["anchors"]
    button = next(anchor for anchor in anchors if anchor["name"] == "button")
    header = next(anchor for anchor in anchors if anchor["name"] == "header")
    assert button["allow_pulse"] and button["correlation"] > 0.99
    assert not header["allow_pulse"] and header["score"] > 0.99


def test_intro_pulse_is_opt_in(tmp_path):
    frame = pulse_profile(tmp_path, enabled=False)
    frame[60:72, 100:124] = (frame[60:72, 100:124].astype(np.float32) * 0.65).astype(np.uint8)
    result = ScreenDetector(tmp_path).detect(frame)
    assert result.state is None and result.tap is None


def test_intro_pulse_rejects_full_frame_dialog_dimming(tmp_path):
    frame = pulse_profile(tmp_path)
    result = ScreenDetector(tmp_path).detect((frame.astype(np.float32) * 0.5).astype(np.uint8))
    assert result.state is None and result.tap is None
    anchors = next(match for match in result.details["matches"] if match["state"] == "battle_intro")["anchors"]
    assert next(a for a in anchors if a["name"] == "header")["score"] < 0.78


def test_intro_pulse_still_requires_visible_text_and_companion(tmp_path):
    frame = pulse_profile(tmp_path)
    detector = ScreenDetector(tmp_path)
    invisible = frame.copy()
    invisible[60:72, 100:124] = (invisible[60:72, 100:124].astype(np.float32) * 0.1).astype(np.uint8)
    result = detector.detect(invisible)
    assert result.state is None and result.tap is None
    missing_header = frame.copy()
    missing_header[10:22, 15:39] = 0
    assert detector.detect(missing_header).state is None
    wrong_shape = frame.copy()
    wrong_shape[60:72, 100:124] = np.random.default_rng(38).integers(0, 255, (12, 24, 3), dtype=np.uint8)
    assert detector.detect(wrong_shape).state is None


@pytest.mark.parametrize("state", ["prepare", "sortie", "score", "experience", "reward", "battle"])
def test_allow_pulse_rejected_on_other_pages(tmp_path, state):
    pulse_profile(tmp_path)
    path = tmp_path / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["states"][0]["name"] = state
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="allow_pulse 只允许"):
        ScreenDetector(tmp_path)


@pytest.mark.parametrize("misuse", ["header", "single_anchor", "both_anchors", "not_boolean"])
def test_allow_pulse_rejects_unsafe_intro_configuration(tmp_path, misuse):
    pulse_profile(tmp_path)
    path = tmp_path / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    anchors = profile["states"][0]["templates"]
    if misuse == "header":
        anchors[0]["allow_pulse"] = True
        anchors[1]["allow_pulse"] = False
    elif misuse == "single_anchor":
        profile["states"][0]["templates"] = [anchors[1]]
    elif misuse == "both_anchors":
        anchors[0]["name"] = "action"
        anchors[0]["allow_pulse"] = True
    else:
        anchors[1]["allow_pulse"] = "yes"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError):
        ScreenDetector(tmp_path)


def copy_actual_profile(tmp_path, enable_pulse):
    root = Path(__file__).resolve().parents[1]
    source = root / "assets" / "profiles" / "default"
    profile = json.loads((source / "profile.json").read_text(encoding="utf-8-sig"))
    for page in profile["states"]:
        for anchor in page["templates"]:
            shutil.copyfile(source / anchor["path"], tmp_path / anchor["path"])
            if page["name"] == "battle_intro" and anchor["name"] == "button":
                anchor["allow_pulse"] = enable_pulse
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return root


def test_native_intro_pulse_fixture_detects_prompt_and_rejects_dimmed_modal(tmp_path):
    root = copy_actual_profile(tmp_path, enable_pulse=True)
    frame_path = root / "tests" / "fixtures" / "live_intro.png"
    frame = cv2.imdecode(np.frombuffer(frame_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    result = ScreenDetector(tmp_path).detect(frame)
    assert result.state == "battle_intro" and result.tap == (640, 648), result.details
    assert result.confidence > 0.9
    intro = next(m for m in result.details["matches"] if m["state"] == "battle_intro")
    button = next(a for a in intro["anchors"] if a["name"] == "button")
    assert button["correlation"] > 0.95 and button["contrast_ratio"] < 0.76
    dimmed = ScreenDetector(tmp_path).detect((frame.astype(np.float32) * 0.5).astype(np.uint8))
    assert dimmed.state is None and dimmed.tap is None
    copy_actual_profile(tmp_path, enable_pulse=False)
    assert ScreenDetector(tmp_path).detect(frame).state is None


def test_native_pulsing_intro_is_clicked_once_then_battle_receives_no_input(tmp_path):
    root = copy_actual_profile(tmp_path, enable_pulse=True)
    detector = ScreenDetector(tmp_path)

    def load(path):
        frame = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        assert frame is not None
        return frame

    intro = load(root / "tests" / "fixtures" / "live_intro.png")
    battle = load(root / "tests" / "fixtures" / "references" / "battle.png")
    assert detector.detect(intro).state == "battle_intro"
    assert detector.detect(battle).state == "battle"
    score = load(root / "tests" / "fixtures" / "references" / "score.png")
    experience = load(root / "tests" / "fixtures" / "references" / "experience.png")
    reward = load(root / "tests" / "fixtures" / "references" / "reward.png")
    frames = [intro] * 4 + [battle] * 4 + [score] * 2 + [experience] * 2 + [reward] * 2
    adb = FakeAdb([None])

    def screenshot():
        frame = frames[min(adb.position, len(frames) - 1)]
        adb.position += 1
        return frame.copy()

    adb.screenshot = screenshot
    events = []
    runner = BotRunner(adb, detector, config(tmp_path, transition_timeout=3, battle_timeout=5), events.append)
    runner.run()
    assert runner.completed == 1
    assert adb.taps == [detector.detect(frame).tap for frame in (intro, score, experience)]
    assert adb.taps.count((640, 648)) == 1
    assert [event.completed for event in events if event.kind == "progress"] == [1]
    assert not any(event.kind == "error" for event in events)


def test_detector_multiscale_and_edge(tmp_path):
    frame, patches = write_profile(tmp_path, method="edge")
    frame[10:22, 15:39] = 0
    frame[60:72, 100:124] = 0
    for patch, x, y in [(patches[0], 15, 10), (patches[1], 100, 60)]:
        resized = cv2.resize(patch, (26, 13))
        frame[y:y + 13, x:x + 26] = resized
    result = ScreenDetector(tmp_path).detect(frame)
    assert result.state == "prepare"
    matched = next(match for match in result.details["matches"] if match["matched"])
    assert any(anchor["scale"] != 1.0 for anchor in matched["anchors"])


def test_detector_ambiguous_or_wrong_aspect_never_supplies_tap(tmp_path):
    frame, _ = write_profile(tmp_path, duplicated=True)
    detector = ScreenDetector(tmp_path)
    result = detector.detect(frame)
    assert result.state is None and result.tap is None
    assert "多个页面" in result.details["reason"]
    result = detector.detect(np.zeros((160, 90, 3), dtype=np.uint8))
    assert result.state is None and result.tap is None
    assert "宽高比" in result.details["reason"]


def test_detector_rejects_constant_templates(tmp_path):
    write_profile(tmp_path)
    cv2.imwrite(str(tmp_path / "a0.png"), np.zeros((12, 24, 3), dtype=np.uint8))
    with pytest.raises(ValueError, match="特征"):
        ScreenDetector(tmp_path)


@pytest.mark.parametrize("tap", [None, [0.5, 0.5]])
def test_detector_battle_never_returns_input_coordinates(tmp_path, tap):
    frame, _ = write_profile(tmp_path)
    path = tmp_path / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["states"][0]["name"] = "battle"
    profile["states"][0]["tap"] = tap
    path.write_text(json.dumps(profile), encoding="utf-8")
    result = ScreenDetector(tmp_path).detect(frame)
    assert result.state == "battle" and result.tap is None


def test_detector_disallows_missing_tap_on_action_page(tmp_path):
    write_profile(tmp_path)
    path = tmp_path / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["states"][0]["tap"] = None
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="只有 battle"):
        ScreenDetector(tmp_path)


@pytest.mark.parametrize("state", ["prepare", "sortie", "battle_intro", "score", "experience", "reward"])
def test_real_game_references_identify_each_page_after_background_change(state):
    root = Path(__file__).resolve().parents[1]
    profile_dir = root / "assets" / "profiles" / "default"
    frame_path = root / "tests" / "fixtures" / "references" / f"{state}.png"
    frame = cv2.imdecode(np.frombuffer(frame_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None and frame.shape[:2] == (720, 1280)
    detector = ScreenDetector(profile_dir)
    original = detector.detect(frame)
    assert original.state == state, original.details
    profile = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8-sig"))
    page = next(item for item in profile["states"] if item["name"] == state)
    keep = np.zeros(frame.shape[:2], dtype=bool)
    for anchor in page["templates"]:
        left, top, right, bottom = anchor["search_roi"]
        keep[math.floor(top * 720):math.ceil(bottom * 720),
             math.floor(left * 1280):math.ceil(right * 1280)] = True
    changed = np.random.default_rng(916).integers(0, 255, frame.shape, dtype=np.uint8)
    changed[keep] = frame[keep]
    result = detector.detect(changed)
    assert result.state == state and result.tap == original.tap, result.details
    # Template confidence also excludes artwork outside the two fixed ROIs.
    assert result.confidence == pytest.approx(original.confidence, abs=0.002)


def test_adb_arguments_binary_screenshot_and_device_parsing(monkeypatch):
    calls = []
    ok, encoded = cv2.imencode(".png", np.full((20, 40, 3), 77, dtype=np.uint8))
    assert ok

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1:] == ["devices", "-l"]:
            output = b"List of devices attached\nemulator-5554 device product:ld model:LDPlayer device:ld\nlocked unauthorized\n"
        elif "screencap" in arguments:
            output = encoded.tobytes()
        elif "dumpsys" in arguments:
            output = f"mCurrentFocus=Window{{123 u0 {GAME_PACKAGE}/.MainActivity}}".encode()
        else:
            output = b""
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    devices = AdbClient.list_devices("C:/adb path/adb.exe")
    assert [(d.serial, d.state, d.model) for d in devices] == [("emulator-5554", "device", "LDPlayer"), ("locked", "unauthorized", "")]
    adb = AdbClient("C:/adb path/adb.exe", "emulator-5554")
    assert adb.screenshot().shape == (20, 40, 3)
    assert adb.is_game_foreground()
    adb.tap(123, 456)
    assert calls[-1][0] == [str(Path("C:/adb path/adb.exe")), "-s", "emulator-5554", "shell", "input", "tap", "123", "456"]
    assert all("shell" not in kwargs and kwargs["timeout"] <= 15 for _, kwargs in calls)


@pytest.mark.parametrize("focus", ["null", "Window{u0 com.android.systemui/.Dialog}", f"Window{{u0 {GAME_PACKAGE}.other/.Main}}"])
def test_adb_foreground_does_not_match_background_or_package_prefix(monkeypatch, focus):
    output = f"mCurrentFocus={focus}\nmFocusedApp=App{{u0 {GAME_PACKAGE}/.Main}}".encode()
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, b""))
    assert not AdbClient("adb", "emulator-5554").is_game_foreground()


def test_adb_timeout_and_bad_screenshot_fail_closed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, b"not PNG", b""))
    with pytest.raises(AdbError, match="PNG"):
        AdbClient("adb", "serial").screenshot()

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AdbError, match="执行失败"):
        AdbClient.list_devices("adb")
