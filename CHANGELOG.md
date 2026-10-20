# Changelog

All notable changes to GenerousUnseen are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] - 2026-07-06

Initial release.

### Added
- **Capture exclusion** — hide chosen windows from screen shares, recordings, and
  screenshots via `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`. On Windows
  builds older than 10 2004 it falls back to `WDA_MONITOR` (window shows black).
- **Cross-process injection** — sets capture exclusion on windows owned by other
  processes by running a shellcode stub via `VirtualAllocEx` +
  `WriteProcessMemory` + `CreateRemoteThread` (the API only succeeds from the
  window's owning process). Both **64-bit (x64 stub)** and **32-bit / WOW64 (x86
  stub**, with the export resolved from `SysWOW64\user32.dll`) targets are
  supported.
- **Hide from view (boss key)** — fully hide all activated apps from the desktop
  and taskbar with `ShowWindow(SW_HIDE)` and restore them on the next press;
  hidden apps keep running in the background.
- **Live window list** — each app's icon, a toggle switch, the window title, and
  the process name. Updates automatically (~2s) without flicker, losing scroll
  position, or losing the selection. Includes a search filter.
- **Per-window activation** — Activating a window affects only that window, never
  other windows of the same process; a background check re-applies exclusion (on
  its own timer, so it works even while running in the background) if a window
  loses it.
- **App-icon extraction** — icons pulled with `SHGetFileInfoW` and encoded to PNG
  in pure Python (no Pillow), composited with the toggle in one column.
- **Theming** — System / Light / Dark selector (System follows the Windows app
  theme), including a themed title bar. The choice is remembered.
- **Global hotkeys**:
  - `Ctrl+Alt+H` — toggle capture on the selected window
  - `Ctrl+Alt+A` — toggle capture on GenerousUnseen's own window
  - `Ctrl+Alt+L` — toggle capture on all activated apps
  - `Ctrl+Alt+G` — hide all activated apps from view / restore (boss key)
  - `Right Ctrl + Right Alt + Left Shift + X` — show / hide the GenerousUnseen
    window itself (side-specific chord via a low-level keyboard hook)
- **Background operation** — runs as a tool window with **no taskbar button, no
  alt-tab entry, and no tray icon**; hotkeys stay active while hidden. A second
  launch reopens the running instance (single instance) without a new UAC prompt.
- **Self-elevation** — relaunches through UAC to obtain the admin rights needed
  for injection; the frozen exe carries a `requireAdministrator` manifest.
- **Standalone build** — one-file `dist/GenerousUnseen.exe` via PyInstaller
  (`GenerousUnseen.spec`), no Python required to run.

### Safety
- Quitting normally restores any windows hidden from view, so none are stranded.
- Hotkey conflicts are reported per-key in the status bar instead of failing
  silently.
