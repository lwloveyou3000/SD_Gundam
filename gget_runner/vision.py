"""Conservative fixed-anchor matching, independent of stage and unit artwork."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class Detection:
    state: str | None
    confidence: float
    tap: tuple[int, int] | None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Anchor:
    name: str
    roi: tuple[float, float, float, float]
    threshold: float
    method: str
    allow_pulse: bool
    variants: tuple[tuple[float, np.ndarray, np.ndarray], ...]


def _gray(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _edge(gray: np.ndarray) -> np.ndarray:
    return cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 140)


class ScreenDetector:
    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir).resolve()
        profile = json.loads((self.profile_dir / "profile.json").read_text(encoding="utf-8-sig"))
        if profile.get("schema_version") != 1:
            raise ValueError("不支持的识别配置版本")
        size = profile.get("reference_size", [])
        if len(size) != 2 or any(not isinstance(v, int) or v < 1 for v in size):
            raise ValueError("reference_size 必须包含两个正整数")
        self.reference_size = tuple(size)
        self.states: list[dict] = []
        seen = set()
        for item in profile.get("states", []):
            name = item["name"]
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError("页面名称无效或重复")
            seen.add(name)
            raw_tap = item["tap"]
            if raw_tap is None:
                if name != "battle":
                    raise ValueError(f"只有 battle 页面可以没有点击位置：{name}")
                tap = None
            else:
                tap = tuple(raw_tap)
                if len(tap) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in tap):
                    raise ValueError(f"{name} 的点击位置无效")
            # A battle profile never grants an input coordinate, even if an
            # edited profile accidentally contains one.
            if name == "battle":
                tap = None
            anchors = []
            for template in item.get("templates", []):
                anchor_name = template.get("name", Path(template["path"]).stem)
                allow_pulse = template.get("allow_pulse", False)
                if not isinstance(allow_pulse, bool):
                    raise ValueError("allow_pulse 必须是布尔值")
                if allow_pulse and (name != "battle_intro" or anchor_name not in {"button", "action"}):
                    raise ValueError("allow_pulse 只允许用于 battle_intro 的 button/action 文字模板")
                path = (self.profile_dir / template["path"]).resolve()
                if not path.is_relative_to(self.profile_dir):
                    raise ValueError("模板必须位于配置目录内")
                loaded = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
                if loaded is None:
                    raise ValueError(f"无法读取模板：{path}")
                method = template.get("method", "gray")
                if method not in {"gray", "edge"}:
                    raise ValueError(f"不支持的匹配方法：{method}")
                roi = tuple(template["search_roi"])
                if (len(roi) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in roi)
                        or roi[0] >= roi[2] or roi[1] >= roi[3]):
                    raise ValueError(f"{name} 的搜索区域无效")
                threshold = float(template.get("threshold", 0.85))
                if not math.isfinite(threshold) or not 0 < threshold <= 1:
                    raise ValueError("匹配阈值必须介于 0 和 1 之间")
                variants = []
                for scale in (0.90, 0.95, 1.0, 1.05, 1.10):
                    width = max(2, round(loaded.shape[1] * scale))
                    height = max(2, round(loaded.shape[0] * scale))
                    resized = cv2.resize(loaded, (width, height), interpolation=cv2.INTER_LINEAR)
                    pixels = _gray(resized)
                    gray_pixels = pixels
                    if method == "edge":
                        pixels = _edge(pixels)
                        # Ignore the crop perimeter: Canny there sees different
                        # neighbours than the same text in a complete screenshot.
                        if min(pixels.shape) <= 5:
                            raise ValueError(f"边缘模板太小：{path.name}")
                        pixels = pixels[2:-2, 2:-2]
                        gray_pixels = gray_pixels[2:-2, 2:-2]
                    # Constant templates produce misleading perfect normalized matches.
                    if float(pixels.std()) < 1.0:
                        raise ValueError(f"模板没有足够的文字/边缘特征：{path.name}")
                    variants.append((scale, pixels, gray_pixels))
                anchors.append(_Anchor(anchor_name, roi, threshold, method, allow_pulse, tuple(variants)))
            if not anchors:
                raise ValueError(f"页面 {name} 至少需要一个模板")
            if any(anchor.allow_pulse for anchor in anchors):
                if sum(anchor.allow_pulse for anchor in anchors) != 1 or not any(not anchor.allow_pulse for anchor in anchors):
                    raise ValueError("脉冲文字只能设置一项，并且必须包含严格检查外观的其他页面标志")
            self.states.append({"name": name, "label": item.get("label", name), "tap": tap, "anchors": anchors})
        if not self.states:
            raise ValueError("配置没有任何页面模板")

    def detect(self, bgr_frame: np.ndarray) -> Detection:
        if not isinstance(bgr_frame, np.ndarray) or bgr_frame.ndim != 3 or bgr_frame.shape[2] != 3 or not bgr_frame.size:
            raise ValueError("识别需要非空的 BGR 截图")
        height, width = bgr_frame.shape[:2]
        ref_width, ref_height = self.reference_size
        if abs((width / height) / (ref_width / ref_height) - 1.0) > 0.03:
            return Detection(None, 0.0, None, {"reason": "截图宽高比与识别配置不符", "size": [width, height]})
        normalized = cv2.resize(bgr_frame, self.reference_size, interpolation=cv2.INTER_AREA if width > ref_width else cv2.INTER_LINEAR)
        gray = _gray(normalized)
        edges = None
        matches = []
        candidates = []
        for state in self.states:
            scores = []
            anchors_pass = True
            anchor_details = []
            for anchor in state["anchors"]:
                if anchor.method == "edge" and edges is None:
                    edges = _edge(gray)
                source = edges if anchor.method == "edge" else gray
                left, top, right, bottom = anchor.roi
                x1, y1 = math.floor(left * ref_width), math.floor(top * ref_height)
                x2, y2 = math.ceil(right * ref_width), math.ceil(bottom * ref_height)
                area = source[y1:y2, x1:x2]
                gray_area = gray[y1:y2, x1:x2]
                best, location, matched_scale = 0.0, None, None
                best_metrics = {"correlation": 0.0, "contrast": 0.0, "luminance": 0.0,
                                "contrast_ratio": 0.0, "mean_delta": 0.0, "visibility_ok": False}
                raw_best = None
                raw_best_correlation = -math.inf
                for scale, template, gray_template in anchor.variants:
                    th, tw = template.shape[:2]
                    if area.shape[0] < th or area.shape[1] < tw:
                        continue
                    result = cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED)
                    _, correlation, _, position = cv2.minMaxLoc(result)
                    px, py = position
                    gray_patch = gray_area[py:py + th, px:px + tw]
                    # Correlation alone also matches an anchor behind a dimmed
                    # modal. Keep its luminance and contrast consistent too.
                    ratio = float(gray_patch.std()) / max(float(gray_template.std()), 1.0)
                    contrast = min(ratio, 1.0 / ratio) if ratio > 0 else 0.0
                    mean_delta = float(gray_patch.mean()) - float(gray_template.mean())
                    luminance = max(0.0, 1.0 - abs(mean_delta) / 128.0)
                    visibility_ok = 0.4 <= ratio <= 1.8 and luminance >= 0.5
                    if anchor.allow_pulse:
                        # The intro's label pulses independently of its fixed
                        # AUTO/header. Retain text-shape matching and a visible
                        # contrast floor; every companion anchor remains strict.
                        score = correlation if visibility_ok else 0.0
                    else:
                        score = min(correlation, contrast, luminance)
                    metrics = {"correlation": round(float(correlation), 4), "contrast": round(contrast, 4),
                               "luminance": round(luminance, 4), "contrast_ratio": round(ratio, 4),
                               "mean_delta": round(mean_delta, 4), "visibility_ok": visibility_ok}
                    if math.isfinite(correlation) and correlation > raw_best_correlation:
                        raw_best_correlation = correlation
                        raw_best = {**metrics, "score": round(float(score), 4), "scale": scale,
                                    "location": [x1 + position[0], y1 + position[1]]}
                    if math.isfinite(score) and score > best:
                        best = float(score)
                        location = [x1 + position[0], y1 + position[1]]
                        matched_scale = scale
                        best_metrics = metrics
                scores.append(best)
                anchors_pass = anchors_pass and best >= anchor.threshold
                anchor_details.append({"name": anchor.name, "score": round(best, 4), "threshold": anchor.threshold,
                                       "location": location, "scale": matched_scale, "method": anchor.method,
                                       "allow_pulse": anchor.allow_pulse, **best_metrics, "raw_best": raw_best})
            confidence = min(scores)
            details = {"state": state["name"], "confidence": confidence, "matched": anchors_pass, "anchors": anchor_details}
            matches.append(details)
            if anchors_pass:
                candidates.append((state, confidence))
        details = {"size": [width, height], "matches": matches}
        if len(candidates) != 1:
            details["reason"] = "多个页面同时匹配，等待人工处理" if candidates else "没有页面的全部模板匹配"
            return Detection(None, max((m["confidence"] for m in matches), default=0.0), None, details)
        state, confidence = candidates[0]
        tap = None if state["tap"] is None else (
            min(width - 1, round(state["tap"][0] * width)), min(height - 1, round(state["tap"][1] * height)))
        details["label"] = state["label"]
        return Detection(state["name"], confidence, tap, details)
