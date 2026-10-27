"""
hide_from_capture.py

Excludes a window from screen capture / screen sharing using the documented
Windows API SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE).

The window stays fully visible to the local user, but shows up as blank/black
(or is entirely skipped) in:
  - Windows built-in screenshots (PrtScn, Snipping Tool)
  - OBS / game-capture and monitor-capture sources
  - Zoom / Teams / Meet screen sharing running on THIS machine

It does NOT hide the window from:
  - A phone/camera pointed at the monitor
  - Someone physically standing behind you
  - Hardware capture cards / a second PC capturing HDMI out

No DLL injection needed: SetWindowDisplayAffinity works cross-process on a
target HWND.

Requires: Windows 10 version 2004 (build 19041) or newer for
WDA_EXCLUDEFROMCAPTURE. On older builds use WDA_MONITOR (shows black instead).
"""

import ctypes
import os
import struct
import time
from ctypes import wintypes
import sys

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# Display-affinity constants
WDA_NONE = 0x00000000              # normal (capturable)
WDA_MONITOR = 0x00000001           # appears black in captures (older Win10)
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # skipped entirely (Win10 2004+)


def _windows_build():
    """Real OS build number via RtlGetVersion (not subject to the GetVersionEx
    manifest shim)."""
    class OSVERSIONINFOW(ctypes.Structure):
        _fields_ = [
            ("dwOSVersionInfoSize", wintypes.DWORD),
            ("dwMajorVersion", wintypes.DWORD),
            ("dwMinorVersion", wintypes.DWORD),
            ("dwBuildNumber", wintypes.DWORD),
            ("dwPlatformId", wintypes.DWORD),
            ("szCSDVersion", wintypes.WCHAR * 128),
        ]
    try:
        ntdll = ctypes.WinDLL("ntdll")
        osvi = OSVERSIONINFOW()
        osvi.dwOSVersionInfoSize = ctypes.sizeof(osvi)
        ntdll.RtlGetVersion(ctypes.byref(osvi))
        return osvi.dwBuildNumber
    except Exception:
        return 0


# WDA_EXCLUDEFROMCAPTURE (fully invisible in captures) needs Win10 2004 (19041).
# Below that, WDA_MONITOR (shows black) is the only option.
_EXCLUDE_SUPPORTED = _windows_build() >= 19041

# --- function prototypes -------------------------------------------------
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

# ShowWindow works cross-process (unlike SetWindowDisplayAffinity), so hiding a
# window from the desktop needs no injection.
SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL


def set_window_hidden(hwnd, hidden):
    """Fully hide a window from the desktop AND taskbar (SW_HIDE) or bring it
    back (SW_SHOW). The process keeps running while hidden. Cross-process safe."""
    user32.ShowWindow(hwnd, SW_HIDE if hidden else SW_SHOW)
    return True


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDisplayAffinity.restype = wintypes.BOOL

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _get_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n == 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_window_by_title(substring):
    """Return the HWND of the first visible top-level window whose title
    contains `substring` (case-insensitive), or None."""
    match = []
    needle = substring.lower()

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = _get_title(hwnd)
            if title and needle in title.lower():
                match.append(hwnd)
                return False  # stop enumerating
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return match[0] if match else None


def _get_process_name(hwnd):
    """Return (pid, image_name, full_path) for the process owning `hwnd`."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, "", ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return pid.value, "", ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return pid.value, os.path.basename(buf.value), buf.value
        return pid.value, "", ""
    finally:
        kernel32.CloseHandle(h)


def list_windows():
    """Return a list of dicts for visible, titled top-level windows:
    {"hwnd", "title", "pid", "proc"}."""
    results = []

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = _get_title(hwnd)
            if title:
                pid, proc, path = _get_process_name(hwnd)
                results.append(
                    {"hwnd": hwnd, "title": title, "pid": pid,
                     "proc": proc, "path": path}
                )
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return results


def get_pid(hwnd):
    """Return the PID that owns `hwnd`, or 0. Lightweight (no OpenProcess).

    Used to detect HWND reuse: Windows recycles handle values, so a window the
    user Activated can close and its HWND be handed to an unrelated new window.
    A PID captured at activation time no longer matching means the handle was
    reused and must not be re-excluded."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_affinity(hwnd):
    """Return the current display affinity (int) or None if it can't be read.
    Reading cross-process may fail on some systems; callers should tolerate None."""
    val = wintypes.DWORD()
    if user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(val)):
        return val.value
    return None


