# GenerousUnseen

A Windows utility that keeps chosen application windows **out of screen shares and
screenshots**, and can **hide them from the screen entirely** (running in the
background) with a global hotkey.

> The built executable is **`GenerousUnseen.exe`**, so it appears as the process
> **`GenerousUnseen`** in Task Manager — matching the window/app name.

It has two independent capabilities:

1. **Capture exclusion** — a window stays fully visible to you on the physical
   screen, but does **not** appear in screen recordings, screenshots, or a
   screen share (Zoom / Teams / Meet / OBS running on this PC). Uses the
   documented Windows API `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
2. **Hide from view (boss key)** — removes windows from the desktop **and** the
   taskbar (`ShowWindow(SW_HIDE)`) while their processes keep running, and brings
   them back on the next press.

> **What it does not do:** it cannot hide a window from a phone/camera pointed at
> the monitor, from someone physically looking at the screen, or from a hardware
> capture card on the HDMI output. Capture exclusion only affects *software*
> capture running on this machine.

---

## Requirements

- Windows 10 version 2004 (build 19041) or newer — required for
  `WDA_EXCLUDEFROMCAPTURE`. On older builds it falls back to `WDA_MONITOR`
  (the window shows as **black** in captures instead of being skipped).
- 64-bit Windows. Both **64-bit and 32-bit (WOW64)** target apps are supported —
  the injector detects the target's bitness and uses x64 or x86 shellcode
  accordingly.
- To run from source: **Python 3.11+** (uses only the standard library —
  `tkinter`, `ctypes`; no third-party packages).

---

## Files

| File | Purpose |
|------|---------|
| `capture_gui.py` | The GUI, global hotkeys, single-instance, background mode. Entry point. |
| `hide_from_capture.py` | Core: capture-exclusion API, cross-process injection, window listing, `ShowWindow` helpers. |
| `win_icons.py` | Extracts each app's icon and draws the checkbox composite (pure `ctypes` + `zlib`, no Pillow). |
| `GenerousUnseen.spec` | PyInstaller build recipe (one-file, windowed, admin manifest). |
| `dist/GenerousUnseen.exe` | The built standalone executable (process name: `GenerousUnseen`). |

---

## Running

### From the prebuilt exe (no Python needed)

Double-click **`dist/GenerousUnseen.exe`**. Windows shows a **UAC prompt**
(the admin request is baked into the exe) — click **Yes**. Admin rights are needed
to inject into other processes' windows.

### From source

```powershell
python capture_gui.py
```

It self-elevates: if not already admin, it relaunches itself through UAC.

---

## Building the exe

PyInstaller is required (`pip install pyinstaller`). Then:

```powershell
python -m PyInstaller GenerousUnseen.spec
```

The result is `dist/GenerousUnseen.exe`. The `--uac-admin` manifest is
already encoded in the spec, so double-clicking the exe prompts for elevation.

> Building regenerates `build/` (intermediate files, safe to delete) and a
> `build/GenerousUnseen/warn-GenerousUnseen.txt` file. That warnings
> file is **normal PyInstaller noise** (optional imports it probed and didn't
> find) — it does not indicate a problem with the build.

---

## Using the app

The main window lists every visible top-level window:

| Column | Meaning |
|--------|---------|
| **Active** | A checkbox + the app's icon. ☑ = the window is Activated (excluded from capture). Click it to toggle. |
| **Window title** | The window's title text. |
| **Process** | The owning `.exe`. |

### Appearance

A **Theme** selector in the toolbar switches between **System / Light / Dark**.
"System" follows the Windows app-theme setting (read from the registry), and the
window title bar is themed to match. Your choice is remembered between runs.

The currently-selected row stays selected even if it temporarily leaves the list
(e.g. after being hidden from view and restored, or filtered out and back).

The list **updates automatically** (about every 2 seconds): windows that open or
close, and capture-state changes, appear on their own. It only redraws when
something actually changed, so it doesn't flicker or lose your scroll position or
selection. The **Refresh** button forces an immediate update.

### Keeping windows Activated

Activation is **per window**: Activating one window never affects other windows of
the same app — activating one Chrome window leaves your other Chrome windows
untouched. A background check keeps each Activated window excluded, so if something
(an app repaint, or malware) clears a window's exclusion, GenerousUnseen re-applies
it within about two seconds — even while running in the background.

Activation lasts for the current session. It is **not** remembered across a restart
of GenerousUnseen (a closed window can't be re-identified afterward); only your
theme choice is saved to `config.json`.

Terminology used throughout:

- **Active / Activated** — the window is excluded from screen capture.
- **Inactive** — the window is captured normally (visible in shares/screenshots).

### Buttons

- **Active** — exclude the selected window from capture.
- **Inactive** — restore the selected window to normal capture.
- **Toggle** — flip the selected window.
- **Toggle all** — flip *every* activated app at once (and remember them so a
  second press restores exactly that set).
- **Hide all from view** — the boss key (see below).
- **Run in background** — hide the controller window (see *Running in the
  background* below).
- **Quit** — exit GenerousUnseen completely.
- **Activate this app itself** (checkbox, default **on**) — excludes
  GenerousUnseen's own window from capture so the controller doesn't show up in
  your share.

GenerousUnseen has **no taskbar button and no alt-tab entry** (it's a tool
window), so it stays out of the way; bring it up with
**Right Ctrl + Right Alt + Left Shift + X** or by launching it again.

---

## Global hotkeys

These work system-wide, even while the GenerousUnseen window is hidden.

| Hotkey | Action |
|--------|--------|
| **Ctrl+Alt+H** | Toggle **capture exclusion** on the selected window |
| **Ctrl+Alt+A** | Toggle **capture exclusion** on GenerousUnseen's own window |
| **Ctrl+Alt+L** | Toggle **capture exclusion** on all activated apps |
| **Ctrl+Alt+G** | **Hide all activated apps from view / restore** (boss key) |
| **Right Ctrl + Right Alt + Left Shift + X** | **Show / hide the GenerousUnseen window itself** (side-specific chord) |

If a combo is already taken by another program, GenerousUnseen reports which one
in the status bar and disables just that hotkey; the rest still work.

### The boss key (Ctrl+Alt+G)

1. Mark the apps you want as **Active** (checkbox or Ctrl+Alt+H).
2. Press **Ctrl+Alt+G** → those windows vanish from the desktop and taskbar but
   keep running in the background.
3. Press **Ctrl+Alt+G** again → they reappear.

While hidden this way, a window has **no taskbar button and can't be alt-tabbed** —
only GenerousUnseen can bring it back (Ctrl+Alt+G or the "Hide all from view"
button). Quitting GenerousUnseen **auto-restores** any windows it hid, so nothing
is left stranded. If GenerousUnseen is force-killed while windows are hidden,
relaunch it and press Ctrl+Alt+G, or restart the affected app.

---

## Running in the background

GenerousUnseen runs as a **tool window**: it has **no taskbar button and no tray
icon**. Click **Run in background** or the window's **[X]** to hide it; it keeps
running and all hotkeys stay active.

To bring the window back:

- Press **Right Ctrl + Right Alt + Left Shift + X** (toggles show/hide), or
- **Launch it again** — a second launch just reopens the already-running instance
  (single instance) instead of starting a new one, and does **not** prompt for UAC.

To exit completely, use the **Quit** button in the window.

---

## How it works (technical)

- **Capture exclusion** is set with `SetWindowDisplayAffinity`. That API only
  succeeds when called by the **process that owns the window**, so setting it on
  another app fails with `ERROR_ACCESS_DENIED` even as admin. GenerousUnseen
  therefore writes a tiny shellcode stub into the target with
  `VirtualAllocEx` + `WriteProcessMemory` and runs it via `CreateRemoteThread`;
  the stub calls the API from inside the owning process. The stub is **x64 for
  64-bit targets and x86 for 32-bit (WOW64) targets** — for the latter the 32-bit
  `user32!SetWindowDisplayAffinity` address is resolved by finding the target's
  32-bit `user32.dll` base (`EnumProcessModulesEx`) and parsing the export table
  of `SysWOW64\user32.dll`. Reading the current state (`GetWindowDisplayAffinity`)
  is allowed cross-process, so the list stays accurate without injection.
- **Own window / black avoidance** — on Windows 10 2004+ the exclusion always uses
  `WDA_EXCLUDEFROMCAPTURE` (invisible) with retries and never falls back to the
  black `WDA_MONITOR`, so re-showing a window is never a black rectangle.
- **Hide from view** uses `ShowWindow(SW_HIDE / SW_SHOW)`, which is allowed
  cross-process — no injection needed.
- **Icons** are pulled with `SHGetFileInfoW`, converted from `HICON` to RGBA via
  GDI, and encoded to PNG in pure Python; the checkbox is drawn and composited
  with the icon so both sit in the tree's image column.
- **Hotkeys + background** run on a dedicated thread that owns a hidden message
  window and registers the Ctrl+Alt+* hotkeys via `RegisterHotKey`. Because
  `RegisterHotKey` can't distinguish left/right modifiers, the show/hide chord
  (**Right Ctrl + Right Alt + Left Shift + X**) uses a `WH_KEYBOARD_LL` hook that
  inspects the specific side keys (`VK_RCONTROL`/`VK_RMENU`/`VK_LSHIFT`). The
  hidden window also receives a `WM_APP_SHOW` message so a second launch of the
  exe can reopen the running instance (single instance);
  `ChangeWindowMessageFilterEx` lets that message cross the UAC/UIPI boundary so
  re-launching never prompts for elevation. Events are marshaled to the Tk thread
  through a queue.

---

## Limitations

- Both 64-bit and 32-bit target apps are supported (via x64/x86 shellcode).
- Some **protected processes** (certain system/anti-cheat/DRM apps) refuse
  `OpenProcess` even when elevated — those can't be excluded.
- Injecting a remote thread is a pattern antivirus/EDR watch for; an unsigned exe
  may trigger a SmartScreen "unknown publisher" prompt (**More info → Run anyway**)
  or a Defender warning. Code-signing removes this but needs a certificate.
- On some GPUs/drivers (e.g. NVIDIA with Multiplane Overlay), an excluded window
  can render black in captures; disabling MPO resolves it. The app never uses the
  black mode itself on modern Windows.

---

## Note on responsible use

Capture exclusion is the same mechanism apps like password managers and messengers
use for privacy while streaming or presenting. Using it to conceal activity from
**monitoring you have agreed to** — remote proctored exams, interview screen-shares,
or employer monitoring software — typically violates those agreements and can have
real consequences. Use it for your own privacy on your own screen.
