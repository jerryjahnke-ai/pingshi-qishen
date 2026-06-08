# -*- coding: utf-8 -*-
"""
Windowed Windows foreground app usage timer.

Tracks the active foreground app, persists totals frequently, and shows live
session/history statistics in a small Tkinter window. It uses only Python's
standard library and Windows APIs.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "屏时起身：软件时长统计与起立活动提醒"
APP_MUTEX = "Local\\PingShiQiShenMutex"
POLL_SECONDS = 1.0
SAVE_SECONDS = 5.0
MAX_SESSIONS_TO_KEEP = 60
REMINDER_INTERVAL_SECONDS = 30 * 60
ACTIVE_IDLE_LIMIT_SECONDS = 60.0

BASE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PingShiQiShen"
STATE_FILE = BASE_DIR / "sessions.json"
LOG_FILE = BASE_DIR / "logs" / "app.log"


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_ICONERROR = 0x00000010
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000
SW_RESTORE = 9

SELF_PID = kernel32.GetCurrentProcessId()
_MUTEX_HANDLE = None

QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
QueryFullProcessImageNameW.restype = wintypes.BOOL

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log_error(message: str) -> None:
    try:
        ensure_dirs()
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_text()}] {message}\n")
    except Exception:
        pass


def message_box(text: str, title: str = APP_NAME, icon: int = MB_ICONINFORMATION) -> None:
    try:
        user32.MessageBoxW(None, text, title, MB_OK | icon | MB_TOPMOST | MB_SETFOREGROUND)
    except Exception:
        log_error("Failed to show message box:\n" + traceback.format_exc())


def acquire_single_instance_mutex() -> bool:
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, APP_MUTEX)
    if not _MUTEX_HANDLE:
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def bring_existing_window_to_front() -> bool:
    hwnd = user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def read_json_file(path: Path, default: dict) -> dict:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        log_error("Failed to read state:\n" + traceback.format_exc())
    return default


def write_json_file(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def load_state() -> dict:
    state = read_json_file(STATE_FILE, {"sessions": []})
    sessions = state.get("sessions")
    if not isinstance(sessions, list):
        state["sessions"] = []
    return state


def save_state(state: dict) -> None:
    ensure_dirs()
    sessions = state.get("sessions", [])
    if len(sessions) > MAX_SESSIONS_TO_KEEP:
        state["sessions"] = sessions[-MAX_SESSIONS_TO_KEEP:]
    write_json_file(STATE_FILE, state)


def default_reminder_settings() -> dict:
    return {
        "enabled": True,
        "mode": "sitting",
        "updated_at": now_text(),
    }


def get_reminder_settings(state: dict) -> dict:
    reminder = state.get("reminder")
    if not isinstance(reminder, dict):
        reminder = default_reminder_settings()
        state["reminder"] = reminder
    reminder["enabled"] = bool(reminder.get("enabled", True))
    if reminder.get("mode") not in {"sitting", "standing"}:
        reminder["mode"] = "sitting"
    reminder.setdefault("updated_at", now_text())
    return reminder


def get_idle_seconds() -> float:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = kernel32.GetTickCount64() - int(info.dwTime)
    return max(0.0, elapsed_ms / 1000.0)


def is_user_active() -> bool:
    return get_idle_seconds() <= ACTIVE_IDLE_LIMIT_SECONDS


def query_process_path(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


def get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def friendly_app_name(exe_path: str, title: str, pid: int) -> str:
    if pid == SELF_PID:
        return APP_NAME
    if exe_path:
        stem = Path(exe_path).stem
        if stem.lower() in {"applicationframehost", "shellexperiencehost"} and title:
            return title[:80]
        return stem
    if title:
        return title[:80]
    return "未知窗口"


def get_foreground_info() -> dict:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {
            "key": "unknown",
            "name": "未知窗口",
            "exe": "",
            "path": "",
            "title": "",
            "pid": 0,
            "is_self": False,
        }

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = get_window_title(hwnd)
    path = query_process_path(pid.value)
    exe = Path(path).name if path else ""
    is_self = pid.value == SELF_PID
    key = "__self__" if is_self else (path.lower() if path else f"pid:{pid.value}:{title}".lower())
    return {
        "key": key,
        "name": friendly_app_name(path, title, pid.value),
        "exe": exe,
        "path": path,
        "title": title,
        "pid": pid.value,
        "is_self": is_self,
    }


def add_usage(session: dict, app_info: dict, seconds: float) -> None:
    if seconds <= 0 or app_info.get("is_self"):
        return
    apps = session.setdefault("apps", {})
    key = app_info.get("key") or "unknown"
    item = apps.setdefault(
        key,
        {
            "name": app_info.get("name") or "未知窗口",
            "exe": app_info.get("exe") or "",
            "path": app_info.get("path") or "",
            "seconds": 0.0,
            "last_title": "",
            "sessions": 0,
        },
    )
    item["name"] = app_info.get("name") or item.get("name") or "未知窗口"
    item["exe"] = app_info.get("exe") or item.get("exe") or ""
    item["path"] = app_info.get("path") or item.get("path") or ""
    item["last_title"] = app_info.get("title") or item.get("last_title") or ""
    item["seconds"] = float(item.get("seconds", 0.0)) + seconds


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分钟"
    if minutes:
        return f"{minutes}分钟{secs:02d}秒"
    return f"{secs}秒"


def format_percent(part: float, total: float) -> str:
    if total <= 0:
        return "0%"
    return f"{part / total * 100:.1f}%"


def session_total_seconds(session: dict) -> float:
    return sum(float(item.get("seconds", 0.0)) for item in session.get("apps", {}).values())


def sorted_app_items(session: dict) -> list[dict]:
    apps = list(session.get("apps", {}).values())
    apps.sort(key=lambda item: float(item.get("seconds", 0.0)), reverse=True)
    return apps


def aggregate_sessions(sessions: list[dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    for session in sessions:
        for key, item in session.get("apps", {}).items():
            total = totals.setdefault(
                key,
                {
                    "name": item.get("name") or item.get("exe") or "未知窗口",
                    "exe": item.get("exe") or "",
                    "path": item.get("path") or "",
                    "seconds": 0.0,
                    "sessions": 0,
                    "last_title": "",
                },
            )
            total["name"] = item.get("name") or total["name"]
            total["exe"] = item.get("exe") or total["exe"]
            total["path"] = item.get("path") or total["path"]
            total["seconds"] += float(item.get("seconds", 0.0))
            total["sessions"] += 1
            total["last_title"] = item.get("last_title") or total["last_title"]
    rows = list(totals.values())
    rows.sort(key=lambda item: float(item.get("seconds", 0.0)), reverse=True)
    return rows


def build_summary(session: dict) -> str:
    apps = sorted_app_items(session)
    started = session.get("started_at", "未知")
    ended = session.get("updated_at") or session.get("ended_at") or "未知"
    total = format_duration(session_total_seconds(session))
    lines = [
        "上一次开机使用统计",
        "",
        f"开始：{started}",
        f"结束：{ended}",
        f"合计：{total}",
        "",
    ]

    if not apps:
        lines.append("上一轮还没有记录到可统计的软件。")
    else:
        for index, app in enumerate(apps[:10], start=1):
            name = app.get("name") or app.get("exe") or "未知窗口"
            duration = format_duration(float(app.get("seconds", 0.0)))
            lines.append(f"{index}. {name}：{duration}")
    return "\n".join(lines)


def consume_previous_session_summary(state: dict) -> str:
    sessions = state.get("sessions", [])
    pending = [session for session in sessions if not session.get("reported")]
    if not pending:
        return ""

    previous = pending[-1]
    summary = build_summary(previous)
    for session in pending:
        session["reported"] = True
        session["reported_at"] = now_text()
    save_state(state)
    return summary


def create_session(state: dict) -> dict:
    session = {
        "id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "started_at": now_text(),
        "updated_at": now_text(),
        "reported": False,
        "app_version": "3-posture-reminder",
        "apps": {},
    }
    state.setdefault("sessions", []).append(session)
    save_state(state)
    return session


def same_app(left: dict, right: dict) -> bool:
    return (left or {}).get("key") == (right or {}).get("key")


class UsageTimerWindow:
    def __init__(self) -> None:
        ensure_dirs()
        self.state = load_state()
        self.previous_summary = consume_previous_session_summary(self.state)
        self.reminder = get_reminder_settings(self.state)
        self.session = create_session(self.state)
        self.current_app = get_foreground_info()
        self.last_tick = time.monotonic()
        self.last_save = self.last_tick
        self.posture_elapsed = 0.0
        self.user_active = is_user_active()
        self.reminder_window: tk.Toplevel | None = None
        self.stopped = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("930x690")
        self.root.minsize(800, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.confirm_close_from_x)

        self.current_app_var = tk.StringVar(value="正在读取...")
        self.current_time_var = tk.StringVar(value="0秒")
        self.total_time_var = tk.StringVar(value="0秒")
        self.started_var = tk.StringVar(value=self.session["started_at"])
        self.saved_var = tk.StringVar(value="刚刚")
        self.status_var = tk.StringVar(value="正在记录")
        self.reminder_mode_var = tk.StringVar(value="")
        self.reminder_next_var = tk.StringVar(value="")
        self.reminder_active_var = tk.StringVar(value="")
        self.reminder_toggle_text = tk.StringVar(value="")

        self.live_tree: ttk.Treeview
        self.history_tree: ttk.Treeview
        self.build_ui()

        if self.previous_summary:
            self.root.after(600, self.show_previous_summary)
        self.root.after(250, self.tick)
        self.root.after(500, self.refresh_ui)

    def build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Reminder.TButton", font=("Microsoft YaHei UI", 11), padding=(12, 8))
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")

        current = ttk.LabelFrame(outer, text="正在计时", padding=12)
        current.pack(fill="x", pady=(12, 8))
        ttk.Label(current, textvariable=self.current_app_var, style="Metric.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(current, textvariable=self.current_time_var, style="Metric.TLabel").grid(
            row=0, column=1, sticky="e", padx=(20, 0)
        )
        current.columnconfigure(0, weight=1)

        metrics = ttk.Frame(outer)
        metrics.pack(fill="x", pady=(0, 10))
        self.add_metric(metrics, "本轮合计", self.total_time_var, 0)
        self.add_metric(metrics, "开始时间", self.started_var, 1)
        self.add_metric(metrics, "最近保存", self.saved_var, 2)

        reminder = ttk.LabelFrame(outer, text="久坐提醒", padding=10)
        reminder.pack(fill="x", pady=(0, 10))
        reminder.columnconfigure(0, weight=1)
        reminder.columnconfigure(1, weight=1)
        reminder.columnconfigure(2, weight=1)
        ttk.Label(reminder, textvariable=self.reminder_mode_var, style="Metric.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(reminder, textvariable=self.reminder_next_var).grid(row=1, column=0, sticky="w")
        ttk.Label(reminder, textvariable=self.reminder_active_var).grid(row=1, column=1, sticky="w")
        actions = ttk.Frame(reminder)
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(actions, text="正在站立", command=self.mark_standing).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="坐下办公", command=self.mark_sitting).pack(side="left", padx=(0, 6))
        ttk.Button(actions, textvariable=self.reminder_toggle_text, command=self.toggle_reminder).pack(
            side="left"
        )

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        live_tab = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        history_tab = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        notebook.add(live_tab, text="本轮实时")
        notebook.add(history_tab, text="历史总计")

        self.live_tree = self.create_tree(
            live_tab,
            ("rank", "name", "duration", "share", "title"),
            {
                "rank": ("#", 48, "center"),
                "name": ("软件", 170, "w"),
                "duration": ("使用时长", 110, "e"),
                "share": ("占比", 80, "e"),
                "title": ("最近窗口", 360, "w"),
            },
        )
        self.history_tree = self.create_tree(
            history_tab,
            ("rank", "name", "duration", "sessions", "title"),
            {
                "rank": ("#", 48, "center"),
                "name": ("软件", 180, "w"),
                "duration": ("总时长", 120, "e"),
                "sessions": ("出现次数", 90, "e"),
                "title": ("最近窗口", 360, "w"),
            },
        )

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="刷新", command=self.refresh_ui).pack(side="left")
        ttk.Button(footer, text="打开记录", command=self.open_data_folder).pack(side="left", padx=8)
        ttk.Button(footer, text="最小化", command=self.root.iconify).pack(side="right")
        ttk.Button(footer, text="退出", command=self.confirm_exit).pack(side="right", padx=(0, 8))

    def add_metric(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int) -> None:
        frame = ttk.LabelFrame(parent, text=label, padding=(10, 8))
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, textvariable=variable, style="Metric.TLabel").pack(anchor="w")
        parent.columnconfigure(column, weight=1)

    def create_tree(self, parent: ttk.Frame, columns: tuple[str, ...], meta: dict) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for column in columns:
            title, width, anchor = meta[column]
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=anchor, stretch=column == columns[-1])
        return tree

    def tick(self) -> None:
        if self.stopped:
            return
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now

        try:
            next_app = get_foreground_info()
            add_usage(self.session, self.current_app, elapsed)
            self.process_posture_reminder(elapsed)
            self.session["updated_at"] = now_text()

            changed = not same_app(self.current_app, next_app)
            self.current_app = next_app
            if changed or now - self.last_save >= SAVE_SECONDS:
                save_state(self.state)
                self.last_save = now
                self.saved_var.set(datetime.now().strftime("%H:%M:%S"))
        except Exception:
            self.status_var.set("记录异常，已写入日志")
            log_error("Timer loop failed:\n" + traceback.format_exc())

        self.refresh_ui()
        self.root.after(int(POLL_SECONDS * 1000), self.tick)

    def refresh_ui(self) -> None:
        if self.stopped:
            return
        total = session_total_seconds(self.session)
        self.total_time_var.set(format_duration(total))

        if self.current_app.get("is_self"):
            self.current_app_var.set(f"当前窗口：{APP_NAME}")
            self.current_time_var.set("查看中")
        else:
            app_name = self.current_app.get("name") or "未知窗口"
            current_item = self.session.get("apps", {}).get(self.current_app.get("key"), {})
            self.current_app_var.set(f"当前窗口：{app_name}")
            self.current_time_var.set(format_duration(float(current_item.get("seconds", 0.0))))

        self.fill_live_tree(total)
        self.fill_history_tree()
        self.refresh_reminder_ui()

    def fill_live_tree(self, total: float) -> None:
        self.live_tree.delete(*self.live_tree.get_children())
        for index, item in enumerate(sorted_app_items(self.session), start=1):
            seconds = float(item.get("seconds", 0.0))
            self.live_tree.insert(
                "",
                "end",
                values=(
                    index,
                    item.get("name") or item.get("exe") or "未知窗口",
                    format_duration(seconds),
                    format_percent(seconds, total),
                    item.get("last_title") or "",
                ),
            )

    def fill_history_tree(self) -> None:
        self.history_tree.delete(*self.history_tree.get_children())
        for index, item in enumerate(aggregate_sessions(self.state.get("sessions", [])), start=1):
            self.history_tree.insert(
                "",
                "end",
                values=(
                    index,
                    item.get("name") or item.get("exe") or "未知窗口",
                    format_duration(float(item.get("seconds", 0.0))),
                    item.get("sessions", 0),
                    item.get("last_title") or "",
                ),
            )

    def open_data_folder(self) -> None:
        ensure_dirs()
        try:
            os.startfile(BASE_DIR)
        except Exception:
            messagebox.showerror(APP_NAME, f"打不开记录文件夹：\n{BASE_DIR}")

    def process_posture_reminder(self, elapsed: float) -> None:
        self.user_active = is_user_active()
        if not self.reminder.get("enabled", True):
            return
        if not self.user_active:
            return

        # Clamp the reminder counter so sleep/resume or a paused UI loop does not
        # accidentally count a long break as active sitting or standing time.
        self.posture_elapsed += min(max(elapsed, 0.0), 5.0)
        if self.posture_elapsed >= REMINDER_INTERVAL_SECONDS:
            self.show_posture_reminder()

    def refresh_reminder_ui(self) -> None:
        enabled = self.reminder.get("enabled", True)
        mode = self.reminder.get("mode", "sitting")
        if not enabled:
            self.reminder_mode_var.set("提醒已关闭")
            self.reminder_next_var.set("沉浸工作中，不打扰。")
            self.reminder_active_var.set("")
            self.reminder_toggle_text.set("开启提醒")
            return

        mode_text = "站立办公" if mode == "standing" else "坐下办公"
        remaining = max(0.0, REMINDER_INTERVAL_SECONDS - self.posture_elapsed)
        self.reminder_mode_var.set(f"当前：{mode_text}")
        self.reminder_next_var.set(f"下一次提醒：{format_duration(remaining)}")
        if self.user_active:
            self.reminder_active_var.set("检测：正在使用屏幕")
        else:
            self.reminder_active_var.set("检测：暂时没有操作，不累计")
        self.reminder_toggle_text.set("关闭提醒")

    def save_reminder_settings(self) -> None:
        self.reminder["updated_at"] = now_text()
        self.state["reminder"] = self.reminder
        save_state(self.state)

    def set_posture_mode(self, mode: str) -> None:
        self.reminder["enabled"] = True
        self.reminder["mode"] = mode
        self.posture_elapsed = 0.0
        self.save_reminder_settings()
        self.refresh_reminder_ui()

    def mark_standing(self) -> None:
        self.set_posture_mode("standing")

    def mark_sitting(self) -> None:
        self.set_posture_mode("sitting")

    def toggle_reminder(self) -> None:
        self.reminder["enabled"] = not self.reminder.get("enabled", True)
        self.posture_elapsed = 0.0
        self.save_reminder_settings()
        self.refresh_reminder_ui()

    def reset_posture_interval(self) -> None:
        self.posture_elapsed = 0.0
        self.save_reminder_settings()
        self.refresh_reminder_ui()

    def show_posture_reminder(self) -> None:
        if self.reminder_window is not None and self.reminder_window.winfo_exists():
            return

        mode = self.reminder.get("mode", "sitting")
        root_was_iconic = self.root.state() == "iconic"
        title = "该活动一下了" if mode == "sitting" else "站立办公提醒"
        kicker = "久坐提醒" if mode == "sitting" else "站立提醒"
        body = (
            "你已经坐着连续使用屏幕约 30 分钟了。\n站起来活动一下，走几步，伸展肩颈。"
            if mode == "sitting"
            else "你已经站立办公约 30 分钟了。\n收腹，站直，肩膀放松，别把身体重心一直压在同一边。"
        )

        win = tk.Toplevel(self.root)
        self.reminder_window = win
        win.title(title)
        win.geometry("470x260")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        win.protocol("WM_DELETE_WINDOW", lambda: self.close_posture_reminder("later"))

        bg = "#f7fafc"
        accent = "#2563eb" if mode == "sitting" else "#0f766e"
        title_color = "#172033"
        body_color = "#344256"
        win.configure(bg=bg)

        shell = tk.Frame(win, bg=bg, padx=20, pady=18)
        shell.pack(fill="both", expand=True)
        content = tk.Frame(
            shell,
            bg="#ffffff",
            highlightbackground="#d7dee8",
            highlightthickness=1,
            padx=20,
            pady=18,
        )
        content.pack(fill="both", expand=True)

        tk.Frame(content, bg=accent, width=5).pack(side="left", fill="y", padx=(0, 16))
        main = tk.Frame(content, bg="#ffffff")
        main.pack(side="left", fill="both", expand=True)
        tk.Label(
            main,
            text=kicker,
            bg="#ffffff",
            fg=accent,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            main,
            text=title,
            bg="#ffffff",
            fg=title_color,
            font=("Microsoft YaHei UI", 20, "bold"),
        ).pack(anchor="w", pady=(4, 8))
        tk.Label(
            main,
            text=body,
            bg="#ffffff",
            fg=body_color,
            font=("Microsoft YaHei UI", 13),
            wraplength=365,
            justify="left",
            spacing2=4,
        ).pack(anchor="w")
        buttons = ttk.Frame(main)
        buttons.pack(fill="x", side="bottom", pady=(16, 0))
        if mode == "sitting":
            ttk.Button(
                buttons,
                text="我正在站立",
                command=lambda: self.close_posture_reminder("standing"),
                style="Reminder.TButton",
            ).pack(side="left")
            ttk.Button(
                buttons,
                text="稍后再提醒",
                command=lambda: self.close_posture_reminder("later"),
                style="Reminder.TButton",
            ).pack(side="left", padx=8)
        else:
            ttk.Button(
                buttons,
                text="继续站立",
                command=lambda: self.close_posture_reminder("later"),
                style="Reminder.TButton",
            ).pack(side="left")
            ttk.Button(
                buttons,
                text="坐下办公",
                command=lambda: self.close_posture_reminder("sitting"),
                style="Reminder.TButton",
            ).pack(side="left", padx=8)
        ttk.Button(
            buttons,
            text="关闭提醒",
            command=lambda: self.close_posture_reminder("off"),
            style="Reminder.TButton",
        ).pack(side="right")

        win.update_idletasks()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = max(20, screen_w - win.winfo_width() - 36)
        y = max(20, screen_h - win.winfo_height() - 76)
        win.geometry(f"+{x}+{y}")
        win.lift()
        win.focus_force()
        if root_was_iconic:
            self.root.after(50, self.root.iconify)

    def close_posture_reminder(self, action: str) -> None:
        if self.reminder_window is not None and self.reminder_window.winfo_exists():
            self.reminder_window.destroy()
        self.reminder_window = None

        if action == "standing":
            self.set_posture_mode("standing")
        elif action == "sitting":
            self.set_posture_mode("sitting")
        elif action == "off":
            self.reminder["enabled"] = False
            self.posture_elapsed = 0.0
            self.save_reminder_settings()
            self.refresh_reminder_ui()
        else:
            self.reset_posture_interval()

    def show_previous_summary(self) -> None:
        threading.Thread(
            target=message_box,
            args=(self.previous_summary, "上一次使用统计"),
            daemon=True,
        ).start()

    def confirm_exit(self) -> None:
        if not messagebox.askyesno(APP_NAME, "退出后会停止记录，确定退出吗？"):
            return
        self.stop_and_exit()

    def confirm_close_from_x(self) -> None:
        should_exit = messagebox.askyesno(
            APP_NAME,
            "要从电脑后台退出吗？\n\n选择“是”：停止记录并退出。\n选择“否”：只最小化窗口，继续在后台记录。",
        )
        if should_exit:
            self.stop_and_exit()
        else:
            self.root.iconify()

    def stop_and_exit(self) -> None:
        self.stopped = True
        self.session["updated_at"] = now_text()
        save_state(self.state)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def smoke_test() -> None:
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ensure_dirs()
    info = get_foreground_info()
    state = load_state()
    reminder = get_reminder_settings(state)
    print(
        json.dumps(
            {
                "ok": True,
                "foreground": info,
                "idle_seconds": round(get_idle_seconds(), 2),
                "user_active": is_user_active(),
                "reminder": reminder,
                "sessions": len(state.get("sessions", [])),
                "state_file": str(STATE_FILE),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    if "--smoke-test" in sys.argv:
        smoke_test()
        return

    ensure_dirs()
    if not acquire_single_instance_mutex():
        if not bring_existing_window_to_front():
            message_box(f"{APP_NAME} 已经在运行。")
        return

    UsageTimerWindow().run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("Fatal error:\n" + traceback.format_exc())
        message_box(f"{APP_NAME} 启动失败。\n\n错误日志位置：\n{LOG_FILE}", icon=MB_ICONERROR)
