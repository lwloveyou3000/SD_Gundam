"""Local reward-card reading and run-scoped loot accounting.

Only the game's reward frame and card chrome are templates. Item artwork is
learned during the run; quantities and bonus badges never define item identity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import re

import cv2
import numpy as np


@dataclass(frozen=True)
class LootDrop:
    item_id: str
    name: str
    quantity: int | None
    icon_png: bytes
    confidence: float = 1.0


@dataclass(frozen=True)
class LootSnapshot:
    drops: tuple[LootDrop, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "drops": [{"item_id": drop.item_id, "name": drop.name,
                       "quantity": drop.quantity, "confidence": drop.confidence}
                      for drop in self.drops],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class LootTotal:
    item_id: str
    name: str
    total_quantity: int
    last_quantity: int | None
    rounds: int
    unknown_rounds: int
    icon_png: bytes


class LootLedger:
    """Record a reward arrival once, including partially unreadable quantities."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._seen_rounds: set[int] = set()
        self._rows: dict[str, LootTotal] = {}
        self._warning_rounds = 0

    @property
    def rows(self) -> tuple[LootTotal, ...]:
        return tuple(self._rows.values())

    @property
    def rounds(self) -> int:
        return len(self._seen_rounds)

    @property
    def coin_total(self) -> int:
        coin = self._rows.get("coin")
        return coin.total_quantity if coin else 0

    @property
    def warning_rounds(self) -> int:
        return self._warning_rounds

    def record_round(self, round_number: int, snapshot: LootSnapshot) -> bool:
        if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
            raise ValueError("统计轮次必须是正整数")
        if round_number in self._seen_rounds:
            return False
        # Validate before committing, so a malformed caller cannot consume a round.
        for drop in snapshot.drops:
            if drop.quantity is not None and (
                    not isinstance(drop.quantity, int) or isinstance(drop.quantity, bool) or drop.quantity < 0):
                raise ValueError("战利品数量必须是非负整数或 None")
        grouped: dict[str, list[LootDrop]] = {}
        for drop in snapshot.drops:
            grouped.setdefault(drop.item_id, []).append(drop)
        self._rows = {key: replace(row, last_quantity=0) for key, row in self._rows.items()}
        for item_id, drops in grouped.items():
            known = sum(drop.quantity for drop in drops if drop.quantity is not None)
            unknown = any(drop.quantity is None for drop in drops)
            first = drops[0]
            old = self._rows.get(item_id)
            self._rows[item_id] = LootTotal(
                item_id, old.name if old else first.name,
                (old.total_quantity if old else 0) + known,
                None if unknown else known, (old.rounds if old else 0) + 1,
                (old.unknown_rounds if old else 0) + int(unknown),
                old.icon_png if old else first.icon_png,
            )
        self._seen_rounds.add(round_number)
        if snapshot.warnings or any(drop.quantity is None for drop in snapshot.drops):
            self._warning_rounds += 1
        return True


