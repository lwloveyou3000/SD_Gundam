# Local reward reader assets

All PNGs are crops or edge masks from `tests/fixtures/references/reward.png`
(1280 × 720), supplied as the user's reward-page reference.

- `reward-header.png`: REWARD heading, x585–695/y29–56.
- `reward-acquired.png`: acquired-items heading, x54–113/y84–109.
- `card-0.png` through `card-5.png`: Canny edges of 106 × 106 card frames,
  y155–260, x60/177/294/411/528/645. Only their masked perimeter is matched.
- `card-mask.png`: frame perimeter; quantities, item artwork, and bonus badges
  are excluded from reward-card localization.
- `coin.png`: the known coin artwork, x301–385/y172–232. This is the only
  resource that assigns a known item name.
- `multiplier.png`: the short x quantity marker, x103–111/y239–247.

Item identities are learned separately for each `LootReader` instance. They use
colored artwork, excluding the number band and bonus badge; matching does not
depend on a stage, unit, card count, or reward-card position. OCR uses the models
bundled in RapidOCR locally with CPU thread limits of 2/1. Recognition confidence
below 85% produces an unknown quantity and a visible warning.

Display thumbnails copy a 112 × 112 square from the original screenshot around
the complete card frame. Long perimeter edges in narrow nearby bands calibrate
the square's center, correcting at most four horizontal or two vertical pixels
from the locator. The number band, artwork and separate hidden-item strip are
excluded from edge measurement. If those frame edges cannot be measured, the
original three-pixel margin remains the fallback. Display pixels include the
card frame and quantity; item matching continues to use only the original
85 × 61 artwork crop and badge mask. No blank padding or stretching is applied.
