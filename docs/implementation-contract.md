# Implementation contract

Lead owns assets/profiles/default/, reference preparation, app.py, launchers, requirements, README, integration and live validation. Core worker owns gget_runner/adb.py, vision.py, runner.py and tests/test_core.py. UI worker owns gget_runner/ui.py and settings.py. Workers must not modify other owners' files or operate the emulator UI.

## Core API

- `AdbClient(adb_path: str | Path, serial: str)`.
- `AdbClient.list_devices(adb_path)` returns `list[Device]`, each with `serial`, `state`, `model` strings.
- `adb.screenshot()` returns a BGR NumPy image. `adb.tap(x, y)` uses device pixels. `adb.is_game_foreground()` returns bool for `com.bandainamcoent.gget_WW`.
- `ScreenDetector(profile_dir: str | Path)` reads profile.json and relative template PNG files. `detect(bgr_frame)` returns `Detection(state: str | None, confidence: float, tap: tuple[int,int] | None, details: dict)`. Coordinates are in the original captured frame.
- `RunConfig(rounds=1, infinite=False, poll_interval=1.0, stable_frames=2, action_cooldown=1.5, transition_timeout=60.0, battle_timeout=900.0, diagnostics_dir=Path('logs'))`.
- `BotRunner(adb, detector, config, on_event)` provides `start()`, `run()` (synchronous), `stop()`, `pause()`, `resume()`, `join(timeout=None)`, `running` and `paused` properties. UI callbacks run on a background thread and must not call Tk.
- `RunEvent(kind, message='', state='', completed=0, frame=None)`; kinds: log, frame, state, progress, finished, error, paused, resumed. `frame` is BGR when provided.

## Workflow

States are `prepare`, `sortie`, `battle_intro`, `battle`, `score`, `experience`, `reward`. `battle` has tap:null and is observation only. Initial supported pages: selected stage, sortie preparation, battle introduction or an already-running auto battle. Do not count a reward page that was already open before a confirmed battle.

The intro will auto-advance after30 seconds. Recognize and click TAP TO NEXT promptly; observed battle is only a recovery fallback, not a deliberate30-second wait.

Click prepare, sortie and intro once each when two stable frames identify the page. During battle send no input and wait for score; then continue score -> experience -> reward. Increment completed exactly once per reward arrival. When the finite target is reached, stop on reward without clicking again. Otherwise click repeat on reward and wait for a new battle_intro before counting another completion. Stopping/pausing must prevent further scheduled clicks. Never click refill, purchase, CAPTCHA or arbitrary unknown dialogs. Unknown transitions time out with an error and saved diagnostic screenshot. Battle may legitimately be unrecognized until score, with its longer watchdog. Handle stale screens without rapid repeated sortie clicks.

## Profile schema

profile.json has `schema_version:1`, `reference_size:[1280,720]`, and `states` list. Each state has name, label, normalized `tap:[x,y]`, and `templates` list. A template has name, `path` relative to profile_dir, normalized `search_roi:[left,top,right,bottom]`, threshold and optional `method` (`gray` default or `edge`). All templates/anchors for a state must match. Normalize screenshots to reference size for matching, then map clicks back to original pixels. Source imagery is the game's content only, excluding the LDPlayer title and side toolbar. Template rectangles isolate fixed labels/buttons; backgrounds, unit art, score numbers and stage names are not identification features.

## UI

Chinese Tkinter app with ADB path browse, device refresh/selection, finite round count or infinite checkbox, start/pause/resume/stop, current state and completion count, screenshot preview and event log. Include a read-only screenshot/detection button. Persist path/device/count/infinite in settings.json. Do not auto-start farming on app launch. All ADB and screenshot work must stay off the Tk main thread. `launch_ui(project_root: Path)` is the GUI entry. Default ADB F:\\leidian\\LDPlayer9\\adb.exe; profile is project_root/assets/profiles/default. Default count 1, finite mode. Use Microsoft YaHei fonts and clear layout; display that AUTO must be enabled in the game and unexpected dialogs require manual handling.