class LootReader:
    """Read normalized visible rewards with local CPU OCR and conservative IDs."""

    _MIN_OCR_CONFIDENCE = 0.85
    _CARD_THRESHOLD = 0.62
    _ITEM_DISTANCE = 12.0
    _DISPLAY_FRAME_MARGIN = 3

    def __init__(self):
        asset_dir = Path(__file__).resolve().parent.parent / "assets" / "loot"
        self._header = self._load(asset_dir / "reward-header.png", cv2.IMREAD_GRAYSCALE)
        self._acquired = self._load(asset_dir / "reward-acquired.png", cv2.IMREAD_GRAYSCALE)
        self._card_mask = self._load(asset_dir / "card-mask.png", cv2.IMREAD_GRAYSCALE)
        self._card_templates = tuple(self._load(asset_dir / f"card-{i}.png", cv2.IMREAD_GRAYSCALE)
                                     for i in range(6))
        self._multiplier = self._load(asset_dir / "multiplier.png", cv2.IMREAD_GRAYSCALE)
        self._coin_signature = self._signature(self._load(asset_dir / "coin.png", cv2.IMREAD_COLOR))
        self._items: list[tuple[str, str, np.ndarray]] = []
        self._ocr = None

    @staticmethod
    def _load(path: Path, mode: int) -> np.ndarray:
        loaded = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), mode)
        if loaded is None:
            raise ValueError(f"无法读取战利品识别资源：{path.name}")
        return loaded

    @staticmethod
    def _png(icon: np.ndarray) -> bytes:
        success, encoded = cv2.imencode(".png", icon)
        if not success:
            raise ValueError("无法生成战利品缩略图")
        return encoded.tobytes()

    @staticmethod
    def _anchor(gray: np.ndarray, template: np.ndarray, bounds: tuple[int, int, int, int]) -> bool:
        x1, y1, x2, y2 = bounds
        area = gray[y1:y2, x1:x2]
        result = cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(result)
        patch = area[y:y + template.shape[0], x:x + template.shape[1]]
        contrast = float(patch.std()) / max(float(template.std()), 1.0)
        return score >= 0.82 and 0.65 <= contrast <= 1.5 and abs(float(patch.mean()) - float(template.mean())) <= 28

    def _cards(self, frame: np.ndarray) -> list[tuple[int, int]]:
        # Search the full reward body, rather than assuming a card count or slot.
        edges = cv2.Canny(frame, 50, 150)[135:550, 40:1240]
        height, width = self._card_mask.shape
        scores = np.zeros((edges.shape[0] - height + 1, edges.shape[1] - width + 1), np.float32)
        for template in self._card_templates:
            match = cv2.matchTemplate(edges, template, cv2.TM_CCORR_NORMED, mask=self._card_mask)
            match[~np.isfinite(match)] = 0
            np.maximum(scores, match, out=scores)
        cards = []
        while True:
            _, confidence, _, (x, y) = cv2.minMaxLoc(scores)
            if confidence < self._CARD_THRESHOLD:
                break
            cards.append((x + 40, y + 135))
            scores[max(0, y - 70):y + 71, max(0, x - 70):x + 71] = 0
        return sorted(cards, key=lambda card: (round(card[1] / 60), card[0]))

    @staticmethod
    def _signature(icon: np.ndarray) -> np.ndarray:
        return cv2.resize(cv2.GaussianBlur(icon, (7, 7), 0), (48, 36)).astype(np.float32)

    @staticmethod
    def _distance(first: np.ndarray, second: np.ndarray) -> float:
        best = math.inf
        # Small shifts account for screenshot filtering and frame-edge alignment.
        # The mask excludes the bonus badge and all quantity pixels.
        mask = np.ones(first.shape[:2], bool)
        mask[:19, 29:] = False
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                ay, by = max(dy, 0), max(-dy, 0)
                ax, bx = max(dx, 0), max(-dx, 0)
                h, w = 36 - abs(dy), 48 - abs(dx)
                valid = mask[ay:ay + h, ax:ax + w] & mask[by:by + h, bx:bx + w]
                difference = np.abs(first[ay:ay + h, ax:ax + w] - second[by:by + h, bx:bx + w])
                best = min(best, float(difference[valid].mean()))
        return best

    def _identity(self, icon: np.ndarray) -> tuple[str, str]:
        signature = self._signature(icon)
        if self._distance(signature, self._coin_signature) <= self._ITEM_DISTANCE:
            return "coin", "金币"
        distances = [self._distance(signature, item[2]) for item in self._items]
        if distances and min(distances) <= self._ITEM_DISTANCE:
            item = self._items[int(np.argmin(distances))]
            return item[0], item[1]
        number = len(self._items) + 1
        item_id, name = f"item-{number:02d}", f"道具{number:02d}"
        self._items.append((item_id, name, signature))
        return item_id, name

    @staticmethod
    def _frame_bounds(frame: np.ndarray, x: int, y: int) -> tuple[int, int, int, int] | None:
        """Measure only long perimeter lines in narrow bands near the locator."""
        edges = cv2.Canny(frame[y - 4:y + 110, x - 4:x + 113], 50, 150)
        # Side samples stop above quantities; bottom samples stop before the
        # separate hidden-item bar. Artwork and badges are outside these bands.
        left = np.flatnonzero(np.count_nonzero(edges[20:86, 1:14], axis=0) >= 15) + x - 3
        right = np.flatnonzero(np.count_nonzero(edges[20:86, 101:113], axis=0) >= 15) + x + 97
        top = np.flatnonzero(np.count_nonzero(edges[1:12, 24:90], axis=1) >= 15) + y - 3
        bottom = np.flatnonzero(np.count_nonzero(edges[101:110, 24:90], axis=1) >= 15) + y + 97
        if any(not len(side) for side in (left, right, top, bottom)):
            return None
        bounds = int(left[0]), int(top[0]), int(right[-1]), int(bottom[-1])
        if not (95 <= bounds[2] - bounds[0] <= 108 and 95 <= bounds[3] - bounds[1] <= 108):
            return None
        return bounds

    def _display_bounds(self, frame: np.ndarray, x: int, y: int) -> tuple[int, int, int, int]:
        height, width = self._card_mask.shape
        margin = self._DISPLAY_FRAME_MARGIN
        left, top = x - margin, y - margin
        display_width, display_height = width + 2 * margin, height + 2 * margin
        frame_bounds = self._frame_bounds(frame, x, y)
        if frame_bounds is not None:
            x1, y1, x2, y2 = frame_bounds
            centered_left = math.floor((x1 + x2 - display_width + 1) / 2 + 0.5)
            centered_top = math.floor((y1 + y2 - display_height + 1) / 2 + 0.5)
            # Only display coordinates move. Bound calibration to the locator's
            # small uncertainty, retaining the original identity/OCR positions.
            left = max(left - 4, min(left + 4, centered_left))
            top = max(top - 2, min(top + 2, centered_top))
        return left, top, left + display_width, top + display_height

    def _display_icon(self, frame: np.ndarray, x: int, y: int) -> np.ndarray:
        left, top, right, bottom = self._display_bounds(frame, x, y)
        # Copy real screenshot pixels; never pad or stretch the displayed card.
        return frame[top:bottom, left:right].copy()

    @classmethod
    def _parse_quantity(cls, text: str, confidence: float) -> int | None:
        if not math.isfinite(confidence) or confidence < cls._MIN_OCR_CONFIDENCE:
            return None
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"^[xX×]", "", text)
        if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", text):
            return None
        return int(text.replace(",", ""))

    def _get_ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                 use_det=False, use_cls=False)
        return self._ocr

    def _quantity_once(self, patch: np.ndarray, is_coin: bool, threshold: int) -> tuple[int | None, float]:
        mask = cv2.threshold(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), threshold, 255, cv2.THRESH_BINARY)[1]
        components = cv2.connectedComponentsWithStats(mask)[2][1:]
        digits = [component for component in components if component[3] >= 13 and component[4] >= 10]
        if not digits:
            return None, 0.0
        left = min(component[0] for component in digits)
        right = max(component[0] + component[2] for component in digits)
        top = min(component[1] for component in digits)
        bottom = max(component[1] + component[3] for component in digits)
        markers = [component for component in components
                   if 6 <= component[2] <= 13 and 6 <= component[3] <= 12
                   and component[4] >= 15 and 0 <= left - component[0] - component[2] <= 8]
        has_marker = False
        for mx, my, mw, mh, _ in markers:
            marker = cv2.resize(mask[my:my + mh, mx:mx + mw], self._multiplier.shape[::-1],
                                interpolation=cv2.INTER_NEAREST) > 0
            expected = self._multiplier > 0
            overlap = 2 * np.count_nonzero(marker & expected) / max(np.count_nonzero(marker) + np.count_nonzero(expected), 1)
            has_marker = has_marker or overlap >= 0.72
        if not is_coin and not has_marker:
            return None, 0.0
        # Isolate digit bodies: the much shorter x glyph otherwise lowers OCR's
        # confidence. Keep any commas between those bodies. No glyph substitution.
        digits_image = 255 - mask[max(0, top - 2):min(len(patch), bottom + 2),
                                  max(0, left - 1):min(patch.shape[1], right + 1)]
        digits_image = cv2.copyMakeBorder(digits_image, 4, 4, 10, 10, cv2.BORDER_CONSTANT, value=255)
        digits_image = cv2.resize(digits_image, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
        results, _ = self._get_ocr()(digits_image, use_det=False, use_cls=False)
        if not results or len(results) != 1 or len(results[0]) != 2:
            return None, 0.0
        text, score = results[0]
        confidence = float(score)
        if not math.isfinite(confidence):
            return None, 0.0
        return self._parse_quantity(str(text), confidence), confidence

    def _quantity(self, patch: np.ndarray, is_coin: bool) -> tuple[int | None, float]:
        best_confidence = 0.0
        for threshold in (155, 170):
            quantity, confidence = self._quantity_once(patch, is_coin, threshold)
            if quantity is not None:
                return quantity, confidence
            best_confidence = max(best_confidence, confidence)
        return None, best_confidence

    def read(self, bgr_frame: np.ndarray) -> LootSnapshot:
        if (not isinstance(bgr_frame, np.ndarray) or bgr_frame.ndim != 3
                or bgr_frame.shape[2] != 3 or not bgr_frame.size):
            raise ValueError("战利品识别需要非空的 BGR 截图")
        height, width = bgr_frame.shape[:2]
        if abs((width / height) / (1280 / 720) - 1) > 0.03:
            return LootSnapshot(warnings=("战利品截图宽高比不符，数量待确认",))
        frame = cv2.resize(bgr_frame, (1280, 720),
                           interpolation=cv2.INTER_AREA if width > 1280 else cv2.INTER_LINEAR)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if not (self._anchor(gray, self._header, (575, 20, 710, 66))
                and self._anchor(gray, self._acquired, (45, 76, 125, 118))):
            return LootSnapshot()
        cards = self._cards(frame)
        if not cards:
            return LootSnapshot(warnings=("奖励页未识别到可见战利品卡片，数量待确认",))
        drops, warnings = [], []
        for index, (x, y) in enumerate(cards, 1):
            artwork = frame[y + 17:y + 78, x + 7:x + 92].copy()
            item_id, name = self._identity(artwork)
            # Full cards are display-only: their frame, quantity and bonus badge
            # never enter the item signature or alter identity across rounds.
            png = self._png(self._display_icon(frame, x, y))
            try:
                quantity, confidence = self._quantity(frame[y + 77:y + 100, x + 8:x + 96], item_id == "coin")
            except Exception as exc:
                quantity, confidence = None, 0.0
                warnings.append(f"{name}（卡片{index}）数量待确认：本地识别失败（{type(exc).__name__}）")
            else:
                if quantity is None:
                    warnings.append(f"{name}（卡片{index}）数量待确认，识别置信度 {confidence:.0%}")
            drops.append(LootDrop(item_id, name, quantity, png, confidence))
        return LootSnapshot(tuple(drops), tuple(warnings))