def affinity_name(value):
    return {
        WDA_NONE: "Inactive",
        WDA_MONITOR: "Activated (black)",
        WDA_EXCLUDEFROMCAPTURE: "Activated",
    }.get(value, "unknown")


# =========================================================================
# Cross-process injection.
#
# SetWindowDisplayAffinity only succeeds when called by the process that OWNS
# the window; a cross-process call fails with ERROR_ACCESS_DENIED even when
# elevated. To set it on another app's window we allocate a tiny x64 shellcode
# stub inside the target, have it call SetWindowDisplayAffinity(hwnd, affinity),
# and run it via CreateRemoteThread. The stub's return value (the BOOL from the
# API) becomes the remote thread's exit code, so we can tell if it worked.
# =========================================================================

# --- extra Win32 prototypes (careful restypes to avoid 64-bit truncation) --
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD
]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD
]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL
kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsWow64Process.restype = wintypes.BOOL

# psapi: enumerate a target's loaded modules (to find its 32-bit user32 base).
psapi = ctypes.WinDLL("psapi", use_last_error=True)
LIST_MODULES_32BIT = 0x01
psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD
]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL
psapi.GetModuleBaseNameW.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD
]
psapi.GetModuleBaseNameW.restype = wintypes.DWORD


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)
]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p
]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL


