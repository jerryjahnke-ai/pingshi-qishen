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

THEME = {
    "bg": "#07090f",
    "bg_soft": "#0c1018",
    "panel": "#111722",
    "panel_alt": "#151d2b",
    "panel_high": "#1b2535",
    "border": "#3a321f",
    "border_soft": "#263143",
    "gold": "#d8aa45",
    "gold_light": "#f1d386",
    "gold_dark": "#8f6b24",
    "text": "#f6f1e6",
    "muted": "#a6adba",
    "muted_dark": "#747c8d",
    "danger": "#f0b35f",
    "shadow": "#05070b",
}

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

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

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
        "app_version": "5-ui-state-scroll-fixes",
        "apps": {},
    }
    state.setdefault("sessions", []).append(session)
    save_state(state)
    return session


def same_app(left: dict, right: dict) -> bool:
    return (left or {}).get("key") == (right or {}).get("key")


def draw_rounded_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    *,
    fill: str,
    outline: str,
    width: int = 1,
    tags: str = "round",
) -> None:
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    canvas.create_polygon(
        points,
        smooth=True,
        splinesteps=18,
        fill=fill,
        outline=outline,
        width=width,
        tags=tags,
    )


class RoundedPanel(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        fill: str = THEME["panel"],
        border: str = THEME["border_soft"],
        bg: str = THEME["bg"],
        radius: int = 18,
        padding: int | tuple[int, int] | tuple[int, int, int, int] = 16,
        border_width: int = 1,
        height: int = 1,
    ) -> None:
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0)
        if isinstance(padding, int):
            pad_left = pad_top = pad_right = pad_bottom = padding
        elif len(padding) == 2:
            pad_left = pad_right = padding[0]
            pad_top = pad_bottom = padding[1]
        else:
            pad_left, pad_top, pad_right, pad_bottom = padding
        self.fill = fill
        self.border = border
        self.radius = radius
        self.pad_left = pad_left
        self.pad_top = pad_top
        self.pad_right = pad_right
        self.pad_bottom = pad_bottom
        self.border_width = border_width
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=height)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=fill, highlightthickness=0, bd=0)
        self.body_window = self.canvas.create_window(
            self.pad_left,
            self.pad_top,
            window=self.body,
            anchor="nw",
        )
        self.canvas.bind("<Configure>", self._redraw)

    def _redraw(self, event: tk.Event) -> None:
        self.canvas.delete("round")
        width = max(2, event.width)
        height = max(2, event.height)
        draw_rounded_rect(
            self.canvas,
            1,
            1,
            width - 2,
            height - 2,
            self.radius,
            fill=self.fill,
            outline=self.border,
            width=self.border_width,
            tags="round",
        )
        self.canvas.tag_lower("round")
        self.canvas.itemconfigure(
            self.body_window,
            width=max(1, width - self.pad_left - self.pad_right),
            height=max(1, height - self.pad_top - self.pad_bottom),
        )


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
        self.active_tab = "live"
        self.standing_button: ttk.Button | None = None
        self.sitting_button: ttk.Button | None = None
        self.reminder_toggle_button: ttk.Button | None = None
        self.live_tab_button: ttk.Button | None = None
        self.history_tab_button: ttk.Button | None = None
        self.live_tab_frame: tk.Frame | None = None
        self.history_tab_frame: tk.Frame | None = None
        self.page_canvas: tk.Canvas | None = None
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
        self.root.configure(bg=THEME["bg"])
        style.configure("App.TFrame", background=THEME["bg"])
        style.configure("Panel.TFrame", background=THEME["panel"])
        style.configure("Selected.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 8))
        style.configure("Neutral.TButton", font=("Microsoft YaHei UI", 10), padding=(14, 8))
        style.configure(
            "Reminder.TButton",
            font=("Microsoft YaHei UI", 11, "bold"),
            padding=(14, 9),
        )
        for button_style in ("Selected.TButton", "Reminder.TButton"):
            style.configure(
                button_style,
                foreground="#12100a",
                background=THEME["gold"],
                bordercolor=THEME["gold_dark"],
                lightcolor=THEME["gold"],
                darkcolor=THEME["gold_dark"],
                focuscolor=THEME["gold"],
            )
            style.map(
                button_style,
                background=[("active", THEME["gold_light"]), ("pressed", THEME["gold_dark"])],
                foreground=[("disabled", THEME["muted_dark"])],
            )
        style.configure(
            "Neutral.TButton",
            foreground=THEME["text"],
            background=THEME["panel_high"],
            bordercolor=THEME["border_soft"],
            lightcolor=THEME["panel_high"],
            darkcolor=THEME["bg_soft"],
            focuscolor=THEME["gold"],
        )
        style.map(
            "Neutral.TButton",
            background=[("active", "#243049"), ("pressed", "#0f1420")],
            foreground=[("disabled", THEME["muted_dark"])],
        )
        style.configure(
            "Treeview",
            rowheight=30,
            background=THEME["panel"],
            fieldbackground=THEME["panel"],
            foreground=THEME["text"],
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
        )
        style.map("Treeview", background=[("selected", THEME["gold_dark"])])
        style.configure(
            "Treeview.Heading",
            background=THEME["panel_high"],
            foreground=THEME["gold_light"],
            borderwidth=0,
            relief="flat",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Vertical.TScrollbar",
            background=THEME["panel_high"],
            troughcolor=THEME["bg_soft"],
            bordercolor=THEME["bg_soft"],
            arrowcolor=THEME["gold"],
        )

        scroll_shell = tk.Frame(self.root, bg=THEME["bg"])
        scroll_shell.pack(fill="both", expand=True)
        page_canvas = tk.Canvas(scroll_shell, bg=THEME["bg"], highlightthickness=0, bd=0)
        page_scroll = ttk.Scrollbar(
            scroll_shell,
            orient="vertical",
            command=page_canvas.yview,
            style="Vertical.TScrollbar",
        )
        page_canvas.configure(yscrollcommand=page_scroll.set)
        page_canvas.pack(side="left", fill="both", expand=True)
        page_scroll.pack(side="right", fill="y")
        self.page_canvas = page_canvas

        outer = tk.Frame(page_canvas, bg=THEME["bg"], padx=18, pady=16)
        outer_window = page_canvas.create_window(0, 0, window=outer, anchor="nw")

        def sync_scroll_region(event: tk.Event | None = None) -> None:
            page_canvas.configure(scrollregion=page_canvas.bbox("all"))

        def sync_page_width(event: tk.Event) -> None:
            page_canvas.itemconfigure(outer_window, width=event.width)

        def on_mousewheel(event: tk.Event) -> None:
            page_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        outer.bind("<Configure>", sync_scroll_region)
        page_canvas.bind("<Configure>", sync_page_width)
        self.root.bind("<MouseWheel>", on_mousewheel)

        header_panel = RoundedPanel(
            outer,
            fill=THEME["bg_soft"],
            border=THEME["border"],
            bg=THEME["bg"],
            radius=24,
            padding=(20, 14, 20, 14),
            height=96,
        )
        header_panel.pack(fill="x")
        header = header_panel.body
        title_group = tk.Frame(header, bg=THEME["bg_soft"])
        title_group.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_group,
            text=APP_NAME,
            bg=THEME["bg_soft"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_group,
            text="Screen-Time Rise  ·  App Usage Tracking  ·  Stand-Up Reminder",
            bg=THEME["bg_soft"],
            fg=THEME["gold_light"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 0))
        status_badge = tk.Label(
            header,
            textvariable=self.status_var,
            bg=THEME["panel_high"],
            fg=THEME["gold_light"],
            padx=14,
            pady=7,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        status_badge.pack(side="right")

        current_panel = RoundedPanel(
            outer,
            fill=THEME["panel"],
            border=THEME["border"],
            bg=THEME["bg"],
            radius=22,
            padding=(20, 16, 20, 16),
            height=100,
        )
        current_panel.pack(fill="x", pady=(14, 10))
        current = current_panel.body
        tk.Label(
            current,
            text="正在计时",
            bg=THEME["panel"],
            fg=THEME["gold_light"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            current,
            textvariable=self.current_app_var,
            bg=THEME["panel"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 15, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Label(
            current,
            textvariable=self.current_time_var,
            bg=THEME["panel"],
            fg=THEME["gold"],
            font=("Segoe UI", 22, "bold"),
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 0))
        current.columnconfigure(0, weight=1)

        metrics = tk.Frame(outer, bg=THEME["bg"])
        metrics.pack(fill="x", pady=(0, 10))
        self.add_metric(metrics, "本轮合计", self.total_time_var, 0)
        self.add_metric(metrics, "开始时间", self.started_var, 1)
        self.add_metric(metrics, "最近保存", self.saved_var, 2)

        reminder_panel = RoundedPanel(
            outer,
            fill=THEME["panel_alt"],
            border=THEME["border"],
            bg=THEME["bg"],
            radius=22,
            padding=(18, 14, 18, 14),
            height=174,
        )
        reminder_panel.pack(fill="x", pady=(0, 12))
        reminder = reminder_panel.body
        reminder.columnconfigure(0, weight=1)
        reminder.columnconfigure(1, weight=1)
        tk.Label(
            reminder,
            text="久坐提醒",
            bg=THEME["panel_alt"],
            fg=THEME["gold_light"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            reminder,
            textvariable=self.reminder_mode_var,
            bg=THEME["panel_alt"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 14, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        tk.Label(
            reminder,
            textvariable=self.reminder_next_var,
            bg=THEME["panel_alt"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 10),
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        tk.Label(
            reminder,
            textvariable=self.reminder_active_var,
            bg=THEME["panel_alt"],
            fg=THEME["gold_light"],
            font=("Microsoft YaHei UI", 10),
        ).grid(row=2, column=1, sticky="w", pady=(4, 0))
        actions = tk.Frame(reminder, bg=THEME["panel_alt"])
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.standing_button = ttk.Button(
            actions,
            text="正在站立",
            command=self.mark_standing,
            style="Neutral.TButton",
        )
        self.standing_button.pack(side="left", padx=(0, 8))
        self.sitting_button = ttk.Button(
            actions,
            text="坐下办公",
            command=self.mark_sitting,
            style="Neutral.TButton",
        )
        self.sitting_button.pack(side="left", padx=(0, 8))
        self.reminder_toggle_button = ttk.Button(
            actions,
            textvariable=self.reminder_toggle_text,
            command=self.toggle_reminder,
            style="Neutral.TButton",
        )
        self.reminder_toggle_button.pack(side="left")

        tabs = tk.Frame(outer, bg=THEME["bg"])
        tabs.pack(fill="x", pady=(4, 0))
        tab_buttons = tk.Frame(tabs, bg=THEME["bg"])
        tab_buttons.pack(fill="x")
        self.live_tab_button = ttk.Button(
            tab_buttons,
            text="本轮实时",
            command=lambda: self.show_tab("live"),
            style="Selected.TButton",
        )
        self.live_tab_button.pack(side="left")
        self.history_tab_button = ttk.Button(
            tab_buttons,
            text="历史总计",
            command=lambda: self.show_tab("history"),
            style="Neutral.TButton",
        )
        self.history_tab_button.pack(side="left", padx=(8, 0))

        tab_actions = tk.Frame(tabs, bg=THEME["bg"])
        tab_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(tab_actions, text="刷新", command=self.refresh_ui, style="Neutral.TButton").pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            tab_actions,
            text="打开记录",
            command=self.open_data_folder,
            style="Neutral.TButton",
        ).pack(side="left", padx=(0, 8))
        ttk.Button(tab_actions, text="退出", command=self.confirm_exit, style="Neutral.TButton").pack(
            side="left"
        )

        tab_content = tk.Frame(outer, bg=THEME["bg"])
        tab_content.pack(fill="both", expand=True)
        self.live_tab_frame = tk.Frame(tab_content, bg=THEME["bg"])
        self.history_tab_frame = tk.Frame(tab_content, bg=THEME["bg"])

        self.live_tree = self.create_tree(
            self.live_tab_frame,
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
            self.history_tab_frame,
            ("rank", "name", "duration", "sessions", "title"),
            {
                "rank": ("#", 48, "center"),
                "name": ("软件", 180, "w"),
                "duration": ("总时长", 120, "e"),
                "sessions": ("出现次数", 90, "e"),
                "title": ("最近窗口", 360, "w"),
            },
        )
        self.show_tab("live")

    def add_metric(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int) -> None:
        panel = RoundedPanel(
            parent,
            fill=THEME["panel"],
            border=THEME["border_soft"],
            bg=THEME["bg"],
            radius=18,
            padding=(14, 10, 14, 10),
            height=82,
        )
        panel.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
        tk.Label(
            panel.body,
            text=label,
            bg=THEME["panel"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            panel.body,
            textvariable=variable,
            bg=THEME["panel"],
            fg=THEME["gold_light"],
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w", pady=(4, 0))
        parent.columnconfigure(column, weight=1)

    def create_tree(self, parent: ttk.Frame, columns: tuple[str, ...], meta: dict) -> ttk.Treeview:
        panel = RoundedPanel(
            parent,
            fill=THEME["panel"],
            border=THEME["border_soft"],
            bg=THEME["bg"],
            radius=20,
            padding=(12, 12, 12, 12),
            height=230,
        )
        panel.pack(fill="both", expand=True, pady=(10, 0))
        frame = tk.Frame(panel.body, bg=THEME["panel"])
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview, style="Vertical.TScrollbar")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for column in columns:
            title, width, anchor = meta[column]
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=anchor, stretch=column == columns[-1])
        return tree

    def show_tab(self, tab_name: str) -> None:
        self.active_tab = tab_name
        if self.live_tab_frame is not None:
            self.live_tab_frame.pack_forget()
        if self.history_tab_frame is not None:
            self.history_tab_frame.pack_forget()

        if tab_name == "history":
            if self.history_tab_frame is not None:
                self.history_tab_frame.pack(fill="both", expand=True)
            if self.live_tab_button is not None:
                self.live_tab_button.configure(style="Neutral.TButton")
            if self.history_tab_button is not None:
                self.history_tab_button.configure(style="Selected.TButton")
        else:
            if self.live_tab_frame is not None:
                self.live_tab_frame.pack(fill="both", expand=True)
            if self.live_tab_button is not None:
                self.live_tab_button.configure(style="Selected.TButton")
            if self.history_tab_button is not None:
                self.history_tab_button.configure(style="Neutral.TButton")

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
            self.refresh_reminder_buttons()
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
        self.refresh_reminder_buttons()

    def refresh_reminder_buttons(self) -> None:
        enabled = self.reminder.get("enabled", True)
        mode = self.reminder.get("mode", "sitting")
        if self.standing_button is not None:
            self.standing_button.configure(
                style="Selected.TButton" if enabled and mode == "standing" else "Neutral.TButton"
            )
        if self.sitting_button is not None:
            self.sitting_button.configure(
                style="Selected.TButton" if enabled and mode == "sitting" else "Neutral.TButton"
            )
        if self.reminder_toggle_button is not None:
            self.reminder_toggle_button.configure(
                style="Selected.TButton" if not enabled else "Neutral.TButton"
            )

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
        title = "别总是坐着！活动活动。" if mode == "sitting" else "注意站姿，别挺肚子。"
        kicker = "久坐提醒" if mode == "sitting" else "站立提醒"
        body = (
            "连续使用屏幕约 30 分钟了。\n站起来走几步，伸展一下肩颈。"
            if mode == "sitting"
            else "站立办公约 30 分钟了。\n收腹，站直，肩膀放松，别把重心一直压在同一边。"
        )

        win = tk.Toplevel(self.root)
        self.reminder_window = win
        win.title(title)
        win.geometry("500x270")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        win.protocol("WM_DELETE_WINDOW", lambda: self.close_posture_reminder("later"))

        bg = THEME["bg"]
        accent = THEME["gold"]
        title_color = THEME["text"]
        body_color = "#d9deea"
        win.configure(bg=bg)

        shell = tk.Frame(win, bg=bg, padx=18, pady=16)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(
            shell,
            fill=THEME["panel"],
            border=THEME["border"],
            bg=bg,
            radius=24,
            padding=(20, 18, 20, 18),
        )
        card.pack(fill="both", expand=True)
        content = card.body

        tk.Frame(content, bg=accent, width=5).pack(side="left", fill="y", padx=(0, 16))
        main = tk.Frame(content, bg=THEME["panel"])
        main.pack(side="left", fill="both", expand=True)
        tk.Label(
            main,
            text=kicker,
            bg=THEME["panel"],
            fg=accent,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            main,
            text=title,
            bg=THEME["panel"],
            fg=title_color,
            font=("Microsoft YaHei UI", 21, "bold"),
        ).pack(anchor="w", pady=(5, 10))
        tk.Label(
            main,
            text=body,
            bg=THEME["panel"],
            fg=body_color,
            font=("Microsoft YaHei UI", 14),
            wraplength=390,
            justify="left",
        ).pack(anchor="w")
        buttons = tk.Frame(main, bg=THEME["panel"])
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
        for _ in range(20):
            if bring_existing_window_to_front():
                return
            time.sleep(0.1)
        return

    UsageTimerWindow().run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("Fatal error:\n" + traceback.format_exc())
        message_box(f"{APP_NAME} 启动失败。\n\n错误日志位置：\n{LOG_FILE}", icon=MB_ICONERROR)
