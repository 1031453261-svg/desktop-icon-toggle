"""
桌面图标控制工具 — 隐藏/显示 Windows 桌面图标，支持系统托盘常驻。

特性：
- 关闭窗口 → 最小化到系统托盘（不退出）
- 系统托盘右键菜单：显示/隐藏图标、退出
- 托盘图标双击切换显示/隐藏
- 退出时自动恢复桌面图标（不会留下隐藏状态）
"""
import ctypes
from ctypes import wintypes
import tkinter as tk
import threading

# ============================================================
# Windows API constants
# ============================================================
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# Window messages
WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_COMMAND = 0x0111

# Window styles
WS_EX_LEFT = 0

# Tray icon
NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4
NIIF_INFO = 1

# Menu
MF_STRING = 0
MF_SEPARATOR = 0x800
TPM_LEFTALIGN = 0
TPM_RIGHTBUTTON = 2

# Menu item IDs
IDM_SHOW = 1001
IDM_HIDE = 1002
IDM_EXIT = 1003

# Desktop icon control
GWL_STYLE = -16
WS_VISIBLE = 0x10000000
SW_HIDE = 0
SW_SHOW = 5


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", wintypes.BYTE * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),      # same underlying type
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ============================================================
# Desktop icon control
# ============================================================
def get_desktop_shell_view():
    """Return HWND of the SHELLDLL_DefView that hosts desktop icons, or None."""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None

    hwnd_found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        sv = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if sv:
            hwnd_found.append(sv)
            return False
        return True

    user32.EnumWindows(enum_callback, 0)

    if hwnd_found:
        return hwnd_found[0]

    # Fallback: direct child of Progman
    sv = user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
    return sv if sv else None


def is_desktop_icons_visible():
    hwnd = get_desktop_shell_view()
    if hwnd:
        return bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_VISIBLE)
    return False


def hide_desktop_icons():
    hwnd = get_desktop_shell_view()
    if hwnd:
        user32.ShowWindow(hwnd, SW_HIDE)


def show_desktop_icons():
    hwnd = get_desktop_shell_view()
    if hwnd:
        user32.ShowWindow(hwnd, SW_SHOW)


# ============================================================
# Tray icon — uses a dedicated hidden window + message pump thread
# ============================================================
class TrayIcon:
    def __init__(self, on_show, on_hide, on_exit):
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_exit = on_exit
        self._hwnd = None
        self._running = False

    def start(self):
        """Create hidden window, add tray icon, start message pump thread."""
        self._running = True
        thread = threading.Thread(target=self._message_loop, daemon=True)
        thread.start()

    def _message_loop(self):
        """Create a hidden window and run its message pump (runs in thread)."""
        class_name = "DesktopIconToggleTray"

        # Get module handle
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

        # Register a window class (keep reference to prevent GC of the callback)
        self._wndproc = self._get_wndproc()
        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wndclass.hInstance = hinst
        wndclass.lpszClassName = class_name

        atom = user32.RegisterClassW(ctypes.byref(wndclass))
        if not atom:
            err = ctypes.get_last_error()
            raise RuntimeError(f"RegisterClassW failed: error {err}")

        # Create the hidden window
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LEFT,
            class_name,
            "TrayHelper",
            0,
            0, 0, 0, 0,
            0, 0, hinst, 0,
        )

        if not self._hwnd:
            err = ctypes.get_last_error()
            raise RuntimeError(f"CreateWindowExW failed: error {err}")

        # Add the tray icon
        self._add_tray_icon()

        # Message loop
        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _get_wndproc(self):
        """Create the WNDPROC callback that handles tray messages."""
        # Use unsigned pointer-sized types for WPARAM/LPARAM to avoid overflow
        # WPARAM is already c_ulonglong (unsigned 64-bit)
        UINT_PTR = wintypes.WPARAM
        LRESULT = wintypes.LPARAM

        # Fix DefWindowProcW argtypes to accept unsigned values
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, UINT_PTR, UINT_PTR]
        user32.DefWindowProcW.restype = LRESULT

        @ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, UINT_PTR, UINT_PTR)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONDBLCLK:
                    try:
                        if is_desktop_icons_visible():
                            self.on_hide()
                        else:
                            self.on_show()
                    except Exception:
                        pass
                    return 0
                if lparam == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                    return 0

            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                try:
                    if cmd == IDM_SHOW:
                        self.on_show()
                    elif cmd == IDM_HIDE:
                        self.on_hide()
                    elif cmd == IDM_EXIT:
                        self.on_exit()
                except Exception:
                    pass
                return 0

            if msg == WM_CLOSE:
                return 0

            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return wnd_proc

    def _add_tray_icon(self):
        hicon = user32.LoadIconW(0, 32512)  # IDI_APPLICATION
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = "桌面图标控制"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _remove_tray_icon(self):
        if not self._hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def _show_menu(self, hwnd):
        menu = user32.CreatePopupMenu()

        if is_desktop_icons_visible():
            user32.AppendMenuW(menu, MF_STRING, IDM_HIDE, "隐藏桌面图标")
        else:
            user32.AppendMenuW(menu, MF_STRING, IDM_SHOW, "显示桌面图标")

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_EXIT, "退出（自动恢复图标）")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))

        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(
            menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, hwnd, None,
        )
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)

    def stop(self):
        """Remove tray icon and stop the message pump."""
        self._remove_tray_icon()
        self._running = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)


