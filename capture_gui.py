"""
capture_gui.py

Tkinter front-end for hide_from_capture.py.

  - Lists visible windows with their process name and current capture state.
  - Pick a window and Hide / Show / Toggle its screen-capture exclusion.
  - Global hotkey Ctrl+Alt+H toggles the currently selected window, even when
    this GUI is not focused.

Stdlib only (tkinter + ctypes). Run:  python capture_gui.py

Notes / caveats:
  - Hiding a window owned by an *elevated* (admin) process from a normal
    process may fail due to Windows UIPI. Run this as admin to target those.
  - This only affects software capture on THIS machine (screenshots, OBS,
    Zoom/Teams/Meet share). It does not hide the window from a camera.
"""

import ctypes
import json
import os
import queue
import sys
import threading
import winreg
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, messagebox

import hide_from_capture as core
import win_icons

# Bump on each meaningful build so you can confirm which build is running
# (shown in the title bar). This build removes the black WDA_MONITOR fallback.
APP_VERSION = "2026.07.06.2"
APP_NAME = "GenerousUnseen"

# --- theme palettes ------------------------------------------------------
THEMES = {
    "light": {
        "bg": "#f4f4f5", "surface": "#ffffff", "stripe": "#f7f7fa",
        "fg": "#1a1a1a", "muted": "#6b6b72", "border": "#e2e2e8",
        "heading": "#e8e8ec", "btn": "#e9e9ee", "btn_active": "#dcdce3",
        "accent": "#2f6feb", "sel_fg": "#ffffff",
        "sel": "#eaf1fe", "hover": "#f2f2f6", "off": "#c9c9d1", "knob": "#ffffff",
    },
    "dark": {
        "bg": "#161618", "surface": "#1e1e21", "stripe": "#242427",
        "fg": "#e8e8ea", "muted": "#9a9aa2", "border": "#303035",
        "heading": "#212124", "btn": "#2b2b30", "btn_active": "#3a3a41",
        "accent": "#3b82f6", "sel_fg": "#ffffff",
        "sel": "#28323f", "hover": "#26262a", "off": "#4a4a52", "knob": "#e8e8ea",
    },
}

# --- global hotkey plumbing (Win32 RegisterHotKey) -----------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_H = 0x48
VK_A = 0x41
VK_L = 0x4C
VK_G = 0x47
VK_X = 0x58

# Hotkey ids and their (modifiers, virtual-key). Ctrl+Alt+<key>.
HK_TOGGLE_SELECTED = 1   # toggle the selected window's capture exclusion
HK_TOGGLE_SELF = 2       # toggle this app's own window's capture exclusion
HK_TOGGLE_ALL = 3        # toggle capture exclusion for all activated apps
HK_HIDE_VIEW_ALL = 4     # boss key: hide/restore all activated apps from the screen
HK_SHOW_WINDOW = 5       # show/hide the Exclusion window itself
# RegisterHotKey can't distinguish left/right modifiers, so the show/hide combo
# (HK_SHOW_WINDOW) is NOT here — it's handled by a low-level keyboard hook that
# checks the specific side keys (Right Ctrl + Right Alt + Left Shift + X).
HOTKEYS = [
    (HK_TOGGLE_SELECTED, MOD_CONTROL | MOD_ALT, VK_H),
    (HK_TOGGLE_SELF, MOD_CONTROL | MOD_ALT, VK_A),
    (HK_TOGGLE_ALL, MOD_CONTROL | MOD_ALT, VK_L),
    (HK_HIDE_VIEW_ALL, MOD_CONTROL | MOD_ALT, VK_G),
]
HOTKEY_LABELS = {
    HK_TOGGLE_SELECTED: "Ctrl+Alt+H",
    HK_TOGGLE_SELF: "Ctrl+Alt+A",
    HK_TOGGLE_ALL: "Ctrl+Alt+L",
    HK_HIDE_VIEW_ALL: "Ctrl+Alt+G",
    HK_SHOW_WINDOW: "RCtrl+RAlt+LShift+X",
}

# Class name of the hidden message window; also used by the single-instance
# check to find an already-running instance and ask it to show itself.
HIDDEN_WND_CLASS = "GenerousUnseenMsgWnd"

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL


def _icon_path():
    """Path to the bundled icon.ico (extracted to _MEIPASS when frozen), or None."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "icon.ico")
    return p if os.path.exists(p) else None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Re-launch this program elevated via a UAC prompt.
    Returns True if the elevated process was started (this one should exit),
    False if elevation failed or the user declined."""
    if getattr(sys, "frozen", False):
        # Running as a bundled .exe (e.g. PyInstaller): relaunch the exe itself.
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        # Running as a .py script: relaunch the interpreter with this script.
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in [script] + sys.argv[1:])
    # SW_SHOWNORMAL = 1; ShellExecuteW returns >32 on success.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return int(rc) > 32


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL


# --- hidden message window plumbing (single-instance + show/hide) ---------
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_APP = 0x8000
WM_APP_SHOW = WM_APP + 2   # posted by a second launch to show the running instance
WS_POPUP = 0x80000000
MSGFLT_ALLOW = 1           # allow a message through UIPI (cross-integrity PostMessage)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.ChangeWindowMessageFilterEx.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL


