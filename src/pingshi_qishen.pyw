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
EYE_REMINDER_INTERVAL_SECONDS = 45 * 60
EYE_ZONE_SECONDS = 60
ACTIVE_IDLE_LIMIT_SECONDS = 60.0

ZONE_SCENES = (
    ("马尔代夫环礁", "Indian Ocean · 海水、沙洲、珊瑚环"),
    ("冰岛黑沙海岸", "North Atlantic · 海岸线与浪花"),
    ("阿尔卑斯雪峰", "Europe · 雪峰与云影"),
    ("撒哈拉沙丘", "North Africa · 金色沙脊"),
    ("新西兰峡湾", "South Pacific · 峡湾与群山"),
    ("云南梯田", "China · 云南梯田水光"),
)

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


def default_eye_reminder_settings() -> dict:
    return {
        "enabled": True,
        "updated_at": now_text(),
    }


def get_eye_reminder_settings(state: dict) -> dict:
    eye_reminder = state.get("eye_reminder")
    if not isinstance(eye_reminder, dict):
        eye_reminder = default_eye_reminder_settings()
        state["eye_reminder"] = eye_reminder
    eye_reminder["enabled"] = bool(eye_reminder.get("enabled", True))
    eye_reminder.setdefault("updated_at", now_text())
    return eye_reminder


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
        "app_version": "6-eye-zone-reminder",
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
        self.eye_reminder = get_eye_reminder_settings(self.state)
        self.session = create_session(self.state)
        self.current_app = get_foreground_info()
        self.last_tick = time.monotonic()
        self.last_save = self.last_tick
        self.posture_elapsed = 0.0
        self.eye_elapsed = 0.0
        self.user_active = is_user_active()
        self.reminder_window: tk.Toplevel | None = None
        self.eye_reminder_window: tk.Toplevel | None = None
        self.zone_window: tk.Toplevel | None = None
        self.zone_canvas: tk.Canvas | None = None
        self.zone_after_id: str | None = None
        self.zone_started = 0.0
        self.active_tab = "live"
        self.standing_button: ttk.Button | None = None
        self.sitting_button: ttk.Button | None = None
        self.reminder_toggle_button: ttk.Button | None = None
        self.eye_toggle_button: ttk.Button | None = None
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
        self.eye_status_var = tk.StringVar(value="")
        self.eye_next_var = tk.StringVar(value="")
        self.eye_toggle_text = tk.StringVar(value="")

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
            height=260,
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

        divider = tk.Frame(reminder, bg=THEME["border_soft"], height=1)
        divider.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 12))

        eye = tk.Frame(reminder, bg=THEME["panel_alt"])
        eye.grid(row=5, column=0, columnspan=3, sticky="ew")
        eye.columnconfigure(0, weight=1)
        eye_text = tk.Frame(eye, bg=THEME["panel_alt"])
        eye_text.grid(row=0, column=0, sticky="ew")
        tk.Label(
            eye_text,
            text="护眼提醒",
            bg=THEME["panel_alt"],
            fg=THEME["gold_light"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            eye_text,
            textvariable=self.eye_status_var,
            bg=THEME["panel_alt"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", pady=(3, 0))
        tk.Label(
            eye_text,
            textvariable=self.eye_next_var,
            bg=THEME["panel_alt"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(3, 0))

        eye_actions = tk.Frame(eye, bg=THEME["panel_alt"])
        eye_actions.grid(row=0, column=1, sticky="e", padx=(14, 0))
        self.eye_toggle_button = ttk.Button(
            eye_actions,
            textvariable=self.eye_toggle_text,
            command=self.toggle_eye_reminder,
            style="Neutral.TButton",
        )
        self.eye_toggle_button.pack(side="left", padx=(0, 8))
        ttk.Button(
            eye_actions,
            text="进入 Zone",
            command=self.start_eye_zone,
            style="Neutral.TButton",
        ).pack(side="left")

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
            self.user_active = is_user_active()
            self.process_posture_reminder(elapsed)
            self.process_eye_reminder(elapsed)
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
        self.refresh_eye_ui()

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
        if not self.reminder.get("enabled", True):
            return
        if not self.user_active:
            return

        # Clamp the reminder counter so sleep/resume or a paused UI loop does not
        # accidentally count a long break as active sitting or standing time.
        self.posture_elapsed += min(max(elapsed, 0.0), 5.0)
        if self.posture_elapsed >= REMINDER_INTERVAL_SECONDS:
            self.show_posture_reminder()

    def process_eye_reminder(self, elapsed: float) -> None:
        if not self.eye_reminder.get("enabled", True):
            return
        if self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            return
        if self.zone_window is not None and self.zone_window.winfo_exists():
            return
        if not self.user_active:
            return

        self.eye_elapsed += min(max(elapsed, 0.0), 5.0)
        if self.eye_elapsed >= EYE_REMINDER_INTERVAL_SECONDS:
            self.show_eye_reminder()

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

    def refresh_eye_ui(self) -> None:
        enabled = self.eye_reminder.get("enabled", True)
        if not enabled:
            self.eye_status_var.set("已关闭：本次不再弹护眼提醒")
            self.eye_next_var.set("需要时可以重新开启，或手动进入 Zone。")
            self.eye_toggle_text.set("开启护眼")
        elif self.zone_window is not None and self.zone_window.winfo_exists():
            remaining = max(0, int(round(EYE_ZONE_SECONDS - (time.monotonic() - self.zone_started))))
            self.eye_status_var.set("Zone 进行中：放松眼睛")
            self.eye_next_var.set(f"自动结束：{format_duration(remaining)}")
            self.eye_toggle_text.set("关闭护眼")
        elif self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            self.eye_status_var.set("护眼提醒正在屏幕中央显示")
            self.eye_next_var.set("可以关闭提醒，或进入 1 分钟 Zone。")
            self.eye_toggle_text.set("关闭护眼")
        else:
            remaining = max(0.0, EYE_REMINDER_INTERVAL_SECONDS - self.eye_elapsed)
            self.eye_status_var.set("每 45 分钟提醒眨眼休息")
            self.eye_next_var.set(f"下一次护眼提醒：{format_duration(remaining)}")
            self.eye_toggle_text.set("关闭护眼")

        if self.eye_toggle_button is not None:
            self.eye_toggle_button.configure(style="Neutral.TButton")

    def save_reminder_settings(self) -> None:
        self.reminder["updated_at"] = now_text()
        self.state["reminder"] = self.reminder
        save_state(self.state)

    def save_eye_reminder_settings(self) -> None:
        self.eye_reminder["updated_at"] = now_text()
        self.state["eye_reminder"] = self.eye_reminder
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

    def toggle_eye_reminder(self) -> None:
        self.eye_reminder["enabled"] = not self.eye_reminder.get("enabled", True)
        self.eye_elapsed = 0.0
        if not self.eye_reminder["enabled"]:
            self.close_eye_reminder("off")
        self.save_eye_reminder_settings()
        self.refresh_eye_ui()

    def reset_posture_interval(self) -> None:
        self.posture_elapsed = 0.0
        self.save_reminder_settings()
        self.refresh_reminder_ui()

    def reset_eye_interval(self) -> None:
        self.eye_elapsed = 0.0
        self.save_eye_reminder_settings()
        self.refresh_eye_ui()

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

    def show_eye_reminder(self) -> None:
        if self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            return
        if self.zone_window is not None and self.zone_window.winfo_exists():
            return

        win = tk.Toplevel(self.root)
        self.eye_reminder_window = win
        win.title("护眼提醒")
        win.geometry("640x360")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        win.protocol("WM_DELETE_WINDOW", lambda: self.close_eye_reminder("later"))
        win.configure(bg=THEME["bg"])

        shell = tk.Frame(win, bg=THEME["bg"], padx=18, pady=16)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(
            shell,
            fill=THEME["panel"],
            border=THEME["border"],
            bg=THEME["bg"],
            radius=24,
            padding=(20, 18, 20, 18),
        )
        card.pack(fill="both", expand=True)
        content = card.body

        tk.Frame(content, bg=THEME["gold"], width=5).pack(side="left", fill="y", padx=(0, 16))
        main = tk.Frame(content, bg=THEME["panel"])
        main.pack(side="left", fill="both", expand=True)
        tk.Label(
            main,
            text="护眼提醒",
            bg=THEME["panel"],
            fg=THEME["gold"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            main,
            text="记得多眨眨眼睛，\n休息休息眼睛。",
            bg=THEME["panel"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 22, "bold"),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(8, 10))
        tk.Label(
            main,
            text="已经连续看屏幕约 45 分钟了。可以进入 1 分钟风景 Zone，看远一点，让眼睛缓一缓。",
            bg=THEME["panel"],
            fg="#d9deea",
            font=("Microsoft YaHei UI", 12),
            wraplength=500,
            justify="left",
        ).pack(anchor="w")

        buttons = tk.Frame(main, bg=THEME["panel"])
        buttons.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(
            buttons,
            text="关闭提醒",
            command=lambda: self.close_eye_reminder("off"),
            style="Reminder.TButton",
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="进入 zone",
            command=lambda: self.close_eye_reminder("zone"),
            style="Reminder.TButton",
        ).pack(side="left", padx=8)

        win.update_idletasks()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = max(20, (screen_w - win.winfo_width()) // 2)
        y = max(20, (screen_h - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        win.lift()
        win.focus_force()
        self.refresh_eye_ui()

    def close_eye_reminder(self, action: str) -> None:
        if self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            self.eye_reminder_window.destroy()
        self.eye_reminder_window = None

        if action == "off":
            self.eye_reminder["enabled"] = False
            self.eye_elapsed = 0.0
            self.save_eye_reminder_settings()
            self.refresh_eye_ui()
        elif action == "zone":
            self.start_eye_zone()
        else:
            self.reset_eye_interval()

    def start_eye_zone(self) -> None:
        if self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            self.eye_reminder_window.destroy()
        self.eye_reminder_window = None
        self.eye_elapsed = 0.0
        self.save_eye_reminder_settings()
        self.refresh_eye_ui()
        self.show_eye_zone()

    def show_eye_zone(self) -> None:
        if self.zone_window is not None and self.zone_window.winfo_exists():
            self.zone_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.zone_window = win
        self.zone_started = time.monotonic()
        win.title("护眼 Zone")
        win.configure(bg="#03050a")
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close_eye_zone)
        win.bind("<Escape>", lambda event: self.close_eye_zone())

        canvas = tk.Canvas(win, bg="#03050a", highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self.zone_canvas = canvas

        exit_button = tk.Button(
            win,
            text="退出 Zone  Esc",
            command=self.close_eye_zone,
            bg="#121826",
            fg=THEME["text"],
            activebackground="#253149",
            activeforeground=THEME["gold_light"],
            relief="flat",
            bd=0,
            padx=16,
            pady=8,
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2",
        )
        exit_button.place(relx=1.0, x=-28, y=26, anchor="ne")

        win.lift()
        win.focus_force()
        self.draw_zone_frame()

    def close_eye_zone(self) -> None:
        if self.zone_canvas is not None and self.zone_after_id is not None:
            try:
                self.zone_canvas.after_cancel(self.zone_after_id)
            except tk.TclError:
                pass
        self.zone_after_id = None
        if self.zone_window is not None and self.zone_window.winfo_exists():
            self.zone_window.destroy()
        self.zone_window = None
        self.zone_canvas = None
        self.reset_eye_interval()

    def draw_zone_frame(self) -> None:
        if self.zone_window is None or not self.zone_window.winfo_exists():
            return
        if self.zone_canvas is None:
            return

        elapsed = time.monotonic() - self.zone_started
        if elapsed >= EYE_ZONE_SECONDS:
            self.close_eye_zone()
            return

        canvas = self.zone_canvas
        width = max(800, canvas.winfo_width())
        height = max(560, canvas.winfo_height())
        scene_seconds = EYE_ZONE_SECONDS / len(ZONE_SCENES)
        scene_index = min(len(ZONE_SCENES) - 1, int(elapsed // scene_seconds))
        scene_progress = (elapsed % scene_seconds) / scene_seconds

        canvas.delete("all")
        self.draw_zone_scene(canvas, width, height, scene_index, scene_progress)

        title, subtitle = ZONE_SCENES[scene_index]
        remaining = max(0, int(round(EYE_ZONE_SECONDS - elapsed)))
        canvas.create_text(
            40,
            38,
            text=f"护眼 Zone · {title}",
            anchor="nw",
            fill=THEME["text"],
            font=("Microsoft YaHei UI", 24, "bold"),
        )
        canvas.create_text(
            42,
            78,
            text=subtitle,
            anchor="nw",
            fill=THEME["gold_light"],
            font=("Segoe UI", 12, "bold"),
        )
        canvas.create_text(
            width / 2,
            height - 76,
            text="慢慢眨眼 · 看向远处 · 放松眼睛",
            anchor="center",
            fill=THEME["text"],
            font=("Microsoft YaHei UI", 21, "bold"),
        )
        canvas.create_text(
            width / 2,
            height - 42,
            text=f"{remaining} 秒后自动退出，也可以按 Esc 提前退出",
            anchor="center",
            fill=THEME["gold_light"],
            font=("Microsoft YaHei UI", 12),
        )
        self.zone_after_id = canvas.after(120, self.draw_zone_frame)

    def draw_zone_scene(
        self,
        canvas: tk.Canvas,
        width: int,
        height: int,
        scene_index: int,
        progress: float,
    ) -> None:
        if scene_index == 0:
            self.draw_atoll_scene(canvas, width, height, progress)
        elif scene_index == 1:
            self.draw_ice_coast_scene(canvas, width, height, progress)
        elif scene_index == 2:
            self.draw_alps_scene(canvas, width, height, progress)
        elif scene_index == 3:
            self.draw_desert_scene(canvas, width, height, progress)
        elif scene_index == 4:
            self.draw_fjord_scene(canvas, width, height, progress)
        else:
            self.draw_terrace_scene(canvas, width, height, progress)

    @staticmethod
    def mix_color(start: str, end: str, ratio: float) -> str:
        ratio = max(0.0, min(1.0, ratio))
        left = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
        right = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
        mixed = tuple(round(left[index] + (right[index] - left[index]) * ratio) for index in range(3))
        return "#{:02x}{:02x}{:02x}".format(*mixed)

    def draw_gradient(self, canvas: tk.Canvas, width: int, height: int, top: str, bottom: str) -> None:
        steps = 54
        stripe = max(1, height // steps + 1)
        for index in range(steps):
            ratio = index / max(1, steps - 1)
            y1 = index * stripe
            canvas.create_rectangle(
                0,
                y1,
                width,
                min(height, y1 + stripe),
                fill=self.mix_color(top, bottom, ratio),
                outline="",
            )

    def draw_atoll_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#042636", "#12a6a1")
        pan = (progress - 0.5) * width * 0.05
        for ratio in (0.18, 0.34, 0.52, 0.72, 0.86):
            x = width * ratio + pan * (1.2 - ratio)
            canvas.create_arc(
                x - 150,
                height * 0.22,
                x + 150,
                height * 0.72,
                start=20,
                extent=300,
                outline="#e9d48a",
                width=18,
                style="arc",
            )
        canvas.create_oval(width * 0.20 + pan, height * 0.25, width * 0.52 + pan, height * 0.70, fill="#82e2dd", outline="")
        canvas.create_oval(width * 0.28 + pan, height * 0.36, width * 0.43 + pan, height * 0.58, fill="#0f9a8f", outline="")
        canvas.create_oval(width * 0.63 - pan, height * 0.32, width * 0.82 - pan, height * 0.58, fill="#e9d48a", outline="")
        canvas.create_oval(width * 0.68 - pan, height * 0.38, width * 0.77 - pan, height * 0.51, fill="#2f8b55", outline="")
        for offset in range(0, width, 180):
            y = height * (0.18 + 0.08 * ((offset // 180) % 3))
            canvas.create_line(
                offset + pan * 1.8,
                y,
                offset + 90 + pan * 1.8,
                y + 26,
                offset + 180 + pan * 1.8,
                y,
                fill="#d9fffb",
                width=2,
                smooth=True,
            )

    def draw_ice_coast_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#071722", "#0a4156")
        pan = (progress - 0.5) * width * 0.06
        canvas.create_polygon(0, 0, width * 0.58 + pan, 0, width * 0.43 + pan, height, 0, height, fill="#062a3c", outline="")
        canvas.create_polygon(width * 0.52 + pan, 0, width, 0, width, height, width * 0.42 + pan, height, fill="#151515", outline="")
        for index in range(8):
            x = width * (0.48 + index * 0.02) + pan
            canvas.create_line(
                x,
                0,
                x - width * 0.08,
                height * 0.34,
                x - width * 0.02,
                height * 0.68,
                x - width * 0.12,
                height,
                fill=self.mix_color("#f5fbff", "#75b8c7", index / 8),
                width=max(2, 7 - index),
                smooth=True,
            )
        for index in range(18):
            x = width * 0.60 + (index * 97 + pan * 2) % (width * 0.4)
            y = (index * 73) % height
            canvas.create_oval(x, y, x + 4, y + 4, fill="#cfd8df", outline="")

    def draw_alps_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#13324a", "#87c7d8")
        pan = (progress - 0.5) * width * 0.04
        cloud_y = height * 0.20
        for index in range(7):
            x = (index * width / 6 + pan * 2) % (width + 220) - 110
            canvas.create_oval(x - 70, cloud_y + (index % 3) * 22, x + 90, cloud_y + 70, fill="#e7f3f4", outline="")
        layers = [
            ("#22354a", 0.72, 0.16),
            ("#31506a", 0.82, 0.10),
            ("#1a2938", 0.94, 0.06),
        ]
        for color, base, speed in layers:
            points = [0, height * base]
            for index in range(7):
                x = index * width / 6 + pan * width * speed
                peak = height * (0.20 + 0.12 * (index % 3))
                points.extend([x, peak])
            points.extend([width, height * base, width, height, 0, height])
            canvas.create_polygon(points, fill=color, outline="")
        for index in range(6):
            x = index * width / 5 + pan * 0.8
            peak_y = height * (0.22 + 0.09 * (index % 2))
            canvas.create_polygon(
                x - 42,
                peak_y + 70,
                x,
                peak_y,
                x + 48,
                peak_y + 74,
                fill="#f4f8f2",
                outline="",
            )

    def draw_desert_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#40240e", "#d69b45")
        pan = (progress - 0.5) * width * 0.09
        for index in range(12):
            y = height * (0.16 + index * 0.075)
            color = self.mix_color("#f4c979", "#8d5720", index / 12)
            canvas.create_line(
                -80 + pan,
                y,
                width * 0.25,
                y - 36 + (index % 2) * 22,
                width * 0.55,
                y + 24,
                width + 80 + pan,
                y - 18,
                fill=color,
                width=max(2, 10 - index // 2),
                smooth=True,
            )
        canvas.create_polygon(0, height * 0.72, width * 0.42 + pan, height * 0.44, width, height * 0.76, width, height, 0, height, fill="#b77731", outline="")
        canvas.create_line(0, height * 0.72, width * 0.42 + pan, height * 0.44, width, height * 0.76, fill="#f2c871", width=3, smooth=True)

    def draw_fjord_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#102c3b", "#7bb5b5")
        pan = (progress - 0.5) * width * 0.04
        canvas.create_polygon(0, height * 0.18, width * 0.38 + pan, height * 0.62, 0, height, fill="#173827", outline="")
        canvas.create_polygon(width, height * 0.12, width * 0.62 - pan, height * 0.58, width, height, fill="#23432f", outline="")
        canvas.create_polygon(width * 0.36 + pan, height * 0.56, width * 0.64 - pan, height * 0.55, width * 0.84, height, width * 0.16, height, fill="#174d63", outline="")
        for index in range(9):
            x = width * (0.36 + index * 0.035) + pan * (index % 2 - 0.5)
            canvas.create_line(x, height * 0.62, width / 2, height, fill="#9bd7d9", width=2, smooth=True)
        canvas.create_polygon(width * 0.42, height * 0.74, width * 0.50, height * 0.68, width * 0.58, height * 0.75, width * 0.52, height * 0.80, fill="#40694b", outline="")

    def draw_terrace_scene(self, canvas: tk.Canvas, width: int, height: int, progress: float) -> None:
        self.draw_gradient(canvas, width, height, "#18331f", "#6e8c48")
        pan = (progress - 0.5) * width * 0.05
        palette = ("#2b5d38", "#6f8f41", "#b7aa5a", "#4e7f75", "#8dc2aa")
        for index in range(18):
            y = height * (0.10 + index * 0.052)
            color = palette[index % len(palette)]
            canvas.create_line(
                -120 + pan * (index % 3),
                y,
                width * 0.20,
                y - 28 + (index % 4) * 12,
                width * 0.48,
                y + 18,
                width * 0.74,
                y - 24,
                width + 120,
                y + 8,
                fill=color,
                width=18,
                smooth=True,
            )
            canvas.create_line(
                -120 + pan * (index % 3),
                y - 9,
                width * 0.20,
                y - 37 + (index % 4) * 12,
                width * 0.48,
                y + 9,
                width * 0.74,
                y - 33,
                width + 120,
                y - 1,
                fill="#d5d88f",
                width=2,
                smooth=True,
            )
        canvas.create_oval(width * 0.62 - pan, height * 0.20, width * 0.78 - pan, height * 0.33, fill="#d8e8c0", outline="")

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
        if self.eye_reminder_window is not None and self.eye_reminder_window.winfo_exists():
            self.eye_reminder_window.destroy()
        if self.zone_window is not None and self.zone_window.winfo_exists():
            self.close_eye_zone()
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
    eye_reminder = get_eye_reminder_settings(state)
    print(
        json.dumps(
            {
                "ok": True,
                "foreground": info,
                "idle_seconds": round(get_idle_seconds(), 2),
                "user_active": is_user_active(),
                "reminder": reminder,
                "eye_reminder": eye_reminder,
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
