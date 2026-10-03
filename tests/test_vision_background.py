"""Title strokes must survive stage changes while covered UI never grants a tap."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

import cv2
import numpy as np
import pytest

from gget_runner.vision import ScreenDetector


ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "assets" / "profiles" / "default"
REFERENCE_DIR = ROOT / "tests" / "fixtures" / "references"


def load_frame(name):
    frame = cv2.imdecode(np.frombuffer((REFERENCE_DIR / f"{name}.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None
    return frame


@pytest.fixture(scope="module")
def detector():
    return ScreenDetector(PROFILE_DIR)


def roi_slice(state, anchor_name):
    profile = json.loads((PROFILE_DIR / "profile.json").read_text(encoding="utf-8-sig"))
    page = next(page for page in profile["states"] if page["name"] == state)
    anchor = next(anchor for anchor in page["templates"] if anchor["name"] == anchor_name)
    left, top, right, bottom = anchor["search_roi"]
    return (slice(math.floor(top * 720), math.ceil(bottom * 720)),
            slice(math.floor(left * 1280), math.ceil(right * 1280)))


def assert_no_action(detector, frame):
    result = detector.detect(frame)
    assert result.state is None and result.tap is None, result.details


@pytest.mark.parametrize("name", ["sortie", "sortie-space"])
@pytest.mark.parametrize("size", [(1280, 720), (640, 360), (960, 540), (1920, 1080)])
def test_real_stage_backgrounds_identify_unique_sortie_with_same_coordinates(detector, name, size):
    result = detector.detect(cv2.resize(load_frame(name), size))
    assert result.state == "sortie", result.details
    assert result.tap == (round(size[0] * .874), round(size[1] * .916))
    assert sum(match["matched"] for match in result.details["matches"]) == 1
    if size == (1280, 720):
        assert result.tap == (1119, 660)
    matched = next(match for match in result.details["matches"] if match["matched"])
    title = next(anchor for anchor in matched["anchors"] if anchor["name"] == "header")
    button = next(anchor for anchor in matched["anchors"] if anchor["name"] == "button")
    assert title["method"] == "white_text" and title["foreground_luminance"] >= .88
    assert button["method"] == "gray" and not button["allow_pulse"]


@pytest.mark.parametrize("name", ["prepare", "sortie", "sortie-space"])
@pytest.mark.parametrize("brightness", [.6, .8])
@pytest.mark.parametrize("dim_title_only", [False, True])
def test_stage_independent_titles_reject_modal_dimming(detector, name, brightness, dim_title_only):
    frame = load_frame(name)
    if dim_title_only:
        area = roi_slice("prepare" if name == "prepare" else "sortie", "header")
        frame[area] = (frame[area].astype(np.float32) * brightness).astype(np.uint8)
    else:
        frame = (frame.astype(np.float32) * brightness).astype(np.uint8)
    assert_no_action(detector, frame)


@pytest.mark.parametrize("anchor", ["header", "button"])
@pytest.mark.parametrize("name", ["sortie", "sortie-space"])
def test_covered_title_or_button_cannot_sortie(detector, name, anchor):
    frame = load_frame(name)
    frame[roi_slice("sortie", anchor)] = 0
    assert_no_action(detector, frame)


@pytest.mark.parametrize("name", ["sortie", "sortie-space"])
def test_partial_title_occlusion_and_low_contrast_cannot_sortie(detector, name):
    frame = load_frame(name)
    # Cover the first character while leaving the other three and the button.
    frame[8:58, 145:190] = 0
    assert_no_action(detector, frame)
    frame = load_frame(name)
    area = roi_slice("sortie", "header")
    frame[area] = (frame[area].astype(np.float32) * .15 + 170).astype(np.uint8)
    assert_no_action(detector, frame)


def test_black_frame_and_wrong_aspect_never_grant_sortie(detector):
    assert_no_action(detector, np.zeros((720, 1280, 3), dtype=np.uint8))
    assert_no_action(detector, cv2.resize(load_frame("sortie-space"), (1280, 800)))


@pytest.mark.parametrize("anchor", ["header", "button"])
def test_single_visible_sortie_anchor_is_insufficient(detector, anchor):
    original = load_frame("sortie-space")
    frame = np.zeros_like(original)
    area = roi_slice("sortie", anchor)
    frame[area] = original[area]
    assert_no_action(detector, frame)


def test_prepare_title_also_survives_dark_stage_background(detector):
    frame = load_frame("prepare")
    area = roi_slice("prepare", "header")
    title = frame[area]
    # Preserve bright neutral strokes and their antialiasing while replacing
    # the backdrop with a dark stage. The strict button stays unchanged.
    pixels = title.astype(np.float32)
    strokes = (pixels.min(axis=2) > 180) & (np.ptp(pixels, axis=2) < 35)
    title[~strokes] = (15, 9, 5)
    result = detector.detect(frame)
    assert result.state == "prepare" and result.tap == (1119, 593), result.details


@pytest.mark.parametrize("misuse", ["wrong_page", "wrong_anchor", "weak_threshold", "missing_button", "no_white_strokes"])
def test_white_title_configuration_stays_restricted(tmp_path, misuse):
    profile = json.loads((PROFILE_DIR / "profile.json").read_text(encoding="utf-8-sig"))
    for page in profile["states"]:
        for anchor in page["templates"]:
            shutil.copyfile(PROFILE_DIR / anchor["path"], tmp_path / anchor["path"])
    page = profile["states"][0]
    title = page["templates"][1]
    if misuse == "wrong_page":
        page["name"] = "other"
    elif misuse == "wrong_anchor":
        title["name"] = "action"
    elif misuse == "weak_threshold":
        title["threshold"] = .72
    elif misuse == "missing_button":
        page["templates"] = [title]
    else:
        assert cv2.imwrite(str(tmp_path / title["path"]), np.zeros((50, 235, 3), dtype=np.uint8))
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError):
        ScreenDetector(tmp_path)
