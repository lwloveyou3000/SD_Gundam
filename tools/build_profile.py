"""Build fixed UI label templates from the six user-supplied screenshots."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(r"C:\Users\Administrator\AppData\Local\Temp")
SOURCES = {
    "prepare": "codex-clipboard-e344730b-0106-4715-9ea4-3c4b984265bd.png",
    "sortie": "codex-clipboard-f4e4a4ff-a95f-4e67-83ac-5397ae93681c.png",
    "battle_intro": "codex-clipboard-8cba02ac-87c0-41d6-8fd7-ca671cdb9144.png",
    "score": "codex-clipboard-7a95fa0f-2133-4bcd-ae9a-009d66438199.png",
    "experience": "codex-clipboard-f34eec34-90f1-4031-a819-c1a4b99f74e6.png",
    "reward": "codex-clipboard-56ab5071-abce-449f-b3d6-8f819a979cc1.png",
    "battle": "gget-bot-live-battle-reference.png",
}
PROFILE_ROOT = ROOT / "assets" / "profiles" / "default"
REFERENCE_ROOT = ROOT / "tests" / "fixtures" / "references"
VIEWPORT = (0, 34, 920, 552)
SIZE = (1280, 720)

# Crops use the original 958x552 screenshots; only stable text is retained.
SPEC = {
    "prepare": {
        "label": "已选关卡 · 出击准备",
        "tap": [0.874, 0.824],
        "anchors": [
            ("button", (758, 446, 855, 475), [0.76, 0.75, 0.995, 0.91], 0.78),
            ("header", (105, 40, 274, 76), [0.07, 0.0, 0.42, 0.12], 0.72),
        ],
    },
    "sortie": {
        "label": "出击准备 · 出击",
        "tap": [0.874, 0.916],
        "anchors": [
            ("button", (775, 483, 832, 509), [0.76, 0.84, 0.995, 0.985], 0.77),
            ("header", (105, 40, 281, 76), [0.07, 0.0, 0.43, 0.12], 0.72),
        ],
    },
    "battle_intro": {
        "label": "战斗开始 · TAP TO NEXT",
        "tap": [0.500, 0.900],
        "anchors": [
            ("button", (396, 486, 532, 518), [0.32, 0.83, 0.70, 0.96], 0.76),
            ("header", (702, 47, 751, 73), [0.72, 0.0, 0.88, 0.12], 0.72),
        ],
    },
    "score": {
        "label": "战斗完成 · 继续",
        "tap": [0.873, 0.916],
        "anchors": [
            ("button", (781, 493, 828, 521), [0.76, 0.83, 0.995, 0.985], 0.78),
            ("header", (40, 191, 158, 216), [0.025, 0.27, 0.25, 0.37], 0.72),
        ],
    },
    "experience": {
        "label": "经验结算 · 继续",
        "tap": [0.873, 0.916],
        "anchors": [
            ("button", (781, 493, 828, 521), [0.76, 0.83, 0.995, 0.985], 0.78),
            ("header", (43, 490, 211, 521), [0.025, 0.86, 0.35, 0.99], 0.72),
        ],
    },
    "reward": {
        "label": "战斗奖励 · 再次出击",
        "tap": [0.671, 0.916],
        "anchors": [
            ("button", (568, 489, 670, 521), [0.54, 0.83, 0.80, 0.985], 0.78),
            ("header", (41, 92, 94, 118), [0.025, 0.09, 0.21, 0.22], 0.70),
        ],
    },
    "battle": {
        "label": "正在自动战斗 · 不发送操作",
        "tap": None,
        "native": True,
        "anchors": [
            ("auto", (971, 16, 1044, 54), [0.72, 0.0, 0.88, 0.10], 0.74),
            ("turn", (51, 49, 86, 73), [0.015, 0.045, 0.18, 0.13], 0.72),
        ],
    },
}


def working_box(source_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = source_box
    return (
        round(x0 / 920 * 1280),
        round((y0 - 34) / 518 * 720),
        round(x1 / 920 * 1280),
        round((y1 - 34) / 518 * 720),
    )


def main() -> None:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    states = []
    previews = []
    for name, source_name in SOURCES.items():
        source = Image.open(SOURCE_ROOT / source_name).convert("RGB")
        spec = SPEC[name]
        frame = source.resize(SIZE, Image.Resampling.LANCZOS) if spec.get("native") else source.crop(VIEWPORT).resize(SIZE, Image.Resampling.LANCZOS)
        frame.save(REFERENCE_ROOT / f"{name}.png")
        templates = []
        for anchor_name, box, roi, threshold in spec["anchors"]:
            path_name = f"{name}_{anchor_name}.png"
            patch = frame.crop(box if spec.get("native") else working_box(box))
            patch.save(PROFILE_ROOT / path_name)
            templates.append({
                "name": anchor_name,
                "path": path_name,
                "search_roi": roi,
                "threshold": threshold,
                "method": "gray",
            })
            if name == "battle_intro" and anchor_name == "button":
                templates[-1]["allow_pulse"] = True
            previews.append((f"{name} / {anchor_name}", patch))
        states.append({"name": name, "label": spec["label"], "tap": spec["tap"], "templates": templates})
    manifest = {
        "schema_version": 1,
        "name": "国际服中文界面",
        "reference_size": list(SIZE),
        "states": states,
    }
    (PROFILE_ROOT / "profile.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sheet = Image.new("RGB", (620, len(previews) * 76), "#1d2530")
    draw = ImageDraw.Draw(sheet)
    for index, (label, patch) in enumerate(previews):
        y = index * 76
        draw.text((10, y + 4), label, fill="white")
        sheet.paste(patch, (270, y + 10))
    sheet.save(SOURCE_ROOT / "gget-ui-template-review.png")
    print(f"Created {len(states)} state profiles and {len(previews)} UI templates.")


if __name__ == "__main__":
    main()
