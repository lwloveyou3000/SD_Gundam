from __future__ import annotations

from types import SimpleNamespace
from io import BytesIO
import threading
import tkinter as tk

import numpy as np
import pytest
from PIL import Image, ImageTk

from gget_runner.adb import Device
from gget_runner.runner import RunEvent
from gget_runner.loot import LootDrop, LootSnapshot
from gget_runner.ui import RunnerWindow


@pytest.fixture
def window(tmp_path):
    root = tk.Tk()
    # Native widget geometry can be checked without showing a window to the user.
    root.attributes('-alpha', 0.0)
    root.withdraw()
    app = RunnerWindow(root, tmp_path)
    root.update_idletasks()
    yield app
    app._closing = True
    root.destroy()


def test_launch_is_idle_and_never_starts_adb(window):
    assert not window._active
    assert window._runner is None
    assert window.events.empty()
    assert window.start_button.instate(['!disabled'])
    assert window.pause_button.instate(['disabled'])
    assert window.stop_button.instate(['disabled'])


def test_device_refresh_chooses_online_device_and_preserves_serial(window):
    window._handle_event('devices', ([Device('emulator-5554', 'device', 'Xiaomi'),
                                     Device('offline-device', 'offline', '')], ''))
    assert window._selection() == 'emulator-5554'
    assert len(window.device_combo['values']) == 1
    assert window.status.get() == '设备已连接'


def test_finite_infinite_controls_and_invalid_finite_count(window):
    window.rounds.set('3')
    assert window._snapshot().rounds == 3
    assert '0 / 3' in window.count.get()
    window.infinite.set(True)
    window._mode_changed()
    assert window.rounds_entry.instate(['disabled'])
    assert '无限' in window.count.get()
    window.rounds.set('')
    assert window._snapshot().infinite
    window.infinite.set(False)
    window._mode_changed()
    with pytest.raises(ValueError):
        window._snapshot()


def test_completion_unlocks_controls_and_keeps_exact_count(window):
    window._active = True
    window._target = 2
    window._runner = SimpleNamespace()
    window._runner_event(RunEvent('progress', '第一轮', 'reward', 1))
    assert '1 / 2' in window.count.get()
    window._runner_event(RunEvent('finished', '目标已完成', 'reward', 2))
    assert window.status.get() == '目标已完成'
    assert '2 / 2' in window.count.get()
    assert not window._active
    assert window.start_button.instate(['!disabled'])
    assert window.stop_button.instate(['disabled'])


def test_background_events_only_change_tk_when_main_thread_drains(window):
    window._active = True
    window._target = 4
    thread = threading.Thread(target=lambda: window._post('runner', RunEvent('progress', '', 'reward', 2)))
    thread.start()
    thread.join()
    assert window._completed == 0
    kind, payload = window.events.get_nowait()
    window._handle_event(kind, payload)
    assert window._completed == 2
    assert '2 / 4' in window.count.get()


def test_read_only_inspection_does_not_count_loot_or_keep_preview(window):
    detection = SimpleNamespace(state='prepare', confidence=0.95)
    window._handle_event('inspection', (detection, True))
    assert window.status.get() == '页面已检查'
    assert '关卡准备' in window.page.get()
    assert window.loot_ledger.rounds == 0
    assert not window.loot_tree.get_children()
    for removed in ('canvas', '_frame_image', '_photo', '_render_preview', '_schedule_preview'):
        assert not hasattr(window, removed)
    assert window._runner is None
    assert not window._active


def snapshot(*drops, warnings=()):
    return LootSnapshot(tuple(drops), tuple(warnings))


def drop(item_id='item01', quantity=2, name='道具01', icon_png=b''):
    return LootDrop(item_id, name, quantity, icon_png)


def loot_event(round_number, loot):
    return RunEvent('loot', '', 'reward', round_number, loot=loot)


def test_loot_aggregates_cards_deduplicates_round_and_shows_current_round(window):
    first = snapshot(drop(), drop(quantity=3), drop('coin', 2000, '金币'))
    window._runner_event(loot_event(1, first))
    window._runner_event(loot_event(1, first))
    assert window.loot_ledger.rounds == 1
    assert window.coin_total.get() == '金币累计：2,000'
    assert window.loot_tree.item('item01', 'values') == ('道具01', '5', '5', '1')
    window._runner_event(loot_event(2, snapshot(drop('item02', 1, '道具02'))))
    assert window.loot_tree.item('item01', 'values') == ('道具01', '0', '5', '1')
    assert window.loot_rounds.get() == '统计轮数：2'


def test_unknown_quantities_are_visible_and_full_card_icons_are_kept(window):
    png = BytesIO()
    card = Image.new('RGB', (106, 106), 'blue')
    card.paste('red', (0, 0, 106, 6))
    card.paste('green', (0, 100, 106, 106))
    card.save(png, format='PNG')
    window._runner_event(loot_event(1, snapshot(drop(quantity=2, icon_png=png.getvalue()),
                                              drop(quantity=None), warnings=('数量无法确认',))))
    assert window.loot_tree.item('item01', 'values') == ('道具01', '待确认', '2 + 待确认', '1')
    assert window.warning_rounds.get() == '待确认轮数：1'
    icon = window._loot_icons['item01']
    assert (icon.width(), icon.height()) == (52, 52)
    shown = ImageTk.getimage(icon)
    assert shown.getpixel((26, 0))[0] > 240
    assert shown.getpixel((26, 51))[1] > 120
    assert window.loot_tree.item('item01', 'image')


