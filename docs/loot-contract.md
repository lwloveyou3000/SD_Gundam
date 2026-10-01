# Loot statistics contract

The desktop UI displays loot statistics in place of the full game-screen preview. Existing stage/count/pause/stop behavior stays.

## Backend API

- Frozen `LootDrop(item_id: str, name: str, quantity: int | None, icon_png: bytes, confidence: float=1.0)`.
- Frozen `LootSnapshot(drops: tuple[LootDrop,...]=(), warnings: tuple[str,...]=())`; `to_dict()` returns JSON-safe drop metadata and warnings, excluding PNG bytes.
- `LootReader()` creates a reader with local CPU OCR, lazily if sensible. `read(bgr_frame) -> LootSnapshot` normalizes16:9 images to1280x720, extracts only visible reward-card items and their quantities. Coins are named 金币 and item_id coin. Items lacking official names get stable generic names 道具01 etc and cropped icon thumbnails; do not invent actual names.
- Reader instance maintains icon identities across one run. Same icons on two cards in one round share item_id. Similar shapes with different colors are distinct. Ignore quantities and corner bonus markers when comparing item icons.
- No guessed quantity: insufficient-confidence quantity becomes None and a warning. Correctly parse x/× prefixes and bare comma-separated coin amounts.
- `LootLedger()`; `reset()`; `record_round(round_number: int, snapshot: LootSnapshot) -> bool`. Duplicate round numbers must never add twice. Snapshot may have duplicate item_id cards and they aggregate into one row.
- Frozen `LootTotal(item_id, name, total_quantity: int, last_quantity: int | None, rounds: int, unknown_rounds: int, icon_png: bytes)`.
- `ledger.rows -> tuple[LootTotal,...]`, `ledger.rounds -> int`, `ledger.coin_total -> int`, `ledger.warning_rounds -> int`. last_quantity is None if that item's current round includes unknown counts; confirmed quantities still contribute to total, and unknown counts are visibly tracked.

## Runner / UI event integration

Append `loot: LootSnapshot | None = None` to RunEvent after frame, preserving existing positional args. Append optional `loot_reader=None` to BotRunner constructor; existing tests/fakes without a reader remain valid. GUI and CLI production paths pass LootReader.

On the single newly-counted reward arrival, before finite-target break or repeat click, read the reward screenshot and emit one `loot` event using the completed round number. Avoid extra input. Failures must show warnings without fabricating quantities or duplicating rewards. Stop/pause remain safe, finite completion's final rewards are included.

## UI

Remove canvas, full-game PhotoImage state, resize/render preview methods and frame-event rendering. Keep small item thumbnails in a loot Treeview. Panel title 战利品统计, metrics cumulative coins/stat rounds/uncertain rounds, rows icon + item name + current-round quantity + cumulative quantity + appearance rounds. Unknown quantities display 待确认. Clear stats on a new start, preserve through pause/resume/stop. On event.kind loot, record_round(event.completed,event.loot) and update. Handle duplicates. Read-only inspection updates recognition/page status but never increments earnings. Ignore/drop normal frame events so no full screenshot backlog builds.

Keep ADB controls and count controls unchanged. Start preflight constructs LootReader in the background and passes to runner. Stats must not be faked from a current already-open reward page. Native widget tests cover removed preview, duplicate events, aggregation and reset.

## Evidence and limits

Reward reference: tests/fixtures/references/reward.png,1280x720. Additional real rewards: logs/first-live-reward.png and logs/live-timing-before.png (the latter may be reward). Reference has two identical V-logo cards x2 each, a blue item x1, coins2000, a gold item x1 and a dark-blue item x2. Identify actual icons visually rather than naming them.

Use rapidocr_onnxruntime1.4.4 (bundled local models, Python3.10–3.12 supported). Dependencies are installed in the project .venv. No images are uploaded for OCR. Screenshot-diagnostic behavior is preserved elsewhere.
