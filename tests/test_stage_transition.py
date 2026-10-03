"""Run the reported new-stage screenshot through the complete input workflow."""
from pathlib import Path

import cv2
import numpy as np

from gget_runner.runner import BotRunner, RunConfig
from gget_runner.vision import ScreenDetector


ROOT = Path(__file__).resolve().parents[1]


def test_space_sortie_fixture_completes_one_round_without_transition_timeout(tmp_path):
    detector = ScreenDetector(ROOT / "assets" / "profiles" / "default")
    names = ["prepare", "sortie-space", "battle_intro", "battle", "score", "experience", "reward"]
    frames = []
    for name in names:
        path = ROOT / "tests" / "fixtures" / "references" / (name + ".png")
        frame = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        assert frame is not None
        frames.extend([frame, frame])

    class FixtureAdb:
        position = 0
        taps = []

        def is_game_foreground(self):
            return True

        def screenshot(self):
            frame = frames[min(self.position, len(frames) - 1)]
            self.position += 1
            return frame

        def tap(self, x, y):
            self.taps.append((x, y))

    adb, events = FixtureAdb(), []
    runner = BotRunner(adb, detector, RunConfig(rounds=1, poll_interval=.001, action_cooldown=0,
                        transition_timeout=5, battle_timeout=5, diagnostics_dir=tmp_path), events.append)
    runner.run()
    assert runner.completed == 1
    assert not any(event.kind == "error" for event in events)
    assert adb.taps == [detector.detect(frames[index * 2]).tap for index in (0, 1, 2, 4, 5)]
    assert events[-1].kind == "finished" and "目标" in events[-1].message