def test_frame_events_are_dropped_before_queueing(window):
    window._post('runner', RunEvent('frame', '', '', 0, frame=np.zeros((720, 1280, 3), dtype=np.uint8)))
    assert window.events.empty()
    window._runner_event(RunEvent('frame', '', '', 8))
    assert window._completed == 0


def test_new_start_clears_stats_and_pause_resume_stop_preserve_them(window, monkeypatch):
    window._runner_event(loot_event(1, snapshot(drop())))
    window._active = True
    window._runner = SimpleNamespace()
    window._runner_event(RunEvent('paused', '', '', 1))
    assert window._paused
    assert window.loot_ledger.rounds == 1
    window._runner_event(RunEvent('resumed', '', '', 1))
    assert not window._paused
    assert window.loot_ledger.rounds == 1
    window._runner_event(RunEvent('finished', '', '', 1))
    assert window.loot_ledger.rounds == 1
    window.device.set('emulator-5554')
    monkeypatch.setattr(window, '_background', lambda work: None)
    window._start()
    assert window.loot_ledger.rounds == 0
    assert window.coin_total.get() == '金币累计：0'
    assert not window.loot_tree.get_children()
    assert not window._loot_icons


def test_start_initializes_reader_in_background_and_passes_it_to_runner(window, monkeypatch):
    from gget_runner import ui
    jobs = []
    calls = []
    reader = object()
    monkeypatch.setattr(window, '_background', jobs.append)
    monkeypatch.setattr(window, '_persist', lambda settings: None)
    monkeypatch.setattr(ui, 'AdbClient', lambda *_args: SimpleNamespace(is_game_foreground=lambda: True))
    monkeypatch.setattr(ui, 'ScreenDetector', lambda *_args: object())
    def create_reader():
        calls.append(('reader', threading.current_thread().name))
        return reader
    def create_runner(*args, **kwargs):
        calls.append(('runner', kwargs['loot_reader']))
        return SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(ui, 'LootReader', create_reader)
    monkeypatch.setattr(ui, 'BotRunner', create_runner)
    window.device.set('emulator-5554')
    window._start()
    assert calls == []
    assert len(jobs) == 1
    worker = threading.Thread(target=jobs[0], name='preflight-test')
    worker.start()
    worker.join()
    assert calls == [('reader', 'preflight-test'), ('runner', reader)]


def test_read_only_background_queues_page_metadata_without_frame(window, monkeypatch):
    from gget_runner import ui
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    detection = SimpleNamespace(state='reward', confidence=0.98)
    monkeypatch.setattr(window, '_background', lambda work: work())
    monkeypatch.setattr(window, '_persist', lambda settings: None)
    monkeypatch.setattr(ui, 'AdbClient', lambda *_args: SimpleNamespace(
        is_game_foreground=lambda: True, screenshot=lambda: image))
    monkeypatch.setattr(ui, 'ScreenDetector', lambda *_args: SimpleNamespace(detect=lambda frame: detection))
    monkeypatch.setattr(ui, 'LootReader', lambda: pytest.fail('只读检查不应初始化战利品识别器'))
    window.device.set('emulator-5554')
    window._read_screen()
    kind, payload = window.events.get_nowait()
    assert kind == 'inspection'
    assert payload == (detection, True)
    window._handle_event(kind, payload)
    assert window.loot_ledger.rounds == 0
    assert window._completed == 0
    assert '奖励结算' in window.page.get()


@pytest.mark.parametrize('size, previous_heights', [((1120, 900), (284, 172)), ((950, 700), (204, 92))])
def test_main_controls_and_loot_fit_window(window, size, previous_heights):
    window.root.geometry(f'{size[0]}x{size[1]}')
    window.root.deiconify()
    window.root.update()
    window.root.update_idletasks()
    width, height = window.root.winfo_width(), window.root.winfo_height()
    root_x, root_y = window.root.winfo_rootx(), window.root.winfo_rooty()
    for control in (window.start_button, window.pause_button, window.resume_button,
                    window.stop_button, window.read_button, window.refresh_button,
                    window.rounds_entry, window.infinite_check, window.loot_tree, window.log_text,
                    window.start_hint, window.runtime_hint):
        x, y = control.winfo_rootx() - root_x, control.winfo_rooty() - root_y
        assert x >= 0 and y >= 0
        assert x + control.winfo_width() <= width
        assert y + control.winfo_height() <= height
    assert window.loot_tree.winfo_height() > previous_heights[0]
    assert window.log_text.winfo_height() > previous_heights[1]
    assert window.start_hint.winfo_y() == window.runtime_hint.winfo_y()
    assert window.runtime_hint.winfo_x() >= window.start_hint.winfo_x() + window.start_hint.winfo_width()
    assert window.runtime_hint.winfo_rooty() < window.path_entry.winfo_rooty()
    body = window.start_hint.master.master
    assert not body.grid_slaves(row=5)