def enable_debug_privilege():
    """Best-effort SeDebugPrivilege so we can open processes owned by other
    users / higher integrity when running elevated. Returns True on success."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        return bool(advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None))
    finally:
        kernel32.CloseHandle(token)


def _build_shellcode(hwnd, affinity, func_addr):
    """x64 stub: SetWindowDisplayAffinity(hwnd, affinity); return its BOOL."""
    return (
        b"\x48\xB9" + struct.pack("<Q", hwnd & 0xFFFFFFFFFFFFFFFF)   # mov rcx, hwnd
        + b"\xBA" + struct.pack("<I", affinity & 0xFFFFFFFF)         # mov edx, affinity
        + b"\x48\xB8" + struct.pack("<Q", func_addr)                 # mov rax, &SetWindowDisplayAffinity
        + b"\x48\x83\xEC\x28"                                        # sub rsp, 0x28 (shadow + align)
        + b"\xFF\xD0"                                                # call rax
        + b"\x48\x83\xC4\x28"                                        # add rsp, 0x28
        + b"\xC3"                                                    # ret  (rax = BOOL result)
    )


def _build_shellcode_x86(hwnd, affinity, func_addr):
    """x86 (stdcall) stub for WOW64 targets: a remote thread in a 32-bit process
    runs in 32-bit mode. SetWindowDisplayAffinity(hwnd, affinity); return BOOL."""
    return (
        b"\x68" + struct.pack("<I", affinity & 0xFFFFFFFF)     # push affinity
        + b"\x68" + struct.pack("<I", hwnd & 0xFFFFFFFF)       # push hwnd
        + b"\xB8" + struct.pack("<I", func_addr & 0xFFFFFFFF)  # mov eax, &SetWindowDisplayAffinity
        + b"\xFF\xD0"                                          # call eax
        + b"\xC2\x04\x00"                                      # ret 4 (clean the thread arg)
    )


def _find_module_base(hproc, module_name):
    """Base address of a loaded 32-bit module (e.g. 'user32.dll') in `hproc`."""
    arr = (ctypes.c_void_p * 1024)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcessModulesEx(
            hproc, arr, ctypes.sizeof(arr), ctypes.byref(needed), LIST_MODULES_32BIT):
        return None
    count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), 1024)
    name = ctypes.create_unicode_buffer(260)
    want = module_name.lower()
    for i in range(count):
        hmod = arr[i]
        if not hmod:
            continue
        if psapi.GetModuleBaseNameW(hproc, hmod, name, 260) and name.value.lower() == want:
            return int(hmod)
    return None


def _export_rva(dll_path, func_name):
    """Parse a PE file on disk and return the RVA of an exported function, or
    None (also None for forwarded exports)."""
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return None
    num_sections = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    size_opt = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    opt_off = e_lfanew + 24
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic == 0x10B:        # PE32
        dd_off = opt_off + 96
    elif magic == 0x20B:      # PE32+
        dd_off = opt_off + 112
    else:
        return None
    export_rva, export_size = struct.unpack_from("<II", data, dd_off)
    if not export_rva:
        return None

    sec_off = opt_off + size_opt
    sections = []
    for i in range(num_sections):
        so = sec_off + i * 40
        vsize = struct.unpack_from("<I", data, so + 8)[0]
        vaddr = struct.unpack_from("<I", data, so + 12)[0]
        rawsize = struct.unpack_from("<I", data, so + 16)[0]
        praw = struct.unpack_from("<I", data, so + 20)[0]
        sections.append((vaddr, max(vsize, rawsize), praw))

    def rva2off(rva):
        for vaddr, vspan, praw in sections:
            if vaddr <= rva < vaddr + vspan:
                return praw + (rva - vaddr)
        return None

    exp_off = rva2off(export_rva)
    if exp_off is None:
        return None
    num_names = struct.unpack_from("<I", data, exp_off + 0x18)[0]
    addr_funcs = struct.unpack_from("<I", data, exp_off + 0x1C)[0]
    addr_names = struct.unpack_from("<I", data, exp_off + 0x20)[0]
    addr_ords = struct.unpack_from("<I", data, exp_off + 0x24)[0]
    names_off = rva2off(addr_names)
    ords_off = rva2off(addr_ords)
    funcs_off = rva2off(addr_funcs)
    if None in (names_off, ords_off, funcs_off):
        return None
    want = func_name.encode()
    for i in range(num_names):
        name_rva = struct.unpack_from("<I", data, names_off + i * 4)[0]
        noff = rva2off(name_rva)
        if noff is None:
            continue
        end = data.index(b"\x00", noff)
        if data[noff:end] == want:
            ordinal = struct.unpack_from("<H", data, ords_off + i * 2)[0]
            func_rva = struct.unpack_from("<I", data, funcs_off + ordinal * 4)[0]
            if export_rva <= func_rva < export_rva + export_size:
                return None  # forwarded export, not a real address
            return func_rva
    return None


def inject_set_affinity(hwnd, affinity):
    """Set `affinity` on `hwnd` by running SetWindowDisplayAffinity INSIDE the
    owning process. Returns the API's BOOL result (nonzero == success).
    Raises OSError on any injection failure."""
    hwnd = int(hwnd)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        raise OSError("Could not resolve the target window's process.")

    enable_debug_privilege()  # best effort; ignored if it fails

    access = (PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION
              | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ)
    hproc = kernel32.OpenProcess(access, False, pid.value)
    if not hproc:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wow = wintypes.BOOL()
        is_wow64 = bool(kernel32.IsWow64Process(hproc, ctypes.byref(wow)) and wow.value)

        if is_wow64:
            # 32-bit target: resolve the 32-bit user32 export and run x86 shellcode.
            base = _find_module_base(hproc, "user32.dll")
            if not base:
                raise OSError("Could not locate 32-bit user32.dll in the target process.")
            rva = _export_rva(
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                             "SysWOW64", "user32.dll"),
                "SetWindowDisplayAffinity")
            if not rva:
                raise OSError(r"Could not resolve SetWindowDisplayAffinity in "
                              r"SysWOW64\user32.dll.")
            code = _build_shellcode_x86(hwnd, affinity, base + rva)
        else:
            func = kernel32.GetProcAddress(
                kernel32.GetModuleHandleW("user32.dll"), b"SetWindowDisplayAffinity")
            if not func:
                raise ctypes.WinError(ctypes.get_last_error())
            code = _build_shellcode(hwnd, affinity, func)
        remote = kernel32.VirtualAllocEx(
            hproc, None, len(code), MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            written = ctypes.c_size_t(0)
            if not kernel32.WriteProcessMemory(
                    hproc, remote, code, len(code), ctypes.byref(written)):
                raise ctypes.WinError(ctypes.get_last_error())

            thread = kernel32.CreateRemoteThread(
                hproc, None, 0, remote, None, 0, None)
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                kernel32.WaitForSingleObject(thread, 5000)
                exit_code = wintypes.DWORD()
                kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code))
                return exit_code.value  # BOOL from SetWindowDisplayAffinity
            finally:
                kernel32.CloseHandle(thread)
        finally:
            kernel32.VirtualFreeEx(hproc, remote, 0, MEM_RELEASE)
    finally:
        kernel32.CloseHandle(hproc)


def set_affinity(hwnd, affinity):
    """Direct (same-process only) set. Cross-process callers must use
    inject_set_affinity() instead."""
    ok = user32.SetWindowDisplayAffinity(hwnd, affinity)
    if not ok:
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)
    return True


def hide(hwnd):
    """Exclude the window from capture via injection.

    On Windows 10 2004+ this uses WDA_EXCLUDEFROMCAPTURE (the window is skipped
    entirely in captures) and RETRIES on failure — it can transiently fail right
    after a window is shown. It deliberately does NOT fall back to WDA_MONITOR
    there, because that would leave a visible BLACK rectangle in screenshots.
    Only on pre-2004 Windows (no exclude support) is WDA_MONITOR used.

    After a successful injection it reads the affinity back as a BEST-EFFORT
    confirmation; a successful inject is still reported as success even if the
    read-back can't confirm it yet (e.g. a GPU-composited window mid-recomposite
    that transiently reports WDA_NONE)."""
    if _EXCLUDE_SUPPORTED:
        injected = False
        for attempt in range(6):
            if inject_set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                injected = True
                # Best-effort confirmation: WDA_EXCLUDEFROMCAPTURE (or an
                # unreadable None) means done. Any other read-back may be a
                # transient drop right after showing, so retry to try to
                # confirm — but a successful injection is NOT downgraded to a
                # failure just because the read-back hasn't caught up.
                if get_affinity(hwnd) in (WDA_EXCLUDEFROMCAPTURE, None):
                    return "Activated"
            time.sleep(0.05)  # let the window settle, then retry (never go black)
        if injected:
            return "Activated"  # injected OK; read-back never confirmed in time
        raise OSError("SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) failed "
                      "after retries.")
    if inject_set_affinity(hwnd, WDA_MONITOR):
        return "Activated (black)"
    raise OSError("SetWindowDisplayAffinity returned FALSE inside the target process.")


def show(hwnd):
    """Restore normal capture behavior via injection."""
    if not inject_set_affinity(hwnd, WDA_NONE):
        raise OSError("SetWindowDisplayAffinity returned FALSE inside the target process.")


def activate_own(hwnd):
    """Exclude OUR OWN window from capture (same-process direct call — no
    injection needed). Like hide(), it resets the affinity first (clears any
    stuck WDA_MONITOR/black state), retries, and never falls back to the black
    WDA_MONITOR on modern Windows — so re-showing the app is never black."""
    if _EXCLUDE_SUPPORTED:
        for attempt in range(6):
            try:
                set_affinity(hwnd, WDA_NONE)               # uncheck (reset)
                set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)  # check again (exclude)
                return "Activated"
            except OSError:
                time.sleep(0.05)  # let the window settle, then retry (never black)
        raise OSError("SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) failed "
                      "after retries.")
    set_affinity(hwnd, WDA_MONITOR)
    return "Activated (black)"


def deactivate_own(hwnd):
    """Make OUR OWN window capturable again (same-process direct call)."""
    set_affinity(hwnd, WDA_NONE)


def main(argv):
    if len(argv) < 2:
        print("Usage:")
        print("  python hide_from_capture.py <title-substring>   # hide by title")
        print("  python hide_from_capture.py --foreground        # hide active window")
        print("  python hide_from_capture.py --show <title>      # un-hide")
        return 1

    if argv[1] == "--foreground":
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            print("No foreground window.")
            return 1
        print(f'Target: "{_get_title(hwnd)}"  ->  {hide(hwnd)}')
        return 0

    if argv[1] == "--show":
        if len(argv) < 3:
            print("Provide a title substring to un-hide.")
            return 1
        hwnd = find_window_by_title(argv[2])
        if not hwnd:
            print("No matching window found.")
            return 1
        show(hwnd)
        print(f'Target: "{_get_title(hwnd)}"  ->  restored to normal capture')
        return 0

    hwnd = find_window_by_title(argv[1])
    if not hwnd:
        print(f'No visible window with title containing "{argv[1]}".')
        return 1
    print(f'Target: "{_get_title(hwnd)}"  ->  {hide(hwnd)}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