# ============================================================
# Callback runner — executes callbacks on tkinter's main thread
# ============================================================
class TkCallbackRunner:
    """Wraps callbacks so they execute safely on tkinter's main thread via .after()."""

    def __init__(self, root):
        self._root = root

    def wrap(self, fn):
        """Return a wrapper that schedules fn to run on the tkinter main thread."""
        def wrapper():
            self._root.after(0, fn)
        return wrapper


# ============================================================
# Main application window (tkinter) — polished UI
# ============================================================
class DesktopIconApp:
    # Color scheme
    BG        = "#1a1d23"   # dark background
    CARD      = "#242830"   # card surface
    ACCENT    = "#5b8af7"   # primary accent blue
    GREEN     = "#43b581"
    RED       = "#f04747"
    TEXT      = "#e0e0e0"
    SUBTLE    = "#6b6e77"
    BORDER    = "#343840"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("桌面图标控制")
        self.root.geometry("296x280")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        # Close (X) → minimize to tray
        self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        # Thread-safe callback runner
        runner = TkCallbackRunner(self.root)

        # Start tray icon (callback wrappers ensure thread safety)
        self.tray = TrayIcon(
            on_show=runner.wrap(self.show_icons),
            on_hide=runner.wrap(self.hide_icons),
            on_exit=runner.wrap(self.exit_app),
        )
        self.tray.start()

        # Build UI
        self._build_ui()
        self._refresh_status()

        # Handle clicking taskbar icon to restore
        self.root.bind("<Map>", lambda e: self._refresh_status())

    # ── layout helpers ──────────────────────────────

    def _card(self, parent, **kw):
        """A rounded container with the card background."""
        f = tk.Frame(parent, bg=self.CARD, highlightthickness=0, **kw)
        return f

    def _label(self, parent, text, size=10, color=None, bold=False, anchor="w", **kw):
        weight = "bold" if bold else "normal"
        return tk.Label(
            parent, text=text,
            font=("Microsoft YaHei UI", size, weight),
            fg=color or self.TEXT, bg=parent["bg"],
            anchor=anchor, **kw,
        )

    def _dot(self, parent, color):
        """A 10px colored circle drawn on a Canvas."""
        c = tk.Canvas(parent, width=16, height=16,
                      bg=parent["bg"], highlightthickness=0)
        c.create_oval(3, 3, 13, 13, fill=color, outline="", tags="dot")
        return c

    # ── build ───────────────────────────────────────

    def _build_ui(self):
        # ── top bar: icon + title ──
        top = tk.Frame(self.root, bg=self.BG)
        top.pack(fill=tk.X, padx=22, pady=(22, 0))

        # small decorative icon (a 2×2 grid of dots suggesting … desktop icons)
        icon_c = tk.Canvas(top, width=22, height=22,
                           bg=self.BG, highlightthickness=0)
        icon_c.place(x=0, y=2)
        for r in range(2):
            for c2 in range(2):
                x0, y0 = 1 + c2 * 11, 1 + r * 11
                icon_c.create_rectangle(
                    x0, y0, x0 + 7, y0 + 7,
                    fill=self.ACCENT, outline="",
                )

        self._label(top, "桌面图标控制", size=12, bold=True).place(x=32, y=0)

        # ── card: status section ──
        card = self._card(self.root)
        card.pack(fill=tk.X, padx=16, pady=(16, 0), ipady=6)

        # current state label
        self._label(card, "当前状态", size=8, color=self.SUBTLE).pack(
            side=tk.TOP, anchor="w", padx=16, pady=(12, 0),
        )

        status_row = tk.Frame(card, bg=self.CARD)
        status_row.pack(fill=tk.X, padx=16, pady=(4, 12))

        # colored dot + status text
        self._dot_canvas = tk.Canvas(
            status_row, width=16, height=16,
            bg=self.CARD, highlightthickness=0,
        )
        self._dot_canvas.pack(side=tk.LEFT, pady=(1, 0))

        self._status_text = self._label(
            status_row, "", size=10, color=self.TEXT,
        )
        self._status_text.pack(side=tk.LEFT, padx=(6, 0))

        # ── card: action section ──
        card2 = self._card(self.root)
        card2.pack(fill=tk.X, padx=16, pady=(10, 0), ipady=6)

        self._label(card2, "操作", size=8, color=self.SUBTLE).pack(
            side=tk.TOP, anchor="w", padx=16, pady=(12, 0),
        )

        # the toggle button — drawn on a Canvas for full style control
        btn_frame = tk.Frame(card2, bg=self.CARD)
        btn_frame.pack(fill=tk.X, padx=16, pady=(6, 14))

        self.btn = tk.Button(
            btn_frame,
            text="",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#ffffff", bg=self.ACCENT,
            activeforeground="#ffffff",
            activebackground=self._lighten(self.ACCENT, 0.12),
            relief="flat", bd=0,
            padx=16, pady=8,
            cursor="hand2",
            command=self._toggle,
        )
        self.btn.pack(fill=tk.X, ipady=2)

        # ── bottom hint ──
        hint = self._label(
            self.root,
            "关闭窗口最小化到托盘  ·  退出时自动恢复图标",
            size=8, color=self.SUBTLE, anchor="center",
        )
        hint.pack(side=tk.BOTTOM, pady=(0, 16))

    # ── helpers ─────────────────────────────────────

    @staticmethod
    def _lighten(hex_color, amount):
        """Lighten a hex color by `amount` (0–1)."""
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        r = min(255, int(r + (255 - r) * amount))
        g = min(255, int(g + (255 - g) * amount))
        b = min(255, int(b + (255 - b) * amount))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ── state ───────────────────────────────────────

    def _refresh_status(self):
        visible = is_desktop_icons_visible()
        color = self.GREEN if visible else self.RED
        text  = "桌面图标可见" if visible else "桌面图标已隐藏"
        btn_text = "隐  藏  图  标" if visible else "显  示  图  标"

        # Update dot
        self._dot_canvas.delete("dot")
        self._dot_canvas.create_oval(3, 3, 13, 13, fill=color, outline="", tags="dot")

        # Update status text
        self._status_text.config(text=text, fg=color)

        # Update button color
        btn_bg = self.ACCENT
        self.btn.config(
            text=btn_text,
            bg=btn_bg,
            activebackground=self._lighten(btn_bg, 0.12),
        )

    def _toggle(self):
        if is_desktop_icons_visible():
            self.hide_icons()
        else:
            self.show_icons()

    def hide_icons(self):
        hide_desktop_icons()
        self._refresh_status()

    def show_icons(self):
        show_desktop_icons()
        self._refresh_status()

    def _minimize_to_tray(self):
        """Close button → hide window (keep running in tray)."""
        self.root.withdraw()

    def exit_app(self):
        """Exit: restore desktop icons, clean up tray, close window."""
        if not is_desktop_icons_visible():
            show_desktop_icons()
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = DesktopIconApp()
    app.run()