# --- low-level keyboard hook (side-specific chord to show/hide the window) ----
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_RCONTROL = 0xA3
VK_RMENU = 0xA5      # right Alt
VK_LSHIFT = 0xA0
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class BackgroundThread(threading.Thread):
    """Owns a hidden message window, registers the Ctrl+Alt+* global hotkeys, and
    installs a low-level keyboard hook for the side-specific show/hide chord
    (Right Ctrl + Right Alt + Left Shift + X). Pumps their shared message loop and
    emits event codes (hotkey ids, or the string 'show') via `on_event`, which runs
    on THIS thread — the App marshals them onto the Tk thread through a queue. The
    hidden window also lets a second launch ask this instance to show itself
    (WM_APP_SHOW)."""

    CLASS_NAME = HIDDEN_WND_CLASS

    def __init__(self, hotkeys, on_event):
        super().__init__(daemon=True)
        self.hotkeys = hotkeys                # list of (id, modifiers, vk)
        self.on_event = on_event
        self.thread_id = None
        self.hwnd = None
        self.registered = {}                  # id -> bool
        self.hook_ok = False                  # low-level keyboard hook installed?
        self._ready = threading.Event()
        self._wndproc_cb = None
        self._wc = None
        self._hook = None
        self._hookproc_cb = None
        self._x_down = False                  # debounce X auto-repeat

    def _create_window(self):
        self._wndproc_cb = WNDPROC(self._wndproc)  # keep a ref alive!
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_cb
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        self._wc = wc  # keep alive
        user32.RegisterClassW(ctypes.byref(wc))  # ignore "already registered"
        self.hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, APP_NAME, WS_POPUP,
            0, 0, 0, 0, None, None, hinst, None)
        # Allow a lower-integrity (non-elevated) second launch to PostMessage us
        # through UIPI so re-launching the exe can show the running instance.
        try:
            user32.ChangeWindowMessageFilterEx(self.hwnd, WM_APP_SHOW, MSGFLT_ALLOW, None)
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_SHOW:
            self.on_event("show")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _install_hook(self):
        self._hookproc_cb = HOOKPROC(self._ll_proc)  # keep a ref alive!
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hookproc_cb, kernel32.GetModuleHandleW(None), 0)
        self.hook_ok = bool(self._hook)

    def _ll_proc(self, ncode, wparam, lparam):
        # Fire HK_SHOW_WINDOW only on Right Ctrl + Right Alt + Left Shift + X.
        if ncode == HC_ACTION:
            vk = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN) and vk == VK_X:
                if not self._x_down:
                    self._x_down = True
                    down = lambda k: user32.GetAsyncKeyState(k) & 0x8000
                    if down(VK_RCONTROL) and down(VK_RMENU) and down(VK_LSHIFT):
                        self.on_event(HK_SHOW_WINDOW)
                        return 1  # swallow the X so it isn't typed anywhere
            elif wparam in (WM_KEYUP, WM_SYSKEYUP) and vk == VK_X:
                self._x_down = False
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        try:
            self._create_window()
            self._install_hook()
        except Exception:
            pass
        for hid, mods, vk in self.hotkeys:
            self.registered[hid] = bool(
                user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk))
        self._ready.set()
        msg = MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):  # WM_QUIT or error
                break
            if msg.message == WM_HOTKEY:
                self.on_event(int(msg.wParam))
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
        for hid, _, _ in self.hotkeys:
            user32.UnregisterHotKey(None, hid)

    def wait_ready(self, timeout=2.0):
        self._ready.wait(timeout)
        return dict(self.registered)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


class ToggleSwitch(tk.Canvas):
    """A small iOS-style on/off pill switch drawn on a Canvas."""

    def __init__(self, master, on=False, command=None, on_color="#3ba55d",
                 off_color="#6b6b72", knob="#ffffff", bg="#ffffff", w=44, h=24):
        super().__init__(master, width=w, height=h, highlightthickness=0, bd=0,
                         bg=bg, cursor="hand2")
        self.on = bool(on)
        self.command = command
        self.on_color, self.off_color, self.knob = on_color, off_color, knob
        self.w, self.h = w, h
        self.bind("<Button-1>", self._click)
        self._draw()

    def _pill(self, x0, y0, x1, y1, fill):
        r = (y1 - y0) / 2
        self.create_oval(x0, y0, x0 + 2 * r, y1, fill=fill, outline=fill)
        self.create_oval(x1 - 2 * r, y0, x1, y1, fill=fill, outline=fill)
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)

    def _draw(self):
        self.delete("all")
        pad = 2
        r = (self.h - 2 * pad) / 2
        self._pill(pad, pad, self.w - pad, self.h - pad,
                   self.on_color if self.on else self.off_color)
        cx = (self.w - pad - r) if self.on else (pad + r)
        k = r - 2
        self.create_oval(cx - k, self.h / 2 - k, cx + k, self.h / 2 + k,
                         fill=self.knob, outline="")

    def set(self, on):
        self.on = bool(on)
        self._draw()

    def set_bg(self, bg):
        self.configure(bg=bg)
        self._draw()

    def recolor(self, on_color, off_color, knob, bg):
        self.on_color, self.off_color, self.knob = on_color, off_color, knob
        self.configure(bg=bg)
        self._draw()

    def _click(self, _e):
        if self.command:
            self.command()
        return "break"


