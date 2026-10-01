from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from gget_runner.loot import LootDrop, LootLedger, LootReader, LootSnapshot


REFERENCES = Path(__file__).resolve().parent / "fixtures" / "references"


def load(name="reward.png"):
    image = cv2.imdecode(np.frombuffer((REFERENCES / name).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


@pytest.fixture(scope="module")
def reader():
    return LootReader()


def test_real_reference_quantities_and_duplicate_icon_aggregation(reader):
    snapshot = reader.read(load())
    assert [drop.quantity for drop in snapshot.drops] == [2, 1, 2000, 2, 1, 2]
    assert snapshot.warnings == ()
    assert snapshot.drops[0].item_id == snapshot.drops[3].item_id
    assert snapshot.drops[2].item_id == "coin"
    assert snapshot.drops[2].name == "金币"
    assert len({drop.item_id for drop in snapshot.drops}) == 5
    assert all(drop.name.startswith("道具") for drop in snapshot.drops if drop.item_id != "coin")
    assert all(cv2.imdecode(np.frombuffer(drop.icon_png, np.uint8), cv2.IMREAD_COLOR) is not None
               for drop in snapshot.drops)
    ledger = LootLedger()
    assert ledger.record_round(1, snapshot)
    assert [row.total_quantity for row in ledger.rows] == [4, 1, 2000, 1, 2]
    assert all(row.rounds == 1 for row in ledger.rows)
    assert ledger.coin_total == 2000


def test_second_real_reward_and_cross_frame_identities(reader):
    reference = reader.read(load())
    actual = reader.read(load("loot-live-reward.png"))
    assert [drop.quantity for drop in actual.drops] == [2, 1, 2000, 2, 1, 1, 1]
    assert actual.warnings == ()
    assert actual.drops[0].item_id == actual.drops[3].item_id == reference.drops[0].item_id
    assert actual.drops[1].item_id == actual.drops[4].item_id == reference.drops[4].item_id
    assert actual.drops[5].item_id == reference.drops[1].item_id
    # The seventh card has different artwork colors, despite the same silhouette.
    assert actual.drops[6].item_id != actual.drops[5].item_id
    ledger = LootLedger()
    ledger.record_round(1, actual)
    assert [row.total_quantity for row in ledger.rows] == [4, 2, 2000, 1, 1]


@pytest.mark.parametrize("name, size", [("reward.png", (1280, 720)),
                                        ("loot-live-reward.png", (1280, 720)),
                                        ("reward.png", (960, 540)),
                                        ("reward.png", (1920, 1080))])
def test_display_icons_include_the_whole_square_frame_from_normalized_source(name, size):
    reader = LootReader()
    source = cv2.resize(load(name), size)
    normalized = cv2.resize(source, (1280, 720), interpolation=(
        cv2.INTER_AREA if size[0] > 1280 else cv2.INTER_LINEAR))
    cards = reader._cards(normalized)
    snapshot = reader.read(source)
    assert len(snapshot.drops) == (7 if name == "loot-live-reward.png" else 6)
    for drop, (x, y) in zip(snapshot.drops, cards):
        icon = cv2.imdecode(np.frombuffer(drop.icon_png, np.uint8), cv2.IMREAD_COLOR)
        assert icon.shape == (112, 112, 3)
        # Check the entire background frame, including its top and bottom lines.
        # The extra pixels must come from the screenshot, never synthetic fill.
        np.testing.assert_array_equal(icon, normalized[y - 3:y + 109, x - 3:x + 109])
        np.testing.assert_array_equal(icon[3:109, 3:109], normalized[y:y + 106, x:x + 106])
    if name == "reward.png" and size == (1280, 720):
        first = cv2.imdecode(np.frombuffer(snapshot.drops[0].icon_png, np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(first, source[152:264, 57:169])
        # The red hidden-item bar starts at y266, outside the displayed card.
        assert 152 + first.shape[0] < 266


def test_display_changes_do_not_split_identity_or_reuse_old_card_pixels():
    reader = LootReader()
    source = load()
    before = reader.read(source)
    changed = source.copy()
    changed[232:255, 68:156] = source[232:255, 185:273]
    changed[172:197, 127:152] = (255, 0, 0)
    after = reader.read(changed)
    assert before.drops[0].quantity == 2
    assert after.drops[0].quantity == 1
    assert after.drops[0].item_id == before.drops[0].item_id
    assert after.drops[0].icon_png != before.drops[0].icon_png


@pytest.mark.parametrize("size", [(960, 540), (1920, 1080)])
def test_resolution_mapping_preserves_items_and_counts(reader, size):
    reference = reader.read(load())
    resized = reader.read(cv2.resize(load(), size))
    assert [drop.quantity for drop in resized.drops] == [2, 1, 2000, 2, 1, 2]
    assert [drop.item_id for drop in resized.drops] == [drop.item_id for drop in reference.drops]
    assert not resized.warnings


def test_reward_card_detection_is_not_tied_to_fixed_slots(reader):
    original = load()
    rearranged = original.copy()
    rearranged[135:550, 40:1240] = 30
    rearranged[310:416, 830:936] = original[155:261, 60:166]
    rearranged[320:426, 180:286] = original[155:261, 294:400]
    snapshot = reader.read(rearranged)
    assert len(snapshot.drops) == 2
    assert sorted(drop.quantity for drop in snapshot.drops) == [2, 2000]
    assert {drop.item_id for drop in snapshot.drops} == {"coin", reader.read(original).drops[0].item_id}


def test_bonus_badges_and_quantity_pixels_do_not_define_identity():
    reader = LootReader()
    frame = load()
    icon = frame[172:233, 67:152].copy()
    first = reader._identity(icon)
    icon[:30, 58:] = (255, 0, 0)
    assert reader._identity(icon)[0] == first[0]
    before = reader.read(frame)
    frame[232:255, 68:156] = 25
    after = reader.read(frame)
    assert after.drops[0].item_id == before.drops[0].item_id
    assert after.drops[0].quantity is None
    assert after.warnings


def test_same_shape_different_colors_do_not_merge(reader):
    drops = reader.read(load()).drops
    assert len({drops[index].item_id for index in (1, 4, 5)}) == 3


def test_low_ocr_confidence_preserves_unknown_without_defaulting_to_one():
    reader = LootReader()
    reader._ocr = lambda *args, **kwargs: ([["1", 0.4]], [0.001])
    snapshot = reader.read(load())
    assert len(snapshot.drops) == 6
    assert all(drop.quantity is None for drop in snapshot.drops)
    assert len(snapshot.warnings) == 6
    ledger = LootLedger()
    ledger.record_round(1, snapshot)
    assert ledger.coin_total == 0
    assert all(row.total_quantity == 0 and row.last_quantity is None and row.unknown_rounds == 1
               for row in ledger.rows)


def test_ocr_failure_is_a_visible_warning():
    reader = LootReader()

    def fail(*args, **kwargs):
        raise RuntimeError("test model failure")

    reader._ocr = fail
    snapshot = reader.read(load())
    assert len(snapshot.drops) == 6
    assert all(drop.quantity is None for drop in snapshot.drops)
    assert all("RuntimeError" in warning for warning in snapshot.warnings)


@pytest.mark.parametrize("text, expected", [("x2", 2), ("×12", 12), ("X 2", 2),
                                             ("2,000", 2000), ("2000", 2000),
                                             ("20,00", None), ("2O00", None), ("1.0", None)])
def test_quantity_parser_accepts_prefixes_and_valid_commas_only(text, expected):
    assert LootReader._parse_quantity(text, 0.99) == expected
    assert LootReader._parse_quantity(text, 0.4) is None
    assert LootReader._parse_quantity(text, float("nan")) is None


def test_bare_comma_separated_coin_text_is_read_locally(reader):
    patch = np.full((23, 88, 3), 25, np.uint8)
    cv2.putText(patch, "2,000", (1, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                (255, 255, 255), 1, cv2.LINE_AA)
    quantity, confidence = reader._quantity(patch, is_coin=True)
    assert quantity == 2000
    assert confidence >= reader._MIN_OCR_CONFIDENCE
    assert reader._quantity(patch, is_coin=False)[0] is None


@pytest.mark.parametrize("name", ["prepare.png", "sortie.png", "battle_intro.png", "battle.png",
                                  "score.png", "experience.png"])
def test_non_reward_pages_never_fabricate_records(name):
    reader = LootReader()
    assert reader.read(load(name)) == LootSnapshot()
    assert reader._ocr is None


def test_wrong_aspect_ratio_is_visible_and_invalid_input_rejected(reader):
    snapshot = reader.read(np.zeros((720, 720, 3), np.uint8))
    assert not snapshot.drops and snapshot.warnings
    with pytest.raises(ValueError):
        reader.read(np.zeros((720, 1280), np.uint8))


def drop(item_id="item-01", quantity=2):
    return LootDrop(item_id, "金币" if item_id == "coin" else "道具01", quantity, b"png")


def test_ledger_duplicate_rounds_unknowns_partial_counts_and_latest_round():
    ledger = LootLedger()
    snapshot = LootSnapshot((drop(quantity=2), drop(quantity=3), drop("coin", 2000)))
    assert ledger.record_round(1, snapshot)
    assert not ledger.record_round(1, LootSnapshot((drop(quantity=999),)))
    assert ledger.rounds == 1
    assert ledger.rows[0].total_quantity == ledger.rows[0].last_quantity == 5
    assert ledger.rows[0].rounds == 1
    unknown = LootSnapshot((drop(quantity=4), drop(quantity=None)), ("数量待确认",))
    assert ledger.record_round(2, unknown)
    assert ledger.rounds == 2
    assert ledger.rows[0].total_quantity == 9
    assert ledger.rows[0].last_quantity is None
    assert ledger.rows[0].rounds == 2
    assert ledger.rows[0].unknown_rounds == 1
    assert ledger.rows[1].last_quantity == 0
    assert ledger.coin_total == 2000
    assert ledger.warning_rounds == 1
    assert not ledger.record_round(2, unknown)
    assert ledger.warning_rounds == 1
    ledger.record_round(3, LootSnapshot((drop(quantity=1),)))
    assert ledger.rows[0].last_quantity == 1
    assert ledger.rows[0].total_quantity == 10
    assert ledger.rows[0].unknown_rounds == 1


def test_ledger_reset_and_warnings_without_cards():
    ledger = LootLedger()
    ledger.record_round(1, LootSnapshot((drop(),)))
    ledger.record_round(2, LootSnapshot(warnings=("读取失败",)))
    assert ledger.warning_rounds == 1
    assert ledger.rows[0].last_quantity == 0
    ledger.reset()
    assert ledger.rows == () and ledger.rounds == ledger.coin_total == ledger.warning_rounds == 0
    assert ledger.record_round(1, LootSnapshot((drop(),)))


def test_ledger_rejects_invalid_quantities_before_consuming_round():
    ledger = LootLedger()
    with pytest.raises(ValueError):
        ledger.record_round(1, LootSnapshot((drop(quantity=-1),)))
    assert ledger.rounds == 0
    assert ledger.record_round(1, LootSnapshot((drop(quantity=None),)))
    assert ledger.warning_rounds == 1


def test_snapshot_metadata_is_json_safe_and_excludes_image_bytes():
    snapshot = LootSnapshot((drop(), drop("coin", None)), ("数量待确认",))
    metadata = snapshot.to_dict()
    assert json.loads(json.dumps(metadata, ensure_ascii=False)) == metadata
    assert metadata["drops"][0] == {"item_id": "item-01", "name": "道具01", "quantity": 2, "confidence": 1.0}
    assert "icon_png" not in metadata["drops"][0]
