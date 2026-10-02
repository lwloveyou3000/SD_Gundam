"""Chinese Tk desktop UI; only the main thread touches Tk widgets."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageTk

from .adb import AdbClient
from .loot import LootLedger, LootReader
from .runner import BotRunner, RunConfig
from .settings import AppSettings, MAX_ROUNDS, load_settings, save_settings
from .vision import ScreenDetector
from .widgets import GridTreeview


STATE_LABELS = {"prepare": "关卡准备", "sortie": "出击准备", "battle_intro": "战斗开始",
                "battle": "正在自动战斗", "score": "分数结算", "experience": "经验结算", "reward": "奖励结算"}


class RunnerWindow:
    def __init__(self, root: tk.Tk, project_root: Path):
        self.root = root
        self.project_root = Path(project_root)
        self.settings_path = self.project_root / "settings.json"
        saved = load_settings(self.settings_path)
        self.events: queue.Queue = queue.Queue()
        self._runner: BotRunner | None = None
        self._runner_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._cancel = threading.Event()
        self._closing = False
        self._active = False
        self._paused = False
        self._stopping = False
        self._control_pending = False
        self._utility_busy = False
        self._completed = 0
        self._run_error = False
        self._target = saved.rounds
        self._infinite_run = saved.infinite
        self._device_labels: dict[str, str] = {}
        self.loot_ledger = LootLedger()
        self._loot_icons: dict[str, ImageTk.PhotoImage] = {}
        self.adb_path = tk.StringVar(root, saved.adb_path)
        self.device = tk.StringVar(root, saved.device)
        self.rounds = tk.StringVar(root, str(saved.rounds))
        self.infinite = tk.BooleanVar(root, saved.infinite)
        self.status = tk.StringVar(root, "待开始")
        self.page = tk.StringVar(root, "等待开始挑战")
        self.count = tk.StringVar(root, "已完成 0 次")
        self.coin_total = tk.StringVar(root, "金币累计：0")
        self.loot_rounds = tk.StringVar(root, "统计轮数：0")
        self.warning_rounds = tk.StringVar(root, "待确认轮数：0")
        self._build()
        self.rounds.trace_add("write", lambda *_args: self._mode_changed())
        self._update_count()
        self._update_controls()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._drain_events)
        self._log("请先开启游戏 AUTO，手动选择关卡，再刷新设备并开始。")

    def _build(self) -> None:
        root = self.root
        root.title("SD 高达 · 关卡自动挑战")
        root.geometry("900x680")
        root.minsize(820, 580)
        root.configure(bg="#f3f5f8")
        root.option_add("*Font", ("Microsoft YaHei", 9))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", foreground="#273447", font=("Microsoft YaHei", 9))
        style.configure("Heading.TLabel", font=("Microsoft YaHei", 14, "bold"), foreground="#17365d")
        style.configure("Muted.TLabel", foreground="#65758a")
        style.configure("Hint.TLabel", foreground="#65758a", font=("Microsoft YaHei", 9))
        style.configure("Status.TLabel", font=("Microsoft YaHei", 9, "bold"), foreground="#17365d")
        style.configure("TButton", padding=(6, 3), font=("Microsoft YaHei", 9), width=6)
        style.configure("Primary.TButton", background="#2769b2", foreground="white")
        style.map("Primary.TButton", background=[("active", "#205895"), ("disabled", "#a6b7cb")])
        style.configure("TCheckbutton", background="#f3f5f8", font=("Microsoft YaHei", 9))
        style.configure("TLabelframe", background="#f3f5f8", bordercolor="#d8e0eb")
        style.configure("TLabelframe.Label", background="#f3f5f8", foreground="#425572", font=("Microsoft YaHei", 9, "bold"))
        style.configure("Loot.Treeview", rowheight=38, font=("Microsoft YaHei", 9))
        style.layout("Loot.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"sticky": ""})]})])
        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="关卡自动挑战", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        controls = ttk.Frame(header)
        controls.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        self.start_button = ttk.Button(controls, text="开始", style="Primary.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(controls, text="暂停", command=lambda: self._control("pause"))
        self.pause_button.pack(side="left", padx=(4, 0))
        self.resume_button = ttk.Button(controls, text="继续", command=lambda: self._control("resume"))
        self.resume_button.pack(side="left", padx=(4, 0))
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop)
        self.stop_button.pack(side="left", padx=(4, 0))
        self.runtime_hint = ttk.Label(header, text="运行期间请保持游戏在模拟器前台；更换关卡前先停止。体力不足或其他提示请手动处理。",
                                      style="Hint.TLabel", wraplength=870)
        self.runtime_hint.grid(row=1, column=0, sticky="w", pady=(4, 0))
        def wrap_hint(event):
            width = max(200, event.width - controls.winfo_reqwidth() - 12)
            if self.runtime_hint.cget("wraplength") != width:
                self.runtime_hint.configure(wraplength=width)
        header.bind("<Configure>", wrap_hint)

        setup = ttk.LabelFrame(body, text="连接与挑战次数", padding=8)
        setup.grid(row=1, column=0, sticky="ew")
        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="ADB 程序").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.path_entry = ttk.Entry(setup, textvariable=self.adb_path)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.browse_button = ttk.Button(setup, text="浏览", command=self._browse)
        self.browse_button.grid(row=0, column=2, sticky="ew", padx=(0, 12))
        ttk.Label(setup, text="模拟器").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.device_combo = ttk.Combobox(setup, textvariable=self.device, state="readonly", width=16,
                                         values=(self.device.get(),) if self.device.get() else ())
        self.device_combo.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=(6, 0))
        self.refresh_button = ttk.Button(setup, text="刷新", command=self._refresh_devices)
        self.refresh_button.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(6, 0))
        options = ttk.Frame(setup)
        options.grid(row=0, column=3, sticky="w")
        ttk.Label(options, text="次数").grid(row=0, column=0, padx=(0, 6))
        self.rounds_entry = ttk.Spinbox(options, from_=1, to=MAX_ROUNDS, width=6, textvariable=self.rounds)
        self.rounds_entry.grid(row=0, column=1)
        ttk.Label(options, text="次").grid(row=0, column=2, padx=(4, 8))
        self.infinite_check = ttk.Checkbutton(options, text="无限循环", variable=self.infinite,
                                             command=self._mode_changed)
        self.infinite_check.grid(row=0, column=3, sticky="w")
        ttk.Label(options, text="有限次数停在奖励页", style="Muted.TLabel").grid(row=0, column=4, padx=(12, 0))
        status_line = ttk.Frame(setup)
        status_line.grid(row=1, column=3, sticky="ew", pady=(6, 0))
        self.status_label = ttk.Label(status_line, textvariable=self.status, style="Status.TLabel", width=16)
        self.status_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.count_label = ttk.Label(status_line, textvariable=self.count, width=22)
        self.count_label.grid(row=0, column=1, sticky="w", padx=(0, 6))
        self.page_label = ttk.Label(status_line, textvariable=self.page, width=16, style="Muted.TLabel")
        self.page_label.grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.progress = ttk.Progressbar(status_line, mode="determinate", maximum=1, length=56)
        self.progress.grid(row=0, column=3, sticky="e")

        content = ttk.Frame(body)
        content.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        content.columnconfigure(0, weight=0)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        loot_panel = ttk.LabelFrame(content, text="战利品统计", padding=8)
        loot_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        loot_panel.columnconfigure(0, weight=1)
        loot_panel.rowconfigure(1, weight=1)
        metrics = ttk.Frame(loot_panel)
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(metrics, textvariable=self.coin_total).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(metrics, textvariable=self.loot_rounds).grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(metrics, textvariable=self.warning_rounds).grid(row=1, column=1, sticky="w")
        self.loot_tree = GridTreeview(loot_panel, rowheight=38, columns=("name", "last", "total", "rounds"),
                                     show="tree headings", style="Loot.Treeview", height=8)
        table_font = tkfont.Font(root, font=("Microsoft YaHei", 9))
        self.loot_tree.heading("#0", text="图标")
        self.loot_tree.column("#0", width=42, minwidth=42, stretch=False, anchor="center")
        for key, title, sample in (("name", "名称", "汉字名称"), ("last", "本轮数量", "9999999"),
                                   ("total", "累计数量", "9,999,999"), ("rounds", "出现\n轮次", "999")):
            width = max(table_font.measure(sample), table_font.measure(title.split("\n")[0])) + 10
            self.loot_tree.heading(key, text=title, anchor="center")
            self.loot_tree.column(key, width=width, minwidth=width, stretch=True, anchor="center")
        self.loot_tree.grid(row=1, column=0, sticky="nsew")
        loot_scroll = ttk.Scrollbar(loot_panel, orient="vertical", command=self.loot_tree.yview)
        loot_scroll.grid(row=1, column=1, sticky="ns")
        self.loot_tree.set_scrollbar(loot_scroll)
        ttk.Label(loot_panel, text="仅统计本次挑战新获得的战利品；无法确认的数量显示「待确认」。",
                  style="Muted.TLabel", wraplength=270).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(content, text="运行记录", padding=6)
        log_frame.grid(row=0, column=1, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, state="disabled", wrap="word", width=28, height=9,
                                font=("Microsoft YaHei", 9), bg="white", fg="#34445b",
                                relief="flat", padx=8, pady=7)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        root.update_idletasks()
        content.columnconfigure(0, minsize=round(loot_panel.winfo_reqwidth() * 1.30) + 8)
        def match_quantity_columns():
            if self._closing or not self.loot_tree.winfo_exists():
                return
            quantity_width = self.loot_tree.column("total", "width")
            for key in ("total", "rounds"):
                self.loot_tree.column(key, width=quantity_width, stretch=False)
            self.loot_tree.heading("rounds", text="出现轮次")
            self.loot_tree.refresh_grid()
        self.loot_tree.bind("<Map>", lambda _event: root.after_idle(match_quantity_columns), add="+")
        if self.loot_tree.winfo_ismapped():
            root.after_idle(match_quantity_columns)

    def _background(self, target) -> None:
        threading.Thread(target=target, daemon=True).start()

    def _post(self, kind: str, payload=None) -> None:
        if kind == "runner" and payload.kind == "frame":
            return
        if not self._closing:
            self.events.put((kind, payload))

    def _selection(self) -> str:
        value = self.device.get().strip()
        return self._device_labels.get(value, value)

    def _snapshot(self, require_device: bool = False, validate_rounds: bool = True) -> AppSettings:
        raw_rounds = self.rounds.get().strip()
        if not raw_rounds.isascii() or not raw_rounds.isdecimal():
            if self.infinite.get() or not validate_rounds:
                raw_rounds = "1"
            else:
                raise ValueError("请填写有效的完成次数（正整数）")
        target = int(raw_rounds)
        if (self.infinite.get() or not validate_rounds) and not 1 <= target <= MAX_ROUNDS:
            target = 1
        settings = AppSettings(adb_path=self.adb_path.get().strip(), device=self._selection(),
                               rounds=target,
                               infinite=self.infinite.get())
        if require_device and not settings.device:
            raise ValueError("请先刷新设备并选择模拟器")
        return settings

    def _persist(self, settings: AppSettings) -> None:
        try:
            with self._settings_lock:
                save_settings(self.settings_path, settings)
        except (OSError, ValueError) as exc:
            self._post("log", f"设置保存失败：{exc}")

    def _browse(self) -> None:
        selected = filedialog.askopenfilename(parent=self.root, title="选择 ADB 程序",
                                              filetypes=[("ADB 程序", "adb.exe"), ("可执行程序", "*.exe"), ("所有文件", "*.*")])
        if selected:
            self.adb_path.set(selected)

    def _mode_changed(self) -> None:
        if not self._active:
            self._infinite_run = self.infinite.get()
            try:
                self._target = self._snapshot().rounds
            except ValueError:
                pass
            self._update_count()
        self._update_controls()

    def _refresh_devices(self) -> None:
        if self._active or self._utility_busy:
            return
        path = self.adb_path.get().strip()
        if not path:
            messagebox.showerror("连接设置", "请先选择 ADB 程序", parent=self.root)
            return
        preferred = self._selection()
        self._utility_busy = True
        self.status.set("正在连接设备…")
        self._update_controls()
        def work():
            try:
                self._post("devices", (AdbClient.list_devices(path), preferred))
            except Exception as exc:
                self._post("utility_error", f"刷新设备失败：{exc}")
        self._background(work)

    def _start(self) -> None:
        if self._active or self._utility_busy or self._closing:
            return
        try:
            settings = self._snapshot(require_device=True)
        except ValueError as exc:
            messagebox.showerror("启动设置", str(exc), parent=self.root)
            return
        self._active = True
        self._paused = self._stopping = self._control_pending = False
        self._completed = 0
        self._run_error = False
        self.loot_ledger.reset()
        self._loot_icons.clear()
        self._update_loot()
        self._target, self._infinite_run = settings.rounds, settings.infinite
        self._cancel.clear()
        self.status.set("正在检查连接…")
        self.page.set("确认游戏位于前台")
        self._update_count()
        self._update_controls()
        def work():
            try:
                self._persist(settings)
                adb = AdbClient(settings.adb_path, settings.device)
                if not adb.is_game_foreground():
                    raise RuntimeError("游戏不在前台；请返回游戏并处理弹窗后重新开始")
                detector = ScreenDetector(self.project_root / "assets" / "profiles" / "default")
                config = RunConfig(rounds=settings.rounds, infinite=settings.infinite,
                                   diagnostics_dir=self.project_root / "logs")
                reader = LootReader()
                runner = BotRunner(adb, detector, config, lambda event: self._post("runner", event),
                                   loot_reader=reader)
                with self._runner_lock:
                    if self._cancel.is_set():
                        self._post("start_cancelled")
                        return
                    self._runner = runner
                    self._post("started")
                    runner.start()
            except Exception as exc:
                self._post("start_error", str(exc))
        self._background(work)

    def _control(self, action: str) -> None:
        if not self._active or self._stopping or self._control_pending:
            return
        self._control_pending = True
        self.status.set("正在暂停…" if action == "pause" else "正在继续…")
        self._update_controls()
        def work():
            try:
                with self._runner_lock:
                    runner = self._runner
                if runner is not None:
                    getattr(runner, action)()
            except Exception as exc:
                self._post("log", f"操作失败：{exc}")
            finally:
                self._post("control_done")
        self._background(work)

    def _stop(self) -> None:
        if not self._active or self._stopping:
            return
        self._stopping = True
        self._cancel.set()
        self.status.set("正在停止…")
        self._update_controls()
        self._background(self._stop_runner)

    def _stop_runner(self) -> None:
        with self._runner_lock:
            runner = self._runner
        if runner is not None:
            runner.stop()

    def _update_controls(self) -> None:
        idle = not self._active and not self._utility_busy and not self._closing
        for widget in (self.path_entry, self.browse_button, self.refresh_button, self.infinite_check):
            widget.configure(state="normal" if idle else "disabled")
        self.device_combo.configure(state="readonly" if idle else "disabled")
        self.rounds_entry.configure(state="normal" if idle and not self.infinite.get() else "disabled")
        self.start_button.configure(state="normal" if idle else "disabled")
        controllable = self._active and not self._stopping and not self._control_pending and not self._closing
        self.pause_button.configure(state="normal" if controllable and not self._paused and self._runner is not None else "disabled")
        self.resume_button.configure(state="normal" if controllable and self._paused else "disabled")
        self.stop_button.configure(state="normal" if self._active and not self._stopping and not self._closing else "disabled")

    def _update_count(self) -> None:
        if self._infinite_run:
            self.count.set(f"完成 {self._completed} 次 · 无限")
            self.progress.configure(maximum=1, value=0)
        else:
            self.count.set(f"完成 {self._completed} / {self._target} 次")
            self.progress.configure(maximum=max(1, self._target), value=self._completed)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        # Keep the widget responsive during long infinite runs.
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 800:
            self.log_text.delete("1.0", f"{lines - 700}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_loot(self) -> None:
        self.coin_total.set(f"金币累计：{self.loot_ledger.coin_total:,}")
        self.loot_rounds.set(f"统计轮数：{self.loot_ledger.rounds}")
        self.warning_rounds.set(f"待确认轮数：{self.loot_ledger.warning_rounds}")
        children = self.loot_tree.get_children()
        if children:
            self.loot_tree.delete(*children)
        for row in self.loot_ledger.rows:
            if row.icon_png and row.item_id not in self._loot_icons:
                try:
                    with Image.open(BytesIO(row.icon_png)) as source:
                        icon = source.convert("RGBA")
                    icon.thumbnail((31, 31), Image.Resampling.LANCZOS)
                    self._loot_icons[row.item_id] = ImageTk.PhotoImage(icon, master=self.root)
                except (OSError, ValueError):
                    pass
            last = "待确认" if row.last_quantity is None else str(row.last_quantity)
            total = f"{row.total_quantity:,}" + (" + 待确认" if row.unknown_rounds else "")
            self.loot_tree.insert("", "end", iid=row.item_id,
                                  image=self._loot_icons.get(row.item_id, ""),
                                  values=(row.name, last, total, row.rounds))
        self.loot_tree.refresh_grid()

    def _end_run(self, message: str) -> None:
        self._active = self._paused = self._stopping = self._control_pending = False
        with self._runner_lock:
            self._runner = None
        self.status.set(message)
        self._update_controls()

    def _runner_event(self, event) -> None:
        if event.kind == "frame":
            return
        if event.kind == "loot" and event.loot is not None:
            if not self.loot_ledger.record_round(event.completed, event.loot):
                return
            self._update_loot()
            for warning in event.loot.warnings:
                self._log("战利品待确认：" + warning)
        if event.state:
            self.page.set(STATE_LABELS.get(event.state, event.state))
        self._completed = event.completed
        self._update_count()
        if event.message:
            self._log(event.message)
        if event.kind == "paused":
            self._paused = True
            if not self._stopping:
                self.status.set("已暂停")
        elif event.kind == "resumed":
            self._paused = False
            if not self._stopping:
                self.status.set("运行中")
        elif event.kind == "state" and not self._stopping:
            self.status.set("运行中")
        elif event.kind == "error":
            self._run_error = True
            self.status.set("发生错误，正在停止")
        elif event.kind == "finished":
            status = "发生错误，已停止" if self._run_error else (
                "目标已完成" if not self._infinite_run and self._completed >= self._target else "已停止")
            self._end_run(status)
        self._update_controls()

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "runner":
            self._runner_event(payload)
        elif kind == "log":
            self._log(payload)
        elif kind == "started":
            if not self._stopping:
                self.status.set("运行中")
            self._update_controls()
        elif kind == "control_done":
            self._control_pending = False
            self._update_controls()
        elif kind in ("start_error", "start_cancelled"):
            self._end_run("启动失败" if kind == "start_error" else "已停止")
            self._log(payload if kind == "start_error" else "启动已取消")
        elif kind == "devices":
            devices, preferred = payload
            self._device_labels = {}
            unavailable = []
            for device in devices:
                if device.state == "device":
                    label = f"{device.serial}  ·  {device.model or '在线设备'}"
                    self._device_labels[label] = device.serial
                else:
                    unavailable.append(f"{device.serial}（{device.state}）")
            values = tuple(self._device_labels)
            self.device_combo.configure(values=values)
            selected = next((label for label, serial in self._device_labels.items() if serial == preferred), values[0] if values else "")
            self.device.set(selected)
            self._utility_busy = False
            self.status.set("设备已连接" if values else "未找到在线设备")
            self._log(f"找到 {len(values)} 台在线设备" + ("；不可用：" + "、".join(unavailable) if unavailable else ""))
            self._update_controls()
        elif kind == "utility_error":
            self._utility_busy = False
            self.status.set("连接失败")
            self._log(payload)
            self._update_controls()

    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            # A bounded batch keeps controls and closing responsive.
            for _ in range(60):
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(kind, payload)
        finally:
            if not self._closing:
                self.root.after(80, self._drain_events)

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel.set()
        self.status.set("正在停止并关闭…")
        self._update_controls()
        try:
            settings = self._snapshot()
        except ValueError:
            settings = None
        stopped = threading.Event()
        def work():
            try:
                self._stop_runner()
                with self._runner_lock:
                    runner = self._runner
                if runner is not None:
                    runner.join(timeout=2.0)
                if settings is not None:
                    self._persist(settings)
            finally:
                stopped.set()
        self._background(work)
        deadline = time.monotonic() + 4.0
        def finish():
            if stopped.is_set() or time.monotonic() >= deadline:
                self.root.destroy()
            else:
                self.root.after(80, finish)
        self.root.after(80, finish)


def launch_ui(project_root: Path) -> None:
    root = tk.Tk()
    RunnerWindow(root, Path(project_root))
    root.mainloop()