# --- GUI -----------------------------------------------------------------
class App:
    ICON_SIZE = 16    # app icon size in the list
    ROW_H = 46        # row height in pixels
    REFRESH_MS = 2000  # auto-refresh interval for the live window list
    REASSERT_MS = 2000  # re-assert cadence; runs on its own timer, even in background

    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME}  v{APP_VERSION}")
        root.geometry("780x520")
        root.minsize(620, 400)
        # Tool-window style => no taskbar button and no alt-tab entry.
        try:
            root.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        # Window title-bar icon (the exe icon is embedded at build time).
        try:
            ip = _icon_path()
            if ip:
                root.iconbitmap(ip)
        except Exception:
            pass

        self.rows = []          # parallel list of window dicts for the tree
        self.event_q = queue.Queue()
        self._icon_cache = {}   # (path, checked) -> PhotoImage (or None); keeps refs alive
        self._remembered_all = []  # hwnds toggled by the capture "toggle all" switch
        self._hidden_from_view = []  # hwnds hidden off-screen by the boss key
        self._sel_hwnd = None      # remembered selection (survives a row briefly leaving the list)
        self._last_sig = None      # last rendered window-list signature (change detection)
        self._panic = False        # Ctrl+Alt+L panic active -> re-assert suspended
        self._reassert_busy = False       # a re-assert worker thread is in flight
        self._reassert_correcting = False  # last tick was actively re-hiding windows
        self._reassert_warned = False      # currently warning about unprotected windows
        self._close_hint_shown = False

        self._config = self._load_config()
        self.theme_mode = self._config.get("theme", "system")  # system | light | dark
        # HWNDs the user has Activated this session — the set the re-assert loop
        # keeps excluded. Per-window (not per-process), so activating one window
        # never pulls in sibling windows of the same app.
        self._activated = set()
        self.style = ttk.Style()

        self._build_ui()
        self.apply_theme()
        self.refresh()

        self.bg = BackgroundThread(HOTKEYS, lambda ev: self.event_q.put(ev))
        self.bg.start()
        self.root.after(300, self._check_hotkey_registered)
        self.root.after(100, self._poll_events)
        self.root.after(self.REFRESH_MS, self._auto_refresh)  # live list updates
        # Re-assert exclusion on its own timer — NOT gated on the window being
        # visible, so protection keeps working while running in the background.
        self.root.after(400, self._reassert_tick)
        # Apply the default "hide this app" state once the window frame exists
        # (silent so it doesn't overwrite the admin/hotkey status messages).
        self.root.after(200, lambda: self._apply_self_hide(announce=False))
        # Re-assert the dark title bar once the window is actually mapped.
        self.root.after(120, lambda: self._set_titlebar_dark(self._resolve_mode() == "dark"))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- layout ----
    def _build_ui(self):
        p = THEMES[self._resolve_mode()]

        # ---- top bar: app name · search · theme · refresh ----
        top = ttk.Frame(self.root, style="Bar.TFrame", padding=(14, 12, 14, 10))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, style="Title.TLabel").pack(side="left")

        ttk.Button(top, text="⟳", width=3, style="Ghost.TButton",
                   command=self.refresh).pack(side="right")
        self.theme_var = tk.StringVar(value=self.theme_mode.capitalize())
        theme_cb = ttk.Combobox(top, textvariable=self.theme_var, width=8, state="readonly",
                                values=("System", "Light", "Dark"))
        theme_cb.pack(side="right", padx=(0, 8))
        theme_cb.bind("<<ComboboxSelected>>", self._on_theme_change)

        search = ttk.Frame(top, style="Search.TFrame")
        search.pack(side="right", padx=(0, 12))
        ttk.Label(search, text="🔍", style="Search.TLabel").pack(side="left", padx=(8, 2))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render())
        e = ttk.Entry(search, textvariable=self.filter_var, width=22, style="Search.TEntry")
        e.pack(side="left", padx=(0, 6), pady=4)

        # ---- fixed "this app itself" row (always visible) ----
        self.hide_self_var = tk.BooleanVar(value=True)
        selfrow = tk.Frame(self.root, bg=p["surface"])
        selfrow.pack(fill="x", padx=14)
        self.self_row = selfrow
        self._selficon = tk.Label(selfrow, text="👁", bg=p["surface"], fg=p["fg"],
                                  font=("Segoe UI Emoji", 13))
        self._selficon.pack(side="left", padx=(10, 10), pady=8)
        self._selftext = tk.Frame(selfrow, bg=p["surface"])
        self._selftext.pack(side="left", fill="x", expand=True)
        self.self_title = tk.Label(self._selftext, text=f"{APP_NAME} (this app)", bg=p["surface"],
                                   fg=p["fg"], font=("Segoe UI Semibold", 10), anchor="w")
        self.self_title.pack(fill="x")
        self.self_sub = tk.Label(self._selftext, text="Hide this window from screen capture",
                                 bg=p["surface"], fg=p["muted"], font=("Segoe UI", 8), anchor="w")
        self.self_sub.pack(fill="x")
        self.self_switch = ToggleSwitch(selfrow, on=True, command=self.toggle_self,
                                        on_color=p["accent"], off_color=p["off"],
                                        knob=p["knob"], bg=p["surface"])
        self.self_switch.pack(side="right", padx=(10, 12))

        # ---- scrollable list of windows ----
        self._mid = tk.Frame(self.root, bg=p["surface"])
        self._mid.pack(fill="both", expand=True, padx=14, pady=(2, 0))
        self.canvas = tk.Canvas(self._mid, highlightthickness=0, bg=p["surface"])
        vsb = ttk.Scrollbar(self._mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.canvas, bg=p["surface"])
        self._list_win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self._list_win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # ---- bottom bar: summary · Hide all · more menu ----
        bar = ttk.Frame(self.root, style="Bar.TFrame", padding=(14, 8))
        bar.pack(fill="x")
        self.summary = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.summary, style="Summary.TLabel").pack(side="left")

        ttk.Button(bar, text="⋯", width=3, style="Ghost.TButton",
                   command=self._show_more_menu).pack(side="right")
        ttk.Button(bar, text="Run in background", style="Ghost.TButton",
                   command=self.run_in_background).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Hide all from view", style="Accent.TButton",
                   command=self.hide_view_all).pack(side="right", padx=(0, 6))

        # ---- status bar ----
        self.status = tk.StringVar(
            value="Ctrl+Alt — H: selected · A: this app · L: capture all · "
                  "G: hide all · RCtrl+RAlt+LShift+X: show/hide")
        ttk.Label(self.root, textvariable=self.status, anchor="w",
                  style="Status.TLabel").pack(fill="x", side="bottom")

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _show_more_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Toggle capture on all", command=self.toggle_all)
        m.add_command(label="Run in background", command=self.run_in_background)
        m.add_separator()
        m.add_command(label="Quit", command=self.quit_app)
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    # ---- theme ----
    def _on_theme_change(self, _event=None):
        self.theme_mode = self.theme_var.get().lower()
        self.apply_theme()
        self.refresh(force=True)  # rebuild rows with the new palette
        self._save_config()

    def _resolve_mode(self):
        if self.theme_mode == "system":
            return "dark" if self._system_is_dark() else "light"
        return self.theme_mode if self.theme_mode in THEMES else "light"

    @staticmethod
    def _system_is_dark():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return val == 0
        except Exception:
            return False

    def apply_theme(self):
        p = THEMES[self._resolve_mode()]
        self._pal = p
        st = self.style
        st.theme_use("clam")
        font = ("Segoe UI", 10)

        self.root.configure(bg=p["bg"])
        st.configure(".", background=p["bg"], foreground=p["fg"],
                     fieldbackground=p["surface"], bordercolor=p["border"],
                     lightcolor=p["bg"], darkcolor=p["bg"], font=font)
        st.configure("TFrame", background=p["bg"])
        st.configure("Bar.TFrame", background=p["bg"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("Title.TLabel", background=p["bg"], foreground=p["fg"],
                     font=("Segoe UI Semibold", 14))
        st.configure("Summary.TLabel", background=p["bg"], foreground=p["muted"])
        st.configure("Status.TLabel", background=p["heading"], foreground=p["muted"],
                     padding=(12, 5))
        # search box
        st.configure("Search.TFrame", background=p["surface"])
        st.configure("Search.TLabel", background=p["surface"], foreground=p["muted"])
        st.configure("Search.TEntry", fieldbackground=p["surface"], foreground=p["fg"],
                     bordercolor=p["surface"], borderwidth=0, insertcolor=p["fg"])
        # buttons
        st.configure("Ghost.TButton", background=p["bg"], foreground=p["fg"],
                     bordercolor=p["bg"], focuscolor=p["accent"], relief="flat",
                     padding=(10, 5))
        st.map("Ghost.TButton",
               background=[("pressed", p["btn_active"]), ("active", p["hover"])],
               foreground=[("disabled", p["muted"])])
        st.configure("Accent.TButton", background=p["accent"], foreground="#ffffff",
                     bordercolor=p["accent"], focuscolor=p["accent"], relief="flat",
                     padding=(12, 5))
        st.map("Accent.TButton", background=[("pressed", p["accent"]), ("active", p["accent"])])
        st.configure("TCombobox", fieldbackground=p["surface"], background=p["btn"],
                     foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"])
        st.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
               foreground=[("readonly", p["fg"])])
        st.configure("Vertical.TScrollbar", background=p["btn"], troughcolor=p["surface"],
                     bordercolor=p["surface"], arrowcolor=p["muted"])
        st.map("Vertical.TScrollbar", background=[("active", p["btn_active"])])

        # themed combobox drop-down list (applies next time it opens)
        self.root.option_add("*TCombobox*Listbox.background", p["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["sel_fg"])

        # recolor the custom (non-ttk) widgets
        for wdg in (self._mid, self.canvas, self.list_frame, self.self_row, self._selftext):
            wdg.configure(bg=p["surface"])
        self._selficon.configure(bg=p["surface"], fg=p["fg"])
        self.self_title.configure(bg=p["surface"], fg=p["fg"])
        self.self_sub.configure(bg=p["surface"], fg=p["muted"])
        self.self_switch.recolor(p["accent"], p["off"], p["knob"], p["surface"])

        self._set_titlebar_dark(self._resolve_mode() == "dark")

    def _set_titlebar_dark(self, dark):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: new id, then old
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            # nudge a non-client redraw so the title bar updates immediately
            swp = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, swp)
        except Exception:
            pass

    # ---- config ----
    @staticmethod
    def _config_path():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME, "config.json")

    def _load_config(self):
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            path = self._config_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"theme": self.theme_mode}, f)
        except Exception:
            pass

    # ---- data ----
    def refresh(self, force=True):
        """Rebuild the window list. Re-renders only when the set of windows or
        their capture state actually changed (unless `force`), so the periodic
        auto-refresh doesn't flicker or disrupt interaction."""
        # skip our own window so we don't hide the controller by accident
        try:
            self_hwnd = int(self.root.wm_frame(), 16) if self.root.wm_frame() else 0
        except Exception:
            self_hwnd = 0
        my_title = self.root.title()
        rows = [w for w in core.list_windows()
                if w["title"] != my_title and w["hwnd"] != self_hwnd]
        for w in rows:
            aff = core.get_affinity(w["hwnd"])
            w["checked"] = aff in (core.WDA_EXCLUDEFROMCAPTURE, core.WDA_MONITOR)

        # Drop a remembered selection once its window is truly gone (closed), so a
        # stale HWND can't drive an action on the wrong window (Windows reuses HWND
        # values). Keep it across a row that only briefly left the list — filtered
        # out by search, or hidden from view by the boss key (still in
        # _hidden_from_view, so not in the visible list).
        if self._sel_hwnd is not None:
            live = {w["hwnd"] for w in rows}
            if self._sel_hwnd not in live and self._sel_hwnd not in self._hidden_from_view:
                self._sel_hwnd = None

        # NOTE: re-applying exclusion to remembered processes lives in
        # _reassert_tick (its own timer + worker thread), not here — so it keeps
        # running while the window is hidden and never blocks the UI thread.
        sig = tuple((w["hwnd"], w["title"], w["proc"], w["checked"]) for w in rows)
        self.rows = rows
        if force or sig != self._last_sig:
            self._last_sig = sig
            self._render()

    def _auto_refresh(self):
        """Periodic live update (skipped while hidden in the tray)."""
        try:
            if self.root.state() != "withdrawn":
                self.refresh(force=False)
        except Exception:
            pass
        self.root.after(self.REFRESH_MS, self._auto_refresh)

    def _reassert_tick(self):
        """Re-apply capture exclusion to each window the user Activated that has
        lost it — the anti-tamper defense against something (e.g. malware) flipping
        a target back to WDA_NONE. Scoped to the exact HWNDs in self._activated, so
        it never pulls in sibling windows of the same process. Runs on its OWN
        timer, NOT gated on the window being visible, so it keeps working while
        GenerousUnseen is in the background; the blocking injection is offloaded to
        a worker thread so the UI never stalls. Keeps retrying every tick (no
        give-up cap) and is suspended only during a Ctrl+Alt+L panic."""
        try:
            if self._activated and not self._panic and not self._reassert_busy:
                own = self._own_hwnd()
                visible = {w["hwnd"] for w in core.list_windows()}
                # Drop Activated windows that have truly closed, but keep ones only
                # hidden from view by the boss key (restored later by _reactivate).
                self._activated &= visible | set(self._hidden_from_view)
                targets = [
                    h for h in self._activated
                    if h != own and h in visible
                    and core.get_affinity(h) not in (
                        core.WDA_EXCLUDEFROMCAPTURE, core.WDA_MONITOR)
                ]
                if targets:
                    self._reassert_busy = True
                    threading.Thread(target=self._reassert_worker,
                                     args=(targets,), daemon=True).start()
        except Exception:
            pass
        self.root.after(self.REASSERT_MS, self._reassert_tick)

    def _reassert_worker(self, targets):
        """Worker thread: re-hide each target, then marshal the result back onto
        the Tk thread through the event queue. Does NO Tk calls itself."""
        corrected = failed = 0
        for hwnd in targets:
            try:
                core.hide(hwnd)
                corrected += 1
            except OSError:
                failed += 1
        self.event_q.put(lambda: self._reassert_done(corrected, failed))

    def _reassert_done(self, corrected, failed):
        """Tk thread: report re-assertion results, announcing only on transitions
        so a flapping window can't clobber the status bar every tick. A failure to
        re-protect is surfaced loudly (a window may be capturable)."""
        self._reassert_busy = False
        if failed:
            if not self._reassert_warned:
                self.status.set(f"⚠ Couldn't re-protect {failed} window(s) — they may "
                                "be capturable until they hold.")
                self._reassert_warned = True
        else:
            self._reassert_warned = False
            if corrected and not self._reassert_correcting:
                self.status.set(f"Re-protected {corrected} window(s) that lost capture "
                                "exclusion.")
        self._reassert_correcting = bool(corrected)

    @staticmethod
    def _clip(text, n):
        text = text or ""
        return text if len(text) <= n else text[:n - 1] + "…"

    def _render(self):
        p = getattr(self, "_pal", THEMES[self._resolve_mode()])
        yview = self.canvas.yview()[0]  # preserve scroll position across rebuild
        needle = self.filter_var.get().lower().strip()
        for child in self.list_frame.winfo_children():
            child.destroy()
        self._row_ui = {}    # hwnd -> {"frame", "bg": [widgets], "sw": ToggleSwitch}
        total = len(self.rows)
        hidden_count = sum(1 for w in self.rows if w.get("checked"))
        shown = 0
        for w in self.rows:
            if needle and needle not in w["title"].lower() \
                    and needle not in (w.get("proc") or "").lower():
                continue
            self._build_row(w, p)
            shown += 1
        if shown == 0:
            tk.Label(self.list_frame, text="No windows" + (" match your search" if needle else ""),
                     bg=p["surface"], fg=p["muted"], font=("Segoe UI", 9),
                     pady=20).pack(fill="x")
        self._highlight_selection()
        self.summary.set(
            f"{hidden_count} hidden · {total} window{'' if total == 1 else 's'}"
            + (f" · {shown} shown" if needle else ""))
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(yview)

    def _build_row(self, w, p):
        hwnd = w["hwnd"]
        checked = bool(w.get("checked"))
        row = tk.Frame(self.list_frame, bg=p["surface"], height=self.ROW_H)
        row.pack(fill="x")
        row.pack_propagate(False)

        # Defer the toggle: it rebuilds the list (destroying this very switch),
        # so let the click handler return first to avoid a destroyed-widget error.
        sw = ToggleSwitch(row, on=checked, on_color=p["accent"], off_color=p["off"],
                          knob=p["knob"], bg=p["surface"],
                          command=lambda h=hwnd: self.root.after(1, lambda: self._apply("toggle", hwnd=h)))
        sw.pack(side="right", padx=(10, 12))

        img = self._get_icon(w.get("path"))
        icon = tk.Label(row, image=img, bg=p["surface"]) if img else \
            tk.Label(row, text="", width=2, bg=p["surface"])
        if img:
            icon.image = img
        icon.pack(side="left", padx=(12, 10))

        txt = tk.Frame(row, bg=p["surface"])
        txt.pack(side="left", fill="both", expand=True)
        title = tk.Label(txt, text=self._clip(w["title"], 68), bg=p["surface"], fg=p["fg"],
                         font=("Segoe UI", 10), anchor="w")
        title.pack(fill="x", pady=(6, 0))
        sub = tk.Label(txt, text=self._clip(w.get("proc") or "", 54), bg=p["surface"],
                       fg=p["muted"], font=("Segoe UI", 8), anchor="w")
        sub.pack(fill="x")

        bgwidgets = [row, txt, title, sub, icon]
        for wdg in bgwidgets:
            wdg.bind("<Button-1>", lambda _e, h=hwnd: self._select_row(h))
        self._row_ui[hwnd] = {"frame": row, "bg": bgwidgets, "sw": sw}

    def _highlight_selection(self):
        p = self._pal
        for hwnd, ui in self._row_ui.items():
            col = p["sel"] if hwnd == self._sel_hwnd else p["surface"]
            for wdg in ui["bg"]:
                try:
                    wdg.configure(bg=col)
                except tk.TclError:
                    pass
            ui["sw"].set_bg(col)

    def _select_row(self, hwnd):
        self._sel_hwnd = hwnd
        self._highlight_selection()

    def _get_icon(self, path):
        """PhotoImage of the app's icon, cached by path (caches None too)."""
        if not path:
            return None
        if path in self._icon_cache:
            return self._icon_cache[path]
        img = None
        b64 = win_icons.icon_png_base64(path, small=True)
        if b64:
            try:
                img = tk.PhotoImage(data=b64)
            except Exception:
                img = None
        self._icon_cache[path] = img
        return img

    def _title_for(self, hwnd):
        return next((w["title"] for w in self.rows if w["hwnd"] == hwnd), str(hwnd))

    def _selected_hwnd(self):
        return self._sel_hwnd

    def _own_hwnd(self):
        """Top-level (decorated) HWND of this GUI window, or None if not ready."""
        try:
            frame = self.root.wm_frame()
            return int(frame, 16) if frame else None
        except Exception:
            return None

    def _apply_self_hide(self, _tries=0, announce=True):
        """Exclude/restore this app's OWN window per the checkbox. Same-process,
        so it uses the direct API (no injection). Retries until the frame exists.
        `announce=False` (used for the silent auto-apply on startup) leaves the
        status bar alone on success so it doesn't overwrite other messages."""
        hwnd = self._own_hwnd()
        if not hwnd:
            if _tries < 10:
                self.root.after(100, lambda: self._apply_self_hide(_tries + 1, announce))
            return
        try:
            if self.hide_self_var.get():
                # Reset-then-exclude with retries; never falls back to black, so
                # re-showing the app after hiding is never a black rectangle.
                core.activate_own(hwnd)
                aff = core.get_affinity(hwnd)
                if aff == core.WDA_EXCLUDEFROMCAPTURE:
                    if announce:
                        self.status.set("This app itself is now Activated — fully hidden "
                                        "from capture (0x11).")
                else:
                    # Should never happen on Win10 2004+. 0x1 = black WDA_MONITOR.
                    # Surfaced loudly regardless of `announce` to catch old builds.
                    self.status.set(f"WARNING — this app's capture state is 0x{aff:x} "
                                    f"({core.affinity_name(aff)}), expected 0x11. "
                                    f"Make sure the title shows v{APP_VERSION}.")
            else:
                core.deactivate_own(hwnd)
                if announce:
                    self.status.set("This app itself is now Inactive (visible in capture).")
        except OSError as e:
            self.status.set(f"Couldn't change this app's capture state: {e}")

    # ---- actions ----
    def _apply(self, action, hwnd=None):
        if hwnd is None:
            hwnd = self._selected_hwnd()
        if not hwnd:
            self.status.set("Select a window first.")
            return
        title = self._title_for(hwnd)
        activated = None  # resulting state (True/False); None if unchanged/failed
        try:
            if action == "hide":
                result = core.hide(hwnd)
                activated = True
            elif action == "show":
                core.show(hwnd)
                result = "Inactive"
                activated = False
            else:  # toggle
                aff = core.get_affinity(hwnd)
                if aff in (core.WDA_EXCLUDEFROMCAPTURE, core.WDA_MONITOR):
                    core.show(hwnd)
                    result = "Inactive"
                    activated = False
                else:
                    result = core.hide(hwnd)
                    activated = True
            self.status.set(f'"{title}"  ->  {result}')
        except OSError as e:
            self.status.set(f"Failed on \"{title}\": {e} (try running as admin)")
        if activated is True:
            self._activated.add(hwnd)
        elif activated is False:
            self._activated.discard(hwnd)
        self.refresh(force=True)

    def hide_selected(self):
        self._apply("hide")

    def show_selected(self):
        self._apply("show")

    def toggle_selected(self):
        self._apply("toggle")

    def toggle_self(self):
        """Flip this app's own capture state (keeps the self-row switch in sync)."""
        self.hide_self_var.set(not self.hide_self_var.get())
        if hasattr(self, "self_switch"):
            self.self_switch.set(self.hide_self_var.get())
        self._apply_self_hide()

    def toggle_all(self):
        """Panic / restore. First press: hide every Activated window from capture
        and suspend re-assertion (panic). Second press: restore the remembered set
        and resume re-assertion. The panic state is tracked explicitly so it can't
        get stuck by windows being (de)activated between presses."""
        if not self._panic:
            own = self._own_hwnd()
            active = [w["hwnd"] for w in core.list_windows()
                      if w["hwnd"] != own
                      and core.get_affinity(w["hwnd"]) in (core.WDA_EXCLUDEFROMCAPTURE,
                                                           core.WDA_MONITOR)]
            if not active:
                self.status.set("No Activated apps to toggle.")
                return
            self._remembered_all = list(active)
            self._panic = True  # suspend re-assert so it doesn't re-hide them
            n = self._bulk(active, "show")
            self.status.set(f"Set {n} app(s) Inactive — press Ctrl+Alt+L again to restore.")
        else:
            self._panic = False  # resume enforcing exclusion
            n = self._bulk(self._remembered_all, "hide")
            self.status.set(f"Re-Activated {n} app(s).")
        self.refresh(force=True)

    def _bulk(self, hwnds, action):
        """Apply hide/show to many hwnds, skipping ones that fail. Returns count."""
        n = 0
        for hwnd in hwnds:
            try:
                core.hide(hwnd) if action == "hide" else core.show(hwnd)
                n += 1
            except OSError:
                pass
        return n

    def hide_view_all(self):
        """Boss key: if activated apps are currently on screen, HIDE them all from
        the desktop (SW_HIDE) and remember them; otherwise bring the hidden ones
        back (SW_SHOW). Hidden windows keep running in the background."""
        if self._hidden_from_view:
            restored = self._hidden_from_view
            self._hidden_from_view = []
            for hwnd in restored:
                core.set_window_hidden(hwnd, False)  # SW_SHOW
            self.status.set(f"Restoring {len(restored)} window(s) to view…")
            # Showing a window can drop its display affinity for some apps
            # (Chrome/Electron and other GPU-composited windows). Give them a
            # moment to finish showing/compositing, THEN re-apply exclusion — so
            # it lands as full WDA_EXCLUDEFROMCAPTURE (invisible in captures) and
            # not the black WDA_MONITOR fallback. Deferred so the UI stays responsive.
            self.root.after(250, lambda r=restored: self._reactivate(r))
            self.refresh()
            return
        # Nothing hidden yet -> hide all currently-Activated (capture-excluded) windows.
        own = self._own_hwnd()
        targets = [w["hwnd"] for w in core.list_windows()
                   if w["hwnd"] != own
                   and core.get_affinity(w["hwnd"]) in (core.WDA_EXCLUDEFROMCAPTURE,
                                                        core.WDA_MONITOR)]
        if not targets:
            self.status.set("No Activated apps to hide from view. "
                            "Activate the apps you want, then press Ctrl+Alt+G.")
            return
        for hwnd in targets:
            core.set_window_hidden(hwnd, True)
        self._hidden_from_view = list(targets)
        self.status.set(f"Hid {len(targets)} window(s) from view (still running) "
                        "— press Ctrl+Alt+G to bring them back.")
        self.refresh()

    def _reactivate(self, hwnds):
        """Re-apply capture exclusion to windows that were just brought back into
        view, via the shared _bulk helper. hide() retries the real exclude and
        never falls back to black."""
        n = self._bulk(hwnds, "hide")
        self.status.set(f"Restored {len(hwnds)} window(s) to view (re-Activated {n}).")
        self.refresh(force=True)

    # ---- run in background ----
    def run_in_background(self):
        """Hide the controller window (no taskbar button, no tray icon); keep
        running with hotkeys live."""
        self.root.withdraw()
        self.status.set("Running in background — press Right Ctrl+Right Alt+Left Shift+X "
                        f"(or relaunch {APP_NAME}) to show it again. Hotkeys stay active.")

    def show_window(self):
        """Bring the (hidden) controller window back into view and to the front."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)   # pop above other windows...
        self.root.attributes("-topmost", False)  # ...but don't stay pinned
        self.root.focus_force()
        self._set_titlebar_dark(self._resolve_mode() == "dark")
        # Re-exclude self AFTER the window has finished re-compositing, so the
        # exclusion lands as full WDA_EXCLUDEFROMCAPTURE and not black.
        self.root.after(200, lambda: self._apply_self_hide(announce=False))
        self.refresh()

    def toggle_window(self):
        """Show the window if hidden, else hide it to background (RCtrl+RAlt+LShift+X)."""
        try:
            hidden = self.root.state() == "withdrawn"
        except Exception:
            hidden = False
        if hidden:
            self.show_window()
        else:
            self.run_in_background()

    def quit_app(self):
        # Safety: never leave windows stranded off-screen after we exit.
        for hwnd in self._hidden_from_view:
            core.set_window_hidden(hwnd, False)
        self._hidden_from_view = []
        try:
            self.bg.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---- background plumbing ----
    def _poll_events(self):
        dispatch = {
            HK_TOGGLE_SELECTED: self.toggle_selected,
            HK_TOGGLE_SELF: self.toggle_self,
            HK_TOGGLE_ALL: self.toggle_all,
            HK_HIDE_VIEW_ALL: self.hide_view_all,
            HK_SHOW_WINDOW: self.toggle_window,
            "show": self.show_window,
            "quit": self.quit_app,
        }
        try:
            while True:
                ev = self.event_q.get_nowait()
                if callable(ev):        # a closure marshaled from a worker thread
                    ev()
                    continue
                fn = dispatch.get(ev)
                if fn:
                    fn()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _check_hotkey_registered(self):
        reg = self.bg.wait_ready(0)
        if not reg:
            return  # not resolved yet; it's a daemon, ignore
        failed = [HOTKEY_LABELS[hid] for hid, ok in reg.items() if not ok]
        if not self.bg.hook_ok:
            failed.append(HOTKEY_LABELS[HK_SHOW_WINDOW] + " (hook)")
        if failed:
            self.status.set("Disabled (in use / blocked): " + ", ".join(failed))

    def on_close(self):
        """The window's [X] hides to the background instead of quitting, so the
        app keeps running with its hotkeys. Use the Quit button to actually exit."""
        if not self._close_hint_shown:
            self._close_hint_shown = True
            messagebox.showinfo(
                APP_NAME,
                f"{APP_NAME} keeps running in the background (no taskbar/tray icon).\n\n"
                "To bring this window back:\n"
                "• Press Right Ctrl + Right Alt + Left Shift + X, or\n"
                f"• Launch {APP_NAME} again (it reopens this instance).\n\n"
                "To exit completely, use the Quit button.\n\n"
                "Hotkeys stay active while hidden:\n"
                "  Ctrl+Alt+H  toggle selected      Ctrl+Alt+L  toggle all\n"
                "  Ctrl+Alt+A  toggle this app       Ctrl+Alt+G  hide all from view")
        self.run_in_background()


def main():
    # Single instance: if Exclusion is already running, ask it to show its window
    # and exit. Done BEFORE elevation so re-launching never triggers a UAC prompt
    # (the running instance allowed WM_APP_SHOW through UIPI via
    # ChangeWindowMessageFilterEx, so a non-elevated launch can post it).
    existing = user32.FindWindowW(HIDDEN_WND_CLASS, None)
    if existing:
        user32.PostMessageW(existing, WM_APP_SHOW, 0, 0)
        return

    # Self-elevate: if not admin, trigger UAC and relaunch this program elevated.
    if not is_admin():
        if relaunch_as_admin():
            return  # elevated instance started; quit this non-elevated one
        # User declined UAC or elevation failed — keep running with a warning
        # (still works for windows owned by non-elevated processes).

    root = tk.Tk()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on HiDPI
    except Exception:
        pass
    app = App(root)
    if is_admin():
        root.title(root.title() + "  [administrator]")
    else:
        app.status.set("Running WITHOUT admin — elevated windows can't be hidden. "
                       "Restart to retry UAC.")
    root.mainloop()


if __name__ == "__main__":
    main()
