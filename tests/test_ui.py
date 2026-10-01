from __future__ import annotations

from types import SimpleNamespace
import threading
import tkinter as tk

import numpy as np
import pytest

from gget_runner.adb import Device
from gget_runner.runner import RunEvent
from gget_runner.ui import RunnerWindow


@pytest.fixture
def window(tmp_path):
    root = tk.Tk()
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


def test_read_only_preview_preserves_frame_ratio(window):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    detection = SimpleNamespace(state='prepare', confidence=0.95)
    window._handle_event('inspection', (image, detection, True))
    assert window.status.get() == '画面已读取'
    assert window._frame_image.size == (1280, 720)
    assert window._runner is None
    assert not window._active


def test_main_controls_fit_default_window(window):
    window.root.deiconify()
    window.root.update_idletasks()
    width, height = window.root.winfo_width(), window.root.winfo_height()
    root_x, root_y = window.root.winfo_rootx(), window.root.winfo_rooty()
    for control in (window.start_button, window.pause_button, window.resume_button,
                    window.stop_button, window.read_button, window.refresh_button,
                    window.rounds_entry, window.infinite_check):
        x, y = control.winfo_rootx() - root_x, control.winfo_rooty() - root_y
        assert x >= 0 and y >= 0
        assert x + control.winfo_width() <= width
        assert y + control.winfo_height() <= height
