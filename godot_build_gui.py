#!/usr/bin/env python3
# ==============================================================================
#  godot_build_gui.py - Godot Build Manager (GTK 4 + libadwaita)
# ==============================================================================
#
#  Graphical front end for godot_build.py. Every operation runs the CLI in
#  `--json` mode and renders its event stream, so the CLI stays the single
#  source of truth and both can be used interchangeably.
#
#  Launched from the "Build Manager" / "Check for Engine Updates" actions on
#  the Godot launcher entry (right click). Single instance: launching it again
#  raises the existing window.
#
#      godot_build_gui.py [--page NAME] [--check-updates]
#
#  Pages: overview, source, editor, templates, encryption, activity, logs,
#         diagnostics, settings
#
#  Styling uses only libadwaita style classes and CSS variables, so the app
#  follows the system theme, accent colour and dark mode.
# ==============================================================================

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource  # noqa: E402
except (ValueError, ImportError):
    GtkSource = None

APP_ID = "io.github.t1nkieplayz.GodotBuildManager"
REPO_URL = "https://github.com/T1nkiePlayz/godot-build-manager"
APP_NAME = "Godot Build Manager"
APP_VERSION = "1.2.0"
BACKEND = Path(__file__).resolve().with_name("godot_build.py")
RELEASE_NOTES = """
<p>New in 1.2.0:</p>
<ul>
  <li>Cross-compile toolchains installer on the Diagnostics page: llvm-mingw for Windows, osxcross and MoltenVK for macOS</li>
  <li>osxcross is built from an Xcode download or a packaged macOS SDK, with live progress</li>
  <li>New setting: Vulkan on macOS (MoltenVK), so Intel Macs get Vulkan instead of OpenGL</li>
  <li>Export Templates links straight to toolchain setup when Windows or macOS is unavailable</li>
</ul>
<p>New in 1.1.0:</p>
<ul>
  <li>Editor snapshots: every successful build is kept, and the launcher can roll back to any of them</li>
  <li>Update checks after opening Godot, with a notification when upstream has new changes</li>
  <li>Readable changelog: pull request titles grouped by source area, watched areas highlighted</li>
  <li>Build History page with duration chart, cache hit rates and time-left estimates during builds</li>
  <li>Passphrase-protected encryption key backup, verify and restore</li>
  <li>Storage page showing caches, objects, snapshots and templates, with cleanup</li>
  <li>Activity output now uses the theme's text colour</li>
</ul>
"""
PREFS_FILE = Path(GLib.get_user_config_dir()) / "godot-build-manager" / "prefs.json"

OUTPUT_MAX_LINES = 12000
CLIPBOARD_CLEAR_SECONDS = 45
CANCEL_GRACE_SECONDS = 20

PAGES = [
    # (name, title, icon candidates, section)
    ("overview", "Overview", ("go-home-symbolic", "user-home-symbolic"), "Build"),
    ("source", "Source", ("emblem-synchronizing-symbolic", "view-refresh-symbolic"), "Build"),
    ("editor", "Editor", ("applications-engineering-symbolic", "preferences-other-symbolic"), "Build"),
    ("templates", "Export Templates", ("package-x-generic-symbolic", "folder-symbolic"), "Build"),
    ("encryption", "Encryption", ("channel-secure-symbolic", "security-high-symbolic"), "Security"),
    ("activity", "Activity", ("utilities-system-monitor-symbolic", "system-run-symbolic"), "Monitor"),
    ("logs", "Build Logs", ("document-open-recent-symbolic", "text-x-generic-symbolic"), "Monitor"),
    ("history", "Build History", ("power-profile-performance-symbolic", "utilities-system-monitor-symbolic",
                                  "x-office-spreadsheet-symbolic"), "Monitor"),
    ("diagnostics", "Diagnostics", ("emblem-ok-symbolic", "dialog-information-symbolic"), "Monitor"),
    ("storage", "Storage", ("drive-harddisk-symbolic", "folder-symbolic"), "Configure"),
    ("settings", "Settings", ("preferences-system-symbolic", "emblem-system-symbolic"), "Configure"),
]

CSS = """
.hero { padding: 6px 4px 18px 4px; }
.hero-title { font-size: 2.1em; font-weight: 800; }
.hero-icon { -gtk-icon-shadow: 0 2px 10px alpha(black, 0.25); }

.chip {
  padding: 3px 11px;
  border-radius: 999px;
  font-size: 0.85em;
  font-weight: 700;
  color: var(--window-fg-color);
  background: color-mix(in srgb, currentColor 9%, transparent);
}
.chip.accent  { color: var(--accent-color);  background: color-mix(in srgb, var(--accent-color) 16%, transparent); }
.chip.success { color: var(--success-color); background: color-mix(in srgb, var(--success-color) 16%, transparent); }
.chip.warning { color: var(--warning-color); background: color-mix(in srgb, var(--warning-color) 16%, transparent); }
.chip.error   { color: var(--error-color);   background: color-mix(in srgb, var(--error-color) 16%, transparent); }

.stat-card { padding: 16px 18px; min-height: 92px; }
.stat-card .stat-label { font-size: 0.82em; font-weight: 700; opacity: 0.65; letter-spacing: 0.04em; }
.stat-card .stat-value { font-size: 1.45em; font-weight: 800; }
.stat-card .stat-caption { font-size: 0.86em; opacity: 0.65; }
.stat-card .stat-icon { opacity: 0.5; }

.page-bottom-bar { padding: 10px 18px; }
.page-bottom-bar .summary { opacity: 0.75; }

.activity-card {
  margin: 6px 8px 10px 8px;
  padding: 10px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--accent-bg-color) 14%, transparent);
}
.activity-card progressbar trough, .activity-card progressbar progress { min-height: 4px; }

.activity-header { padding: 18px 24px 10px 24px; }
.activity-elapsed { font-feature-settings: "tnum"; }
.job-time { font-feature-settings: "tnum"; opacity: 0.7; }
.job-progress { min-width: 130px; }

.output-frame { border-radius: 12px; }
.output-view, .output-view text { background: transparent; color: var(--view-fg-color); }
.output-view { padding: 10px 12px; font-size: 0.9em; }

.failure-card { padding: 12px 6px 0 14px; border: 1px solid color-mix(in srgb, var(--error-color) 45%, transparent); }
.failure-text { font-size: 0.88em; color: var(--error-color); }

.key-text { font-family: monospace; font-size: 0.95em; letter-spacing: 0.02em; }
.mono { font-family: monospace; }
.sidebar-badge { min-width: 18px; padding: 0 6px; border-radius: 9px; font-size: 0.75em; font-weight: 800;
                 color: var(--accent-fg-color); background: var(--accent-bg-color); }
.log-list row { padding: 4px 2px; }
levelbar.disk-bar block.filled.disk-ok   { background-color: var(--accent-bg-color); }
levelbar.disk-bar block.filled.disk-warn { background-color: var(--warning-color); }
levelbar.disk-bar block.filled.disk-full { background-color: var(--error-color); }
levelbar.size-bar block.filled { background-color: var(--accent-bg-color); }
levelbar.disk-bar block, levelbar.size-bar block { min-height: 6px; }
.step-number { min-width: 26px; min-height: 26px; border-radius: 13px; font-weight: 800;
               color: var(--accent-fg-color); background: var(--accent-bg-color); }
"""


# ------------------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------------------

def human_size(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def to_datetime(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value).astimezone()
        parsed = dt.datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (ValueError, OSError):
        return None


def human_ago(value) -> str:
    when = to_datetime(value)
    if when is None:
        return "never"
    seconds = (dt.datetime.now().astimezone() - when).total_seconds()
    if seconds < 45:
        return "just now"
    for limit, div, unit in ((3600, 60, "minute"), (86400, 3600, "hour"), (86400 * 30, 86400, "day"),
                             (86400 * 365, 86400 * 30, "month")):
        if seconds < limit:
            n = max(1, round(seconds / div))
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    n = round(seconds / (86400 * 365))
    return f"{n} year{'s' if n != 1 else ''} ago"


def human_date(value) -> str:
    when = to_datetime(value)
    return when.strftime("%a %d %b %Y, %H:%M") if when else "—"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def human_eta(seconds: float) -> str:
    """Coarse remaining-time phrase: precise ETAs for compiles are fiction."""
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    return f"about {minutes // 60} h {minutes % 60:02d} min"


_icon_cache: dict[tuple, str] = {}


def icon(*candidates: str) -> str:
    """First icon name the current theme provides (symbolic fallbacks last)."""
    if candidates in _icon_cache:
        return _icon_cache[candidates]
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    chosen = candidates[-1]
    if theme:
        for name in candidates:
            if theme.has_icon(name):
                chosen = name
                break
    _icon_cache[candidates] = chosen
    return chosen


def clear_box(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def label(text: str = "", *classes: str, xalign: float = 0.0, wrap: bool = False,
          ellipsize: bool = False, selectable: bool = False, **props) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign, css_classes=list(classes), **props)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    widget.set_selectable(selectable)
    return widget


def chip(text: str, kind: str = "") -> Gtk.Label:
    widget = Gtk.Label(label=text, css_classes=["chip"] + ([kind] if kind else []))
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def button(text: str = "", icon_name: str | None = None, *classes: str, tooltip: str = "",
           on_click=None, action: str | None = None) -> Gtk.Button:
    btn = Gtk.Button(css_classes=list(classes), valign=Gtk.Align.CENTER)
    if text and icon_name:
        btn.set_child(Adw.ButtonContent(label=text, icon_name=icon_name, use_underline=True))
    elif icon_name:
        btn.set_icon_name(icon_name)
    else:
        btn.set_label(text)
        btn.set_use_underline(True)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    if on_click:
        btn.connect("clicked", lambda *_: on_click())
    if action:
        btn.set_action_name(action)
    return btn


def action_row(title: str, subtitle: str = "", icon_name: str | None = None, *,
               selectable: bool = False, on_activate=None, chevron: bool = False) -> Adw.ActionRow:
    row = Adw.ActionRow(title=GLib.markup_escape_text(title), use_markup=True)
    if subtitle:
        row.set_subtitle(GLib.markup_escape_text(subtitle))
    row.set_subtitle_selectable(selectable)
    if icon_name:
        row.add_prefix(Gtk.Image(icon_name=icon_name))
    if on_activate:
        row.set_activatable(True)
        row.connect("activated", lambda *_: on_activate())
    if chevron:
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
    return row


def set_row_text(row: Adw.PreferencesRow, title: str | None = None, subtitle: str | None = None) -> None:
    if title is not None:
        row.set_title(GLib.markup_escape_text(title))
    if subtitle is not None and hasattr(row, "set_subtitle"):
        row.set_subtitle(GLib.markup_escape_text(subtitle))


def state_icon(state: str) -> Gtk.Image:
    names = {
        "ok": ("emblem-ok-symbolic", "object-select-symbolic"),
        "warning": ("dialog-warning-symbolic",),
        "error": ("dialog-error-symbolic",),
        "skip": ("media-skip-forward-symbolic", "go-next-symbolic"),
        "pending": ("content-loading-symbolic", "media-record-symbolic"),
        "info": ("dialog-information-symbolic",),
    }
    css = {"ok": "success", "warning": "warning", "error": "error", "info": "accent"}.get(state, "dim-label")
    return Gtk.Image(icon_name=icon(*names.get(state, names["info"])), css_classes=[css])


def open_path(window: Gtk.Window, path: str | Path, folder: bool = False) -> None:
    gfile = Gio.File.new_for_path(str(path))
    launcher = Gtk.FileLauncher.new(gfile)
    if folder and Path(path).is_file():
        launcher.open_containing_folder(window, None, None)
    else:
        launcher.launch(window, None, None)


class Prefs:
    """Small JSON store for GUI-only preferences."""

    DEFAULTS = {
        "notifications": True,
        "follow_activity": True,
        "editor_glue": True,
        "templates": {},
        "rebuild_editor_after_update": True,
        "rebuild_templates_after_update": False,
    }

    def __init__(self) -> None:
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(PREFS_FILE.read_text()))
        except (OSError, ValueError):
            pass
        self._save_id = 0

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value) -> None:
        self.data[key] = value
        if not self._save_id:
            self._save_id = GLib.timeout_add(400, self._save)

    def _save(self) -> bool:
        self._save_id = 0
        try:
            PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = PREFS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(PREFS_FILE)
        except OSError as exc:
            print(f"warning: could not save preferences: {exc}", file=sys.stderr)
        return False


# ------------------------------------------------------------------------------
#  Backend process: runs godot_build.py --json and streams its events
# ------------------------------------------------------------------------------

class BackendProcess:
    """Async wrapper around one `godot_build.py --json ...` invocation."""

    def __init__(self, args: list[str], on_event=None, on_exit=None, stdin_text: str | None = None) -> None:
        self.args = args
        self.on_event = on_event
        self.on_exit = on_exit
        self.exit_code: int | None = None
        self.reported_code: int | None = None
        self.cancellable = Gio.Cancellable()
        self.interrupted = False

        flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
        if stdin_text is not None:
            flags |= Gio.SubprocessFlags.STDIN_PIPE
        launcher = Gio.SubprocessLauncher.new(flags)
        launcher.setenv("PYTHONUNBUFFERED", "1", True)
        launcher.setenv("NO_COLOR", "1", True)
        launcher.set_cwd(str(BACKEND.parent))
        argv = [sys.executable or "/usr/bin/python3", str(BACKEND), "--json", *args]
        self.proc = launcher.spawnv(argv)
        if stdin_text is not None:
            # Secrets go through a pipe, never argv; the pipe closes right after.
            stdin = self.proc.get_stdin_pipe()
            stdin.write_all((stdin_text + "\n").encode(), None)
            stdin.close(None)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self._read_next()

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def _read_next(self) -> None:
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._on_line)

    def _on_line(self, stream, result) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            self.proc.wait_async(None, self._on_wait)
            return
        line = line.rstrip("\r")
        event = None
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
        if event is None:
            # Something outside the protocol (a traceback, a stray print).
            lowered = line.lower()
            bad = "traceback" in lowered or "error" in lowered
            event = {"type": "raw", "text": line, "error": bad}
        if event.get("type") == "exit":
            self.reported_code = int(event.get("code", 0))
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:  # never let a UI bug kill the stream
                print(f"event handler failed: {exc!r}", file=sys.stderr)
        self._read_next()

    def _on_wait(self, proc, result) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc.get_if_exited():
            code = proc.get_exit_status()
        elif proc.get_if_signaled():
            code = 128 + proc.get_term_sig()
        else:
            code = 1
        if self.reported_code is not None:
            code = self.reported_code
        if self.interrupted and code == 0:
            code = 130
        self.exit_code = code
        if self.on_exit:
            self.on_exit(code)

    def interrupt(self) -> None:
        if self.running:
            self.interrupted = True
            self.proc.send_signal(signal.SIGINT)

    def kill(self) -> None:
        if self.running:
            self.interrupted = True
            self.proc.force_exit()


class Operation:
    """One backend invocation as shown on the Activity page."""

    def __init__(self, title: str, args: list[str], *, icon_name: str = "system-run-symbolic") -> None:
        self.title = title
        self.args = args
        self.icon_name = icon_name


# ------------------------------------------------------------------------------
#  Base page
# ------------------------------------------------------------------------------

class Page(Adw.Bin):
    name = ""
    title = ""

    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        self.win = win
        self.status: dict = {}

    def update(self, status: dict) -> None:
        self.status = status

    def shown(self) -> None:
        """Called whenever the page becomes visible."""

    @staticmethod
    def make_page(*groups: Adw.PreferencesGroup) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        for group in groups:
            page.add(group)
        return page

    def with_bottom_bar(self, content: Gtk.Widget, summary: Gtk.Label, *buttons: Gtk.Widget) -> Gtk.Widget:
        view = Adw.ToolbarView(content=content, bottom_bar_style=Adw.ToolbarStyle.RAISED)
        bar = Gtk.Box(spacing=10, css_classes=["page-bottom-bar"])
        summary.add_css_class("summary")
        summary.set_hexpand(True)
        summary.set_xalign(0)
        summary.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(summary)
        for b in buttons:
            bar.append(b)
        view.add_bottom_bar(bar)
        return view


# ------------------------------------------------------------------------------
#  Overview
# ------------------------------------------------------------------------------

class OverviewPage(Page):
    name = "overview"
    title = "Overview"

    def __init__(self, win):
        super().__init__(win)

        # Hero ---------------------------------------------------------------
        hero = Gtk.Box(spacing=22, css_classes=["hero"])
        self.hero_icon = Gtk.Image(icon_name="godot", pixel_size=96, css_classes=["hero-icon"])
        hero.append(self.hero_icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_title = label("Godot Engine", "hero-title")
        self.hero_subtitle = label("Reading the source tree…", "dim-label", ellipsize=True)
        self.chips = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_top=4)
        text.append(self.hero_title)
        text.append(self.hero_subtitle)
        text.append(self.chips)
        hero.append(text)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        menu = Gio.Menu()
        menu.append("Launch with Encryption Key", "win.launch-with-key")
        menu.append("Open Editor Folder", "win.open-bin")
        self.launch_button = Adw.SplitButton(menu_model=menu, action_name="win.launch",
                                             css_classes=["suggested-action", "pill"],
                                             tooltip_text="Launch Godot (Ctrl+L)")
        self.launch_button.set_child(Adw.ButtonContent(label="Launch Godot",
                                                       icon_name="media-playback-start-symbolic"))
        actions.append(self.launch_button)
        actions.append(button("Check for Updates", "emblem-synchronizing-symbolic", "pill",
                              action="win.check-updates"))
        hero.append(actions)

        hero_group = Adw.PreferencesGroup()
        hero_group.add(hero)

        # Stat cards ---------------------------------------------------------
        self.cards = Gtk.FlowBox(homogeneous=True, column_spacing=12, row_spacing=12,
                                 min_children_per_line=2, max_children_per_line=4,
                                 selection_mode=Gtk.SelectionMode.NONE)
        self.card_widgets = {}
        for key, title, icon_names, target in (
            ("editor", "EDITOR", ("applications-engineering-symbolic",), "editor"),
            ("source", "SOURCE", ("emblem-synchronizing-symbolic",), "source"),
            ("templates", "TEMPLATES", ("package-x-generic-symbolic",), "templates"),
            ("key", "ENCRYPTION", ("channel-secure-symbolic", "security-high-symbolic"), "encryption"),
        ):
            card = Gtk.Button(css_classes=["card", "stat-card"])
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            head = Gtk.Box(spacing=6)
            head.append(label(title, "stat-label", hexpand=True))
            head.append(Gtk.Image(icon_name=icon(*icon_names), css_classes=["stat-icon"]))
            value = label("…", "stat-value", ellipsize=True)
            caption = label("", "stat-caption", ellipsize=True)
            box.append(head)
            box.append(value)
            box.append(caption)
            card.set_child(box)
            card.connect("clicked", lambda _b, t=target: self.win.navigate(t))
            self.cards.append(card)
            self.card_widgets[key] = (value, caption)
        cards_group = Adw.PreferencesGroup()
        cards_group.add(self.cards)

        # Quick actions ------------------------------------------------------
        quick = Adw.PreferencesGroup(title="Quick Actions")
        self.quick_rows = []
        for title, subtitle, icon_names, action in (
            ("Update source and rebuild", "Pull the latest commits, then rebuild the editor",
             ("software-update-available-symbolic", "emblem-synchronizing-symbolic"), "win.update-rebuild"),
            ("Build editor", "Rebuild the Linux editor with the options on the Editor page",
             ("applications-engineering-symbolic",), "win.build-editor"),
            ("Build export templates", "Build every template selected on the Templates page",
             ("package-x-generic-symbolic",), "win.build-templates"),
            ("Run diagnostics", "Check toolchains, SDKs and the desktop integration",
             ("emblem-ok-symbolic",), "win.run-doctor"),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True, action_name=action)
            row.add_prefix(Gtk.Image(icon_name=icon(*icon_names)))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            quick.add(row)

        # Recent builds ------------------------------------------------------
        self.recent = Adw.PreferencesGroup(title="Recent Activity")
        self.recent.set_header_suffix(button("All Logs", None, "flat",
                                             on_click=lambda: self.win.navigate("logs")))
        self.recent_rows: list = []

        self.set_child(self.make_page(hero_group, cards_group, quick, self.recent))

    def update(self, status):
        super().update(status)
        version = status.get("version", {})
        git = status.get("git", {})
        editor = status.get("editor", {})
        key = status.get("key", {})
        launcher = status.get("launcher", {})
        mono = status.get("mono_default", False)

        self.hero_icon.set_from_icon_name(icon("godot-mono", "godot") if mono else icon("godot", "godot-mono"))
        self.hero_title.set_label(f"Godot {version.get('display', '')}")
        sub = f"{git.get('branch', '?')} · {git.get('short', '')}"
        if git.get("subject"):
            sub += f" · {git['subject']}"
        self.hero_subtitle.set_label(sub)
        self.hero_subtitle.set_tooltip_text(sub)

        clear_box(self.chips)
        behind = git.get("behind")
        if behind:
            self.chips.append(chip(f"{behind} new commit{'s' if behind != 1 else ''}", "accent"))
        elif behind == 0:
            self.chips.append(chip("Up to date", "success"))
        self.chips.append(chip(".NET" if mono else "GDScript only", ""))
        if not editor.get("path"):
            self.chips.append(chip("Editor not built", "error"))
        elif editor.get("mono") and not editor.get("godotsharp"):
            self.chips.append(chip("C# glue missing", "warning"))
        if not key.get("exists"):
            self.chips.append(chip("No encryption key", "warning"))
        elif key.get("backed_up"):
            self.chips.append(chip("Key backed up", "success"))
        else:
            self.chips.append(chip("Key not backed up", "warning"))
        if launcher.get("snapshot"):
            self.chips.append(chip(f"Pinned to {launcher['snapshot'].split('_')[-1]}", "accent"))
        if not launcher.get("desktop_entry"):
            self.chips.append(chip("Not in launcher", "warning"))
        if git.get("dirty"):
            self.chips.append(chip("Local changes", "warning"))

        # Cards
        value, caption = self.card_widgets["editor"]
        if editor.get("path"):
            value.set_label(human_size(editor.get("size")))
            caption.set_label(f"Built {human_ago(editor.get('modified'))}")
        else:
            value.set_label("Not built")
            caption.set_label("Build the editor to get started")

        value, caption = self.card_widgets["source"]
        if behind:
            value.set_label(f"{behind} behind")
        elif behind == 0:
            value.set_label("Up to date")
        else:
            value.set_label("Unknown")
        caption.set_label(f"Checked {human_ago(git.get('last_fetch'))}")

        value, caption = self.card_widgets["templates"]
        installed = [t for t in status.get("installed_templates", [])]
        current = version.get("template_dir")
        value.set_label(f"{len(installed)} installed")
        has_current = any(t["name"] == current for t in installed)
        caption.set_label(f"{current} {'installed' if has_current else 'not installed'}")

        value, caption = self.card_widgets["key"]
        if key.get("exists"):
            value.set_label("Ready")
            caption.set_label(f"Fingerprint {key.get('fingerprint', '')}")
        else:
            value.set_label("No key")
            caption.set_label("PCK encryption disabled")

        for row in self.recent_rows:
            self.recent.remove(row)
        self.recent_rows = []
        logs = status.get("logs", [])[:5]
        if not logs:
            row = Adw.ActionRow(title="No builds yet", subtitle="Build logs will appear here")
            row.add_prefix(state_icon("info"))
            self.recent.add(row)
            self.recent_rows.append(row)
        for log in logs:
            code = log.get("exit_code")
            state = "ok" if code == 0 else ("pending" if code is None else ("warning" if code == 130 else "error"))
            row = action_row(log.get("title", log["name"]),
                             f"{human_ago(log.get('modified'))} · {human_size(log.get('size'))}",
                             on_activate=lambda p=log["path"]: self.win.logs.select_path(p), chevron=True)
            row.add_prefix(state_icon(state))
            self.recent.add(row)
            self.recent_rows.append(row)


# ------------------------------------------------------------------------------
#  Source
# ------------------------------------------------------------------------------

class SourcePage(Page):
    name = "source"
    title = "Source"

    def __init__(self, win):
        super().__init__(win)
        self.checking = False

        repo = Adw.PreferencesGroup(title="Repository", description="The Godot source tree this manager builds from.")
        self.row_path = action_row("Location", "", icon("folder-symbolic"), selectable=True)
        self.row_path.add_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Open folder",
                                        on_click=lambda: open_path(self.win, self.status.get("source", "/"))))
        self.row_branch = action_row("Branch", "", icon("media-playlist-consecutive-symbolic", "view-list-symbolic"))
        self.row_commit = action_row("Commit", "", icon("emblem-documents-symbolic", "text-x-generic-symbolic"),
                                     selectable=True)
        self.row_commit.add_suffix(button(None, "edit-copy-symbolic", "flat", tooltip="Copy commit hash",
                                          on_click=self._copy_commit))
        self.row_tree = action_row("Working tree", "", icon("document-edit-symbolic"))
        for row in (self.row_path, self.row_branch, self.row_commit, self.row_tree):
            repo.add(row)

        updates = Adw.PreferencesGroup(title="Updates")
        self.check_button = button("Check Now", "emblem-synchronizing-symbolic", "flat",
                                   action="win.check-updates")
        updates.set_header_suffix(self.check_button)
        self.row_upstream = action_row("Upstream", "")
        self.status_icon = Gtk.Stack()
        self.status_icon.add_named(Adw.Spinner(), "busy")
        self.status_img = Gtk.Image()
        self.status_icon.add_named(self.status_img, "icon")
        self.row_upstream.add_prefix(self.status_icon)
        updates.add(self.row_upstream)

        self.commits = Adw.PreferencesGroup(title="Incoming Changes")
        self.commits.set_visible(False)
        self.commit_rows: list = []
        self.changes: list[dict] = []
        self.commit_count = 0
        filters = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.area_model = Gtk.StringList()
        self.area_keys: list[str | None] = [None]
        self.area_filter = Gtk.DropDown(model=self.area_model, valign=Gtk.Align.CENTER,
                                        tooltip_text="Filter by source area")
        self.area_filter.connect("notify::selected", lambda *_: self._render_changes())
        self.watched_only = Gtk.ToggleButton(icon_name=icon("starred-symbolic", "emblem-favorite-symbolic"),
                                             css_classes=["flat"], valign=Gtk.Align.CENTER,
                                             tooltip_text="Only changes touching your watched areas")
        self.watched_only.connect("toggled", lambda *_: self._render_changes())
        filters.append(self.watched_only)
        filters.append(self.area_filter)
        self.commits.set_header_suffix(filters)

        update = Adw.PreferencesGroup(title="Apply Update",
                                      description="Fast-forwards the source tree. Local changes are never discarded.")
        self.sw_editor = Adw.SwitchRow(title="Rebuild editor afterwards",
                                       subtitle="Uses the options from the Editor page",
                                       active=self.win.prefs["rebuild_editor_after_update"])
        self.sw_editor.connect("notify::active",
                               lambda r, _p: self.win.prefs.__setitem__("rebuild_editor_after_update", r.get_active()))
        self.sw_templates = Adw.SwitchRow(title="Rebuild export templates afterwards",
                                          subtitle="Uses the selection from the Templates page",
                                          active=self.win.prefs["rebuild_templates_after_update"])
        self.sw_templates.connect("notify::active", lambda r, _p: self.win.prefs.__setitem__(
            "rebuild_templates_after_update", r.get_active()))
        update.add(self.sw_editor)
        update.add(self.sw_templates)

        self.summary = Gtk.Label()
        self.update_button = button("Update Source", "software-update-available-symbolic",
                                    "suggested-action", "pill", action="win.update")
        self.set_child(self.with_bottom_bar(self.make_page(repo, updates, self.commits, update),
                                            self.summary, self.update_button))

    def update(self, status):
        super().update(status)
        git = status.get("git", {})
        set_row_text(self.row_path, subtitle=status.get("source", ""))
        set_row_text(self.row_branch, subtitle=git.get("branch") or "—")
        set_row_text(self.row_commit, subtitle=f"{git.get('short', '')} · {git.get('subject', '')}\n"
                                               f"{human_date(git.get('date'))}")
        set_row_text(self.row_tree, subtitle="Local modifications present" if git.get("dirty") else "Clean")
        if not self.checking:
            self._show_upstream_state()
        behind = git.get("behind")
        if behind:
            self.summary.set_label(f"{behind} commit{'s' if behind != 1 else ''} ready to merge")
        elif behind == 0:
            self.summary.set_label(f"Up to date · last checked {human_ago(git.get('last_fetch'))}")
        else:
            self.summary.set_label("No upstream branch configured")

    def _show_upstream_state(self):
        git = self.status.get("git", {})
        behind = git.get("behind")
        upstream = git.get("upstream") or "none"
        if behind:
            title, state = f"{behind} new commit{'s' if behind != 1 else ''} on {upstream}", "info"
        elif behind == 0:
            title, state = f"Up to date with {upstream}", "ok"
        else:
            title, state = "No upstream configured", "warning"
        set_row_text(self.row_upstream, title=title,
                     subtitle=f"Last checked {human_ago(git.get('last_fetch'))}")
        img = state_icon(state)
        self.status_img.set_from_icon_name(img.get_icon_name())
        for css in ("success", "warning", "error", "accent", "dim-label"):
            self.status_img.remove_css_class(css)
        for css in img.get_css_classes():
            self.status_img.add_css_class(css)
        self.status_icon.set_visible_child_name("icon")

    def check(self):
        if self.checking:
            return
        self.checking = True
        self.win.lookup_action("check-updates").set_enabled(False)
        self.status_icon.set_visible_child_name("busy")
        set_row_text(self.row_upstream, title="Checking for updates…", subtitle="Fetching from the remote")
        commits: list = []
        errors: list[str] = []
        total = [0]

        def on_event(ev):
            if ev["type"] == "incoming":
                commits.extend(ev.get("commits", []))
                total[0] = ev.get("commit_count", len(commits))
            elif ev["type"] == "message" and ev.get("level") == "error":
                errors.append(ev.get("text", ""))

        def on_exit(code):
            self.checking = False
            self.win.lookup_action("check-updates").set_enabled(True)
            self.commit_count = total[0]
            self._show_commits(commits)
            if code != 0:
                self.win.toast(errors[0] if errors else "Could not check for updates")
            elif commits:
                watched = sum(1 for c in commits if c.get("watched"))
                extra = f" · {watched} in watched areas" if watched else ""
                self.win.toast(f"{len(commits)} new change{'s' if len(commits) != 1 else ''}{extra}")
            else:
                self.win.toast("Godot source is up to date")
            self.win.refresh()

        BackendProcess(["update", "--check"], on_event, on_exit)

    def _show_commits(self, commits):
        self.changes = commits
        # Area filter: every area touched, most active first.
        counts: dict[str, int] = {}
        labels: dict[str, str] = {}
        for c in commits:
            for a in c.get("areas", []):
                counts[a["key"]] = counts.get(a["key"], 0) + 1
                labels[a["key"]] = a["label"]
        previous = self.area_keys[self.area_filter.get_selected()] if self.area_keys else None
        self.area_keys = [None] + sorted(counts, key=lambda k: (-counts[k], labels[k]))
        self.area_model.splice(0, self.area_model.get_n_items(),
                               ["All areas"] + [f"{labels[k]} ({counts[k]})" for k in self.area_keys[1:]])
        self.area_filter.set_selected(self.area_keys.index(previous) if previous in self.area_keys else 0)
        self._render_changes()

    def _render_changes(self):
        for row in self.commit_rows:
            self.commits.remove(row)
        self.commit_rows = []
        commits = self.changes
        self.commits.set_visible(bool(commits))
        if not commits:
            return
        area = self.area_keys[self.area_filter.get_selected()] if self.area_filter.get_selected() < len(
            self.area_keys) else None
        shown_list = [c for c in commits
                      if (not area or any(a["key"] == area for a in c.get("areas", [])))
                      and (not self.watched_only.get_active() or c.get("watched"))]
        watched = sum(1 for c in commits if c.get("watched"))
        desc = f"{len(commits)} change{'s' if len(commits) != 1 else ''}"
        if self.commit_count and self.commit_count != len(commits):
            desc += f" ({self.commit_count} commits)"
        if watched:
            desc += f" · ★ {watched} touch your watched areas"
        self.commits.set_description(desc)
        shown = shown_list[:80]
        for c in shown:
            ref = f"#{c['pr']}" if c.get("pr") else c.get("short", "")
            row = action_row(c.get("title", ""), f"{ref} · {c.get('author', '')} · {human_ago(c.get('date'))}"
                                                 f" · {c.get('files', 0)} file{'s' if c.get('files') != 1 else ''}")
            row.set_title_lines(2)
            if c.get("watched"):
                star = Gtk.Image(icon_name=icon("starred-symbolic", "emblem-favorite-symbolic"),
                                 css_classes=["accent"], tooltip_text="Touches " + ", ".join(c["watched"]))
                row.add_prefix(star)
            for a in c.get("areas", [])[:2]:
                tag = chip(a["label"], "accent" if c.get("watched") and any(
                    a["key"] == w or a["key"].startswith(w) or w.startswith(a["key"] + "/")
                    for w in c["watched"]) else "")
                tag.set_tooltip_text(f"{a['key']} · {a['files']} file{'s' if a['files'] != 1 else ''}")
                row.add_suffix(tag)
            if c.get("url"):
                row.set_activatable(True)
                row.set_tooltip_text(c["url"])
                row.connect("activated", lambda _r, u=c["url"]: Gtk.UriLauncher.new(u).launch(self.win, None, None))
                row.add_suffix(Gtk.Image(icon_name=icon("adw-external-link-symbolic", "send-to-symbolic",
                                                        "go-next-symbolic"), css_classes=["dim-label"]))
            self.commits.add(row)
            self.commit_rows.append(row)
        if not shown:
            row = action_row("No changes match this filter", "")
            self.commits.add(row)
            self.commit_rows.append(row)
        elif len(shown_list) > len(shown):
            row = action_row(f"…and {len(shown_list) - len(shown)} more", "")
            self.commits.add(row)
            self.commit_rows.append(row)

    def _copy_commit(self):
        commit = self.status.get("git", {}).get("commit")
        if commit:
            self.win.get_clipboard().set(commit)
            self.win.toast("Commit hash copied")

    def update_operations(self) -> list[Operation]:
        ops = [Operation("Update source", ["update"], icon_name="software-update-available-symbolic")]
        if self.sw_editor.get_active():
            ops.append(self.win.editor.operation())
        if self.sw_templates.get_active():
            op = self.win.templates.operation()
            if op:
                ops.append(op)
        return ops


# ------------------------------------------------------------------------------
#  Editor
# ------------------------------------------------------------------------------

class EditorPage(Page):
    name = "editor"
    title = "Editor"

    def __init__(self, win):
        super().__init__(win)
        installed = Adw.PreferencesGroup(title="Installed Build")
        self.row_binary = action_row("Binary", "", icon("application-x-executable-symbolic", "system-run-symbolic"),
                                     selectable=True)
        self.row_binary.add_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Show in folder",
                                          action="win.open-bin"))
        self.row_built = action_row("Built", "", icon("document-open-recent-symbolic", "x-office-calendar-symbolic"))
        self.row_dotnet = action_row(".NET", "", icon("applications-science-symbolic", "emblem-system-symbolic"))
        self.row_launcher = action_row("Launcher", "", icon("utilities-terminal-symbolic"), selectable=True)
        self.fix_launcher = button("Repair", None, "flat", on_click=lambda: self.win.run_integrate(False),
                                   tooltip="Re-link ~/.local/bin/godot and reinstall the desktop entry")
        self.row_launcher.add_suffix(self.fix_launcher)
        for row in (self.row_binary, self.row_built, self.row_dotnet, self.row_launcher):
            installed.add(row)

        options = Adw.PreferencesGroup(title="Build Options",
                                       description="Builds an optimised editor for this machine "
                                                   "(Clang, ThinLTO, tuned for the local CPU).")
        self.sw_mono = Adw.SwitchRow(title=".NET (C#) support", subtitle="Defaults to the _DOTNET setting in custom.py")
        self.sw_glue = Adw.SwitchRow(title="Generate C# glue and assemblies",
                                     subtitle="Required once after every .NET editor build",
                                     active=self.win.prefs["editor_glue"])
        self.sw_glue.connect("notify::active", lambda r, _p: self.win.prefs.__setitem__("editor_glue", r.get_active()))
        self.sw_mono.connect("notify::active", lambda *_: self._sync())
        self.spin_jobs = Adw.SpinRow.new_with_range(1, max(1, os.cpu_count() or 1) * 2, 1)
        self.spin_jobs.set_title("Parallel jobs")
        self.spin_jobs.set_subtitle("Compiler processes run at once")
        self.spin_jobs.set_value(os.cpu_count() or 4)
        self.entry_args = Adw.EntryRow(title="Extra SCons arguments")
        self.entry_args.set_tooltip_text("e.g. debug_symbols=yes verbose=yes")
        for row in (self.sw_mono, self.sw_glue, self.spin_jobs, self.entry_args):
            options.add(row)
        self._mono_initialised = False

        self.summary = Gtk.Label()
        self.build_button = button("Build Editor", "applications-engineering-symbolic",
                                   "suggested-action", "pill", action="win.build-editor")
        self.snapshots = Adw.PreferencesGroup(
            title="Versions",
            description="Every successful build is kept as a snapshot. Switch the launcher to an older one if a "
                        "newer build misbehaves; the next build switches back to the latest.")
        snap_actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.latest_btn = button("Use Latest", None, "flat", tooltip="Launch the newest build again",
                                 on_click=lambda: self._snapshot_cmd(["snapshot", "use", "latest"],
                                                                     "Godot launches the latest build again"))
        snap_actions.append(self.latest_btn)
        snap_actions.append(button(None, "list-add-symbolic", "flat", tooltip="Snapshot the current build now",
                                   on_click=lambda: self._snapshot_cmd(["snapshot", "create"], "Snapshot saved")))
        self.snapshots.set_header_suffix(snap_actions)
        self.snapshot_rows: list = []

        self.set_child(self.with_bottom_bar(self.make_page(installed, self.snapshots, options), self.summary,
                                            self.build_button))

    def _snapshot_cmd(self, args: list[str], success: str):
        errors: list[str] = []

        def on_event(ev):
            if ev["type"] == "message" and ev.get("level") == "error":
                errors.append(ev.get("text", ""))

        def on_exit(code):
            self.win.toast(success if code == 0 else (errors[0] if errors else "Snapshot command failed"))
            self.win.refresh()

        BackendProcess(args, on_event, on_exit)

    def _confirm_delete_snapshot(self, snap: dict):
        dialog = Adw.AlertDialog(heading="Delete Snapshot?",
                                 body=f"{snap['short']} · {snap.get('subject', '')}\nFrees "
                                      f"{human_size(snap.get('size'))}.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("delete", "_Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "delete" and self._snapshot_cmd(
            ["snapshot", "delete", snap["id"]], "Snapshot deleted"))
        dialog.present(self.win)

    def _render_snapshots(self, status: dict):
        for row in self.snapshot_rows:
            self.snapshots.remove(row)
        self.snapshot_rows = []
        snaps = status.get("snapshots", [])
        pinned = status.get("launcher", {}).get("snapshot")
        self.latest_btn.set_visible(bool(pinned))
        if not snaps:
            row = Adw.ActionRow(title="No snapshots yet",
                                subtitle="One is saved automatically after the next successful build")
            row.add_prefix(state_icon("info"))
            self.snapshots.add(row)
            self.snapshot_rows.append(row)
            return
        current_commit = status.get("git", {}).get("commit", "")
        for i, snap in enumerate(snaps):
            bits = [human_ago(snap.get("created")), human_size(snap.get("size"))]
            if snap.get("mono"):
                bits.append(".NET" if snap.get("godotsharp") else ".NET (no assemblies)")
            row = action_row(f"{snap['short']} · {snap.get('subject', '')}", " · ".join(bits),
                             icon("media-optical-symbolic", "drive-harddisk-symbolic", "folder-symbolic"))
            row.set_title_lines(1)
            row.set_tooltip_text(f"{snap['id']}\n{snap.get('version', '')} · built {human_date(snap.get('created'))}")
            if snap.get("pinned"):
                row.add_suffix(chip("in use", "accent"))
            elif i == 0 and not pinned:
                row.add_suffix(chip("latest", "success"))
            if snap.get("commit") == current_commit and i:
                row.add_suffix(chip("same commit"))
            if not snap.get("pinned"):
                row.add_suffix(button("Use", None, "flat",
                                      tooltip="Make the launcher open this version",
                                      on_click=lambda s=snap: self._snapshot_cmd(
                                          ["snapshot", "use", s["id"]], f"Godot now launches {s['short']}")))
                row.add_suffix(button(None, "user-trash-symbolic", "flat", tooltip="Delete snapshot",
                                      on_click=lambda s=snap: self._confirm_delete_snapshot(s)))
            self.snapshots.add(row)
            self.snapshot_rows.append(row)

    def _sync(self):
        self.sw_glue.set_sensitive(self.sw_mono.get_active())
        variant = ".NET" if self.sw_mono.get_active() else "GDScript-only"
        arch = self.status.get("host_arch", "")
        self.summary.set_label(f"Linux {arch} · {variant} editor · {int(self.spin_jobs.get_value())} jobs")

    def update(self, status):
        super().update(status)
        editor = status.get("editor", {})
        launcher = status.get("launcher", {})
        if not self._mono_initialised:
            self.sw_mono.set_active(status.get("mono_default", False))
            self._mono_initialised = True
        if editor.get("path"):
            set_row_text(self.row_binary, subtitle=f"{editor['name']}  ·  {human_size(editor.get('size'))}")
            set_row_text(self.row_built, subtitle=f"{human_date(editor.get('modified'))} "
                                                  f"({human_ago(editor.get('modified'))})")
        else:
            set_row_text(self.row_binary, subtitle="No editor binary in bin/ yet")
            set_row_text(self.row_built, subtitle="—")
        if editor.get("mono"):
            ready = editor.get("godotsharp")
            set_row_text(self.row_dotnet, subtitle="Enabled · GodotSharp assemblies present" if ready else
                         "Enabled · GodotSharp missing — build the editor with glue to finish setup")
        else:
            set_row_text(self.row_dotnet, subtitle="Not included in this build")
        if launcher.get("snapshot"):
            set_row_text(self.row_launcher, subtitle=f"{launcher['path']} → snapshot {launcher['snapshot']}")
        elif launcher.get("current"):
            set_row_text(self.row_launcher, subtitle=f"{launcher['path']} → latest build")
        elif launcher.get("is_symlink"):
            set_row_text(self.row_launcher, subtitle=f"{launcher['path']} points at an older binary")
        else:
            set_row_text(self.row_launcher, subtitle=f"{launcher.get('path', '~/.local/bin/godot')} not installed")
        self.fix_launcher.set_visible(not ((launcher.get("current") or launcher.get("snapshot"))
                                           and launcher.get("desktop_entry")))
        self._render_snapshots(status)
        self._sync()

    def operation(self) -> Operation:
        args = ["editor", "--mono" if self.sw_mono.get_active() else "--no-mono",
                "-j", str(int(self.spin_jobs.get_value()))]
        if self.sw_mono.get_active() and not self.sw_glue.get_active():
            args.append("--no-glue")
        extra = self.entry_args.get_text().strip()
        if extra:
            args += ["--", *shlex.split(extra)]
        return Operation("Build editor", args, icon_name="applications-engineering-symbolic")


# ------------------------------------------------------------------------------
#  Export templates
# ------------------------------------------------------------------------------

PLATFORM_ICONS = {
    "linux": ("distributor-logo-linux", "penguin", "computer-symbolic"),
    "windows": ("distributor-logo-windows", "windows", "video-display-symbolic"),
    "macos": ("distributor-logo-mac", "apple", "laptop-symbolic", "computer-apple-symbolic", "computer-symbolic"),
}


class TemplatesPage(Page):
    name = "templates"
    title = "Export Templates"

    def __init__(self, win):
        super().__init__(win)
        saved = self.win.prefs["templates"] or {}
        self.saved = saved
        self.platform_rows: dict[str, Adw.ExpanderRow] = {}
        self.arch_rows: dict[tuple[str, str], Adw.SwitchRow] = {}
        self.platforms_group = Adw.PreferencesGroup(
            title="Platforms", description="Linux and Windows build here directly; macOS needs osxcross.")
        self.setup_toolchains = button("Set Up Toolchains…", None, "flat",
                                       tooltip="Install the Windows and macOS cross-compilers",
                                       on_click=lambda: self.win.navigate("diagnostics"))
        self.setup_toolchains.set_visible(False)
        self.platforms_group.set_header_suffix(self.setup_toolchains)

        targets = Adw.PreferencesGroup(title="Targets")
        self.sw_release = Adw.SwitchRow(title="Release", subtitle="Shipped builds · fully optimised",
                                        active=saved.get("release", True))
        self.sw_debug = Adw.SwitchRow(title="Debug", subtitle="Exports with debugging enabled",
                                      active=saved.get("debug", True))
        targets.add(self.sw_release)
        targets.add(self.sw_debug)

        options = Adw.PreferencesGroup(title="Options")
        self.sw_encrypt = Adw.SwitchRow(title="Encrypt PCK", active=saved.get("encrypt", True))
        self.sw_require = Adw.SwitchRow(title="Require encryption",
                                        subtitle="Fail instead of building unencrypted when the key is missing",
                                        active=saved.get("require", False))
        self.sw_mono = Adw.SwitchRow(title=".NET (C#) templates",
                                     subtitle="Defaults to the _DOTNET setting in custom.py")
        self.sw_install = Adw.SwitchRow(title="Install into the editor",
                                        subtitle="Copies templates to ~/.local/share/godot/export_templates",
                                        active=saved.get("install", True))
        self.sw_tpz = Adw.SwitchRow(title="Package .tpz archive", subtitle="Installable from Godot's template manager",
                                    active=saved.get("tpz", False))
        self.sw_keep = Adw.SwitchRow(title="Keep going after failures",
                                     subtitle="Build the remaining templates if one fails",
                                     active=saved.get("keep_going", True))
        self.spin_jobs = Adw.SpinRow.new_with_range(1, max(1, os.cpu_count() or 1) * 2, 1)
        self.spin_jobs.set_title("Parallel jobs")
        self.spin_jobs.set_value(os.cpu_count() or 4)
        self.entry_args = Adw.EntryRow(title="Extra SCons arguments")
        for row in (self.sw_encrypt, self.sw_require, self.sw_mono, self.sw_install, self.sw_tpz, self.sw_keep,
                    self.spin_jobs, self.entry_args):
            options.add(row)
        self._mono_initialised = False

        self.staged = Adw.PreferencesGroup(title="Staged Templates",
                                           description="Built templates waiting in the Dist folder.")
        self.staged.set_header_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Open Dist folder",
                                             on_click=lambda: self._open("dist")))
        self.installed = Adw.PreferencesGroup(title="Installed Templates")
        self.installed.set_header_suffix(button(None, "folder-open-symbolic", "flat",
                                                tooltip="Open export templates folder",
                                                on_click=lambda: self._open("templates")))
        self.dynamic_rows: list = []

        for sw in (self.sw_release, self.sw_debug, self.sw_encrypt, self.sw_mono, self.sw_require):
            sw.connect("notify::active", lambda *_: self._sync())
        self.spin_jobs.connect("notify::value", lambda *_: self._sync())

        self.summary = Gtk.Label()
        self.build_button = button("Build Templates", "package-x-generic-symbolic",
                                   "suggested-action", "pill", action="win.build-templates")
        self.page = self.make_page(self.platforms_group, targets, options, self.staged, self.installed)
        self.set_child(self.with_bottom_bar(self.page, self.summary, self.build_button))

    def _open(self, which):
        path = self.status.get("paths", {}).get(which)
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            open_path(self.win, path)

    def _build_platform_rows(self, platforms: dict):
        saved_platforms = self.saved.get("platforms", {})
        for key, info in platforms.items():
            row = Adw.ExpanderRow(title=info["label"], show_enable_switch=True)
            row.add_prefix(Gtk.Image(icon_name=icon(*PLATFORM_ICONS.get(key, ("computer-symbolic",))),
                                     pixel_size=24))
            chosen = saved_platforms.get(key, {})
            for arch in info["supported_arches"]:
                default = arch in info["default_arches"]
                sw = Adw.SwitchRow(title=arch, active=chosen.get(arch, default))
                if key == "macos":
                    sw.set_subtitle("Combined into one universal binary with lipo")
                sw.connect("notify::active", lambda *_: self._sync())
                row.add_row(sw)
                self.arch_rows[(key, arch)] = sw
            row.set_enable_expansion(chosen.get("enabled", key != "macos"))
            row.connect("notify::enable-expansion", lambda *_: self._sync())
            self.platforms_group.add(row)
            self.platform_rows[key] = row

    def update(self, status):
        super().update(status)
        platforms = status.get("platforms", {})
        if not self.platform_rows and platforms:
            self._build_platform_rows(platforms)
        for key, row in self.platform_rows.items():
            info = platforms.get(key, {})
            available = info.get("available", False)
            row.set_sensitive(available)
            if available:
                row.set_subtitle("")
            else:
                row.set_enable_expansion(False)
                row.set_subtitle(GLib.markup_escape_text(info.get("reason") or "Unavailable"))
        self.setup_toolchains.set_visible(any(not p.get("available", False) for p in platforms.values()))
        if not self._mono_initialised:
            self.sw_mono.set_active(self.saved.get("mono", status.get("mono_default", False)))
            self._mono_initialised = True

        key = status.get("key", {})
        self.sw_encrypt.set_sensitive(bool(key.get("exists")))
        if key.get("exists"):
            self.sw_encrypt.set_subtitle(f"Embed the AES-256 key · fingerprint {key.get('fingerprint', '')}")
        else:
            self.sw_encrypt.set_subtitle("No key yet — generate one on the Encryption page")

        # Staged + installed lists
        for group, row in self.dynamic_rows:
            group.remove(row)
        self.dynamic_rows = []
        staged = status.get("staged", {})
        if not staged:
            self._add_dynamic(self.staged, Adw.ActionRow(title="Nothing staged", subtitle="Build templates first"))
        for name, info in staged.items():
            files = info.get("files", {})
            encrypted = sum(1 for f in files.values() if isinstance(f, dict) and f.get("encrypted"))
            row = action_row(name, f"{len(files)} file{'s' if len(files) != 1 else ''}"
                                   f"{f' · {encrypted} encrypted' if encrypted else ''}",
                             icon("folder-symbolic"))
            row.add_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Open folder",
                                  on_click=lambda p=info["dir"]: open_path(self.win, p)))
            mono = info.get("mono", False)
            row.add_suffix(button("Install", None, "flat", tooltip="Install into the editor's template folder",
                                  on_click=lambda m=mono, n=name: self.win.run_heavy([Operation(
                                      f"Install {n}", ["install", "--mono" if m else "--no-mono"],
                                      icon_name="document-save-symbolic")])))
            self._add_dynamic(self.staged, row)

        installed = status.get("installed_templates", [])
        if not installed:
            self._add_dynamic(self.installed, Adw.ActionRow(title="No templates installed"))
        current = status.get("version", {}).get("template_dir")
        for t in installed:
            row = Adw.ExpanderRow(title=GLib.markup_escape_text(t["name"]),
                                  subtitle=f"{len(t['files'])} files")
            row.add_prefix(Gtk.Image(icon_name=icon("package-x-generic-symbolic")))
            if t["name"] == current:
                row.add_suffix(chip("current", "accent"))
            for f in t["files"]:
                row.add_row(Adw.ActionRow(title=GLib.markup_escape_text(f), css_classes=["property"]))
            self._add_dynamic(self.installed, row)
        self._sync()

    def _add_dynamic(self, group, row):
        group.add(row)
        self.dynamic_rows.append((group, row))

    def selection(self):
        platforms, arches = [], []
        for key, row in self.platform_rows.items():
            if not (row.get_sensitive() and row.get_enable_expansion()):
                continue
            chosen = [a for (p, a), sw in self.arch_rows.items() if p == key and sw.get_active()]
            if chosen:
                platforms.append(key)
                arches += [f"{key}:{a}" for a in chosen]
        targets = [t for t, sw in (("release", self.sw_release), ("debug", self.sw_debug)) if sw.get_active()]
        return platforms, arches, targets

    def _sync(self):
        for key, row in self.platform_rows.items():
            if row.get_sensitive():
                chosen = [a for (p, a), sw in self.arch_rows.items() if p == key and sw.get_active()]
                row.set_subtitle(", ".join(chosen) if chosen else "No architecture selected")
        platforms, arches, targets = self.selection()
        builds = 0
        for key in platforms:
            n = sum(1 for a in arches if a.startswith(key + ":"))
            # macOS arches merge into one universal template but build separately.
            builds += n * len(targets)
        labels = [self.status.get("platforms", {}).get(p, {}).get("label", p) for p in platforms]
        encrypt = self.sw_encrypt.get_active() and self.sw_encrypt.get_sensitive()
        if builds:
            self.summary.set_label(f"{builds} build{'s' if builds != 1 else ''} · {', '.join(labels)} · "
                                   f"{' + '.join(targets)}{' · encrypted' if encrypt else ''}")
        else:
            self.summary.set_label("Select at least one platform, architecture and target")
        self.win.set_action_enabled("build-templates", bool(builds) and not self.win.busy)

    def operation(self) -> Operation | None:
        platforms, arches, targets = self.selection()
        if not (platforms and targets):
            return None
        self.saved = {
            "release": self.sw_release.get_active(), "debug": self.sw_debug.get_active(),
            "encrypt": self.sw_encrypt.get_active(), "require": self.sw_require.get_active(),
            "mono": self.sw_mono.get_active(), "install": self.sw_install.get_active(),
            "tpz": self.sw_tpz.get_active(), "keep_going": self.sw_keep.get_active(),
            "platforms": {k: {"enabled": r.get_enable_expansion(),
                              **{a: sw.get_active() for (p, a), sw in self.arch_rows.items() if p == k}}
                          for k, r in self.platform_rows.items()},
        }
        self.win.prefs["templates"] = self.saved
        args = ["templates", "-p", ",".join(platforms), "--arch", ",".join(arches), "-t", ",".join(targets),
                "--mono" if self.sw_mono.get_active() else "--no-mono", "-j", str(int(self.spin_jobs.get_value()))]
        if not (self.sw_encrypt.get_active() and self.sw_encrypt.get_sensitive()):
            args.append("--no-encrypt")
        if self.sw_require.get_active():
            args.append("--require-encryption")
        if self.sw_install.get_active():
            args.append("--install")
        if self.sw_tpz.get_active():
            args.append("--tpz")
        if self.sw_keep.get_active():
            args.append("--keep-going")
        extra = self.entry_args.get_text().strip()
        if extra:
            args += ["--", *shlex.split(extra)]
        return Operation("Build export templates", args, icon_name="package-x-generic-symbolic")


# ------------------------------------------------------------------------------
#  Encryption
# ------------------------------------------------------------------------------

class EncryptionPage(Page):
    name = "encryption"
    title = "Encryption"

    def __init__(self, win):
        super().__init__(win)
        self.revealed = False
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)

        # No key yet
        gen = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        gen.append(button("Generate Key", "channel-secure-symbolic", "suggested-action", "pill",
                          on_click=self.generate))
        gen.append(button("Restore Backup…", "document-open-symbolic", "pill",
                          on_click=lambda: self._pick_backup("restore")))
        empty = Adw.StatusPage(icon_name=icon("channel-secure-symbolic", "security-high-symbolic"),
                               title="No Encryption Key",
                               description="Generate a 256-bit AES key to encrypt your game's PCK files.\n"
                                           "It is compiled into every export template you build.",
                               child=gen)
        self.stack.add_named(empty, "empty")

        # Key present
        key_group = Adw.PreferencesGroup(title="Key")
        self.row_fp = action_row("Fingerprint", "", icon("fingerprint-symbolic", "auth-fingerprint-symbolic",
                                                         "dialog-password-symbolic"), selectable=True)
        self.row_key = Adw.ActionRow(title="Key")
        self.row_key.add_prefix(Gtk.Image(icon_name=icon("dialog-password-symbolic", "channel-secure-symbolic")))
        self.key_label = label("", "key-text", ellipsize=True, selectable=True)
        self.key_label.set_hexpand(True)
        self.row_key.add_suffix(self.key_label)
        self.reveal_btn = Gtk.ToggleButton(icon_name="view-reveal-symbolic", valign=Gtk.Align.CENTER,
                                           css_classes=["flat"], tooltip_text="Show key")
        self.reveal_btn.connect("toggled", lambda b: self._set_revealed(b.get_active()))
        self.row_key.add_suffix(self.reveal_btn)
        self.row_key.add_suffix(button(None, "edit-copy-symbolic", "flat",
                                       tooltip=f"Copy key (cleared from the clipboard after "
                                               f"{CLIPBOARD_CLEAR_SECONDS}s)",
                                       on_click=self.copy_key))
        self.row_file = action_row("Location", "", icon("folder-symbolic"), selectable=True)
        self.row_file.add_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Show in folder",
                                        on_click=lambda: open_path(self.win, self.key_path(), folder=True)))
        self.row_perm = action_row("Permissions", "", icon("system-lock-screen-symbolic", "changes-prevent-symbolic"))
        self.fix_perm = button("Fix", None, "flat", tooltip="Restrict the key file to your user (0600)",
                               on_click=self._fix_permissions)
        self.row_perm.add_suffix(self.fix_perm)
        for row in (self.row_fp, self.row_key, self.row_file, self.row_perm):
            key_group.add(row)

        usage = Adw.PreferencesGroup(title="Use in the Editor",
                                     description="The editor needs the same key to export encrypted PCKs. "
                                                 "Either paste it into each export preset or provide it "
                                                 "through GODOT_SCRIPT_ENCRYPTION_KEY.")
        launch = action_row("Launch Godot with the key", "Starts the editor with the key in its environment",
                            icon("media-playback-start-symbolic"))
        launch.set_activatable(True)
        launch.set_action_name("win.launch-with-key")
        launch.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        usage.add(launch)
        for shell in ("fish", "bash"):
            row = action_row(f"Copy {shell} export line",
                             "set -gx GODOT_SCRIPT_ENCRYPTION_KEY …" if shell == "fish"
                             else "export GODOT_SCRIPT_ENCRYPTION_KEY=…",
                             icon("utilities-terminal-symbolic"), on_activate=lambda s=shell: self.copy_env(s))
            row.add_suffix(Gtk.Image(icon_name="edit-copy-symbolic"))
            usage.add(row)

        verify = Adw.PreferencesGroup(title="Verify",
                                      description="Confirms the key is actually compiled into each staged "
                                                  "template binary.")
        self.verify_button = button("Verify Templates", None, "flat", on_click=self.verify)
        verify.set_header_suffix(self.verify_button)
        self.verify_group = verify
        self.verify_rows: list = []

        steps = Adw.PreferencesGroup(title="How It Works")
        for n, (title, text) in enumerate((
            ("Build templates with the key", "Templates built here embed the key automatically."),
            ("Enable encryption in the export preset",
             "Export → Encryption: tick “Encrypt Exported PCK” and add include filters such as *.gd, *.tscn, *.tres."),
            ("Give the editor the key", "Paste it into the preset, or launch Godot with the key from this page."),
            ("Back it up", "Losing the key means you cannot rebuild matching templates. Never commit it."),
        ), 1):
            row = Adw.ActionRow(title=title, subtitle=text)
            row.set_subtitle_lines(3)
            row.add_prefix(label(str(n), "step-number", xalign=0.5))
            steps.add(row)

        danger = Adw.PreferencesGroup(title="Danger Zone")
        replace = Adw.ActionRow(title="Replace key",
                                subtitle="Backs up the current key and clears templates built with it. "
                                         "Existing exported games keep working with the old key only.")
        replace.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
        replace.add_suffix(button("Replace…", None, "destructive-action", on_click=self.confirm_replace))
        danger.add(replace)

        backup = Adw.PreferencesGroup(
            title="Backup",
            description="A passphrase-protected copy of the key (GnuPG, AES-256). Keep it off this disk — "
                        "without the key you can never build templates that open your shipped games' PCKs.")
        self.row_backup = Adw.ActionRow(title="Backup status")
        self.backup_icon = Gtk.Image()
        self.row_backup.add_prefix(self.backup_icon)
        self.row_backup.set_subtitle_lines(3)
        backup.add(self.row_backup)
        actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        actions.append(button("Back Up…", "document-save-symbolic", "flat", on_click=self.backup))
        actions.append(button("Verify…", None, "flat", tooltip="Decrypt a backup and check it matches this key",
                              on_click=lambda: self._pick_backup("check-backup")))
        backup.set_header_suffix(actions)

        self.stack.add_named(self.make_page(key_group, backup, usage, verify, steps, danger), "key")
        self.set_child(self.stack)

    def key_path(self) -> Path:
        return Path(self.status.get("key", {}).get("path", ""))

    def read_key(self) -> str | None:
        try:
            key = self.key_path().read_text().strip()
        except OSError:
            return None
        return key if len(key) == 64 else None

    def update(self, status):
        super().update(status)
        key = status.get("key", {})
        self.stack.set_visible_child_name("key" if key.get("exists") else "empty")
        if not key.get("exists"):
            return
        set_row_text(self.row_fp, subtitle=key.get("fingerprint", "—") if key.get("valid", True)
                     else "Key file is invalid (expected 64 hex characters)")
        set_row_text(self.row_file, subtitle=key.get("path", ""))
        mode = key.get("mode", "")
        secure = mode in ("0o600", "0o400")
        set_row_text(self.row_perm, subtitle=f"{mode.replace('0o', '')} · only you can read it" if secure
                     else f"{mode.replace('0o', '')} · readable by other users")
        self.fix_perm.set_visible(not secure)
        self._set_revealed(self.reveal_btn.get_active())
        backups = key.get("backups", [])
        if backups:
            newest = max(backups, key=lambda b: b.get("created", ""))
            set_row_text(self.row_backup, title=f"Backed up · {len(backups)} cop{'ies' if len(backups) != 1 else 'y'}",
                         subtitle=f"{newest['path']}\nlast verified {human_ago(newest.get('created'))}")
            self.backup_icon.set_from_icon_name(icon("emblem-ok-symbolic"))
            self.backup_icon.set_css_classes(["success"])
        else:
            set_row_text(self.row_backup, title="No backup yet",
                         subtitle="Back the key up now and store the file somewhere safe.")
            self.backup_icon.set_from_icon_name(icon("dialog-warning-symbolic"))
            self.backup_icon.set_css_classes(["warning"])

    def _set_revealed(self, revealed: bool):
        key = self.read_key() or ""
        self.reveal_btn.set_icon_name("view-conceal-symbolic" if revealed else "view-reveal-symbolic")
        self.reveal_btn.set_tooltip_text("Hide key" if revealed else "Show key")
        if revealed:
            self.key_label.set_label(key)
        else:
            self.key_label.set_label(f"{key[:4]}{'•' * 20}{key[-4:]}" if key else "")

    def _fix_permissions(self):
        try:
            self.key_path().chmod(0o600)
            self.win.toast("Key file restricted to your user")
        except OSError as exc:
            self.win.toast(f"Could not change permissions: {exc.strerror}")
        self.win.refresh()

    def _copy_secret(self, text: str, what: str):
        clipboard = self.win.get_clipboard()
        provider = Gdk.ContentProvider.new_for_value(text)
        clipboard.set_content(provider)

        def clear():
            if clipboard.get_content() is provider:
                clipboard.set_content(None)
            return False

        GLib.timeout_add_seconds(CLIPBOARD_CLEAR_SECONDS, clear)
        self.win.toast(f"{what} copied · clipboard clears in {CLIPBOARD_CLEAR_SECONDS}s")

    def copy_key(self):
        key = self.read_key()
        if key:
            self._copy_secret(key, "Key")

    def copy_env(self, shell: str):
        key = self.read_key()
        if not key:
            return
        line = (f"set -gx GODOT_SCRIPT_ENCRYPTION_KEY {key}" if shell == "fish"
                else f"export GODOT_SCRIPT_ENCRYPTION_KEY={key}")
        self._copy_secret(line, f"{shell} export line")

    def generate(self, force: bool = False):
        args = ["key", "generate"] + (["--force"] if force else [])
        messages: list[str] = []

        def on_event(ev):
            if ev["type"] == "message" and ev.get("level") == "error":
                messages.append(ev.get("text", ""))

        def on_exit(code):
            if code == 0:
                self.win.toast("New encryption key generated · rebuild your templates")
            else:
                self.win.toast(messages[0] if messages else "Key generation failed")
            self.reveal_btn.set_active(False)
            self.win.refresh()

        BackendProcess(args, on_event, on_exit)

    # --- backup / restore -----------------------------------------------------

    def _passphrase_dialog(self, heading: str, body: str, confirm_label: str, *, new: bool, on_ok):
        """Ask for a passphrase (twice for new backups). Calls on_ok(passphrase)."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        group = Adw.PreferencesGroup()
        first = Adw.PasswordEntryRow(title="Passphrase", activates_default=True)
        group.add(first)
        second = Adw.PasswordEntryRow(title="Repeat passphrase", activates_default=True) if new else None
        if second:
            group.add(second)
        hint = label("", "caption", "dim-label", wrap=True)
        hint.set_margin_top(8)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(hint)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("ok", confirm_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def validate(*_):
            phrase = first.get_text()
            message, good = "", bool(phrase)
            if new:
                if len(phrase) < 8:
                    message, good = "Use at least 8 characters — a few random words work well.", False
                elif second.get_text() != phrase:
                    message, good = "The passphrases don't match yet.", False
                else:
                    message = "Write the passphrase down: it cannot be recovered."
            hint.set_label(message)
            dialog.set_response_enabled("ok", good)

        first.connect("changed", validate)
        if second:
            second.connect("changed", validate)
        validate()
        dialog.connect("response", lambda _d, r: r == "ok" and on_ok(first.get_text()))
        dialog.present(self.win)
        first.grab_focus()

    def _run_key_cmd(self, args: list[str], passphrase: str, success: str):
        errors: list[str] = []

        def on_event(ev):
            if ev["type"] == "message" and ev.get("level") in ("error", "warning"):
                errors.append(ev.get("text", ""))

        def on_exit(code):
            self.win.toast(success if code == 0 else (errors[0] if errors else "Key operation failed"), 5)
            self.win.refresh()

        BackendProcess([*args, "--passphrase-stdin"], on_event, on_exit, stdin_text=passphrase)

    def backup(self):
        fp = self.status.get("key", {}).get("fingerprint", "key")
        dialog = Gtk.FileDialog(title="Save Key Backup", initial_name=f"godot-pck-key-{fp}.asc",
                                initial_folder=Gio.File.new_for_path(str(Path.home())))

        def chosen(dlg, result):
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return  # cancelled
            path = gfile.get_path()
            self._passphrase_dialog(
                "Protect the Backup", "Choose a passphrase for this backup. You will need it to restore the key.",
                "_Back Up", new=True,
                on_ok=lambda phrase: self._run_key_cmd(["key", "backup", path], phrase,
                                                       f"Key backed up to {Path(path).name}"))

        dialog.save(self.win, None, chosen)

    def _pick_backup(self, action: str):
        filters = Gio.ListStore.new(Gtk.FileFilter)
        flt = Gtk.FileFilter(name="Key backups")
        for pattern in ("*.asc", "*.gpg", "*.pgp"):
            flt.add_pattern(pattern)
        filters.append(flt)
        dialog = Gtk.FileDialog(title="Open Key Backup", filters=filters,
                                initial_folder=Gio.File.new_for_path(str(Path.home())))

        def chosen(dlg, result):
            try:
                path = dlg.open_finish(result).get_path()
            except GLib.Error:
                return
            if action == "check-backup":
                self._passphrase_dialog("Verify Backup", f"Enter the passphrase for {Path(path).name}.", "_Verify",
                                        new=False, on_ok=lambda phrase: self._run_key_cmd(
                                            ["key", "check-backup", path], phrase, "Backup verified: it matches this key"))
                return
            force = bool(self.status.get("key", {}).get("exists"))
            body = f"Enter the passphrase for {Path(path).name}."
            if force:
                body += " A different current key is moved aside (kept as a .bak file)."
            self._passphrase_dialog("Restore Key", body, "_Restore", new=False,
                                    on_ok=lambda phrase: self._run_key_cmd(
                                        ["key", "restore", path, *(["--force"] if force else [])], phrase,
                                        "Encryption key restored"))

        dialog.open(self.win, None, chosen)

    def confirm_replace(self):
        dialog = Adw.AlertDialog(
            heading="Replace Encryption Key?",
            body="The current key is backed up next to the new one, and staged templates built with it are "
                 "deleted. Games already exported with the old key can only be patched with templates "
                 "that use the old key.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("replace", "_Replace Key")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "replace" and self.generate(force=True))
        dialog.present(self.win)

    def verify(self):
        for row in self.verify_rows:
            self.verify_group.remove(row)
        self.verify_rows = []
        self.verify_button.set_sensitive(False)
        results: list[dict] = []
        messages: list[dict] = []

        def on_event(ev):
            if ev["type"] == "verify":
                results.append(ev)
            elif ev["type"] == "message" and ev.get("level") in ("error", "warning"):
                messages.append(ev)

        def on_exit(code):
            self.verify_button.set_sensitive(True)
            for r in results:
                row = action_row(r["name"], "Key embedded" if r["ok"] else "Key NOT found in this binary")
                row.add_prefix(state_icon("ok" if r["ok"] else "error"))
                self.verify_group.add(row)
                self.verify_rows.append(row)
            if not results:
                text = messages[0]["text"] if messages else "No staged templates to verify"
                row = action_row(text, "")
                row.add_prefix(state_icon("warning" if code else "info"))
                self.verify_group.add(row)
                self.verify_rows.append(row)
            good = sum(r["ok"] for r in results)
            if results:
                self.win.toast(f"{good}/{len(results)} templates contain the key")

        BackendProcess(["key", "verify"], on_event, on_exit)


# ------------------------------------------------------------------------------
#  Activity
# ------------------------------------------------------------------------------

class JobRow(Adw.ActionRow):
    def __init__(self, job_id: str, title: str, subtitle: str = ""):
        super().__init__(title=GLib.markup_escape_text(title))
        if subtitle:
            self.set_subtitle(GLib.markup_escape_text(subtitle))
        self.job_id = job_id
        self.state = "pending"
        self.started: float | None = None
        self.seconds: float | None = None
        self.fraction = 0.0

        self.icon_stack = Gtk.Stack()
        self.spinner = Adw.Spinner()
        self.image = Gtk.Image(icon_name=icon("content-loading-symbolic", "media-record-symbolic"),
                               css_classes=["dim-label"])
        self.icon_stack.add_named(self.spinner, "busy")
        self.icon_stack.add_named(self.image, "icon")
        self.icon_stack.set_visible_child_name("icon")
        self.add_prefix(self.icon_stack)

        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, css_classes=["job-progress"], visible=False)
        self.time_label = label("Queued", "job-time", xalign=1)
        self.add_suffix(self.progress)
        self.add_suffix(self.time_label)
        self.log_path: str | None = None
        self.expected_seconds: float | None = None  # median of earlier successful runs
        self.step_based = False  # progress comes from SCons step counts

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0

    def remaining(self) -> float | None:
        """Seconds left based on earlier runs, None when this step has no history."""
        if not self.expected_seconds:
            return None
        if self.state == "pending":
            return self.expected_seconds
        if self.state == "running":
            return max(0.0, self.expected_seconds - self.elapsed())
        return 0.0

    def set_state(self, state: str, detail: str = ""):
        self.state = state
        if state == "running":
            self.icon_stack.set_visible_child_name("busy")
            self.progress.set_visible(True)
            self.started = self.started or time.monotonic()
        else:
            img = state_icon({"ok": "ok", "failed": "error", "interrupted": "warning",
                              "skipped": "skip"}.get(state, "pending"))
            self.image.set_from_icon_name(img.get_icon_name())
            self.image.set_css_classes(img.get_css_classes())
            self.icon_stack.set_visible_child_name("icon")
            if state in ("ok", "failed", "interrupted", "skipped"):
                self.progress.set_visible(False)
        if detail:
            self.time_label.set_label(detail)

    def tick(self):
        if self.state == "running" and self.started:
            elapsed = self.elapsed()
            text = human_duration(elapsed)
            if self.expected_seconds:
                left = self.expected_seconds - elapsed
                text += f" · {human_eta(left)} left" if left > 0 else " · longer than usual"
                if not self.step_based:
                    self.fraction = min(0.99, elapsed / self.expected_seconds)
                    self.progress.set_fraction(self.fraction)
            self.time_label.set_label(text)
        elif self.state == "pending" and self.expected_seconds:
            self.time_label.set_label(f"usually {human_duration(self.expected_seconds)}")


class ActivityPage(Page):
    name = "activity"
    title = "Activity"

    def __init__(self, win):
        super().__init__(win)
        self.rows: dict[str, JobRow] = {}
        self.current_task: str | None = None
        self.started_at: float | None = None
        self.finished = True
        self.pending: list[tuple[str, str | None]] = []
        self.flush_id = 0
        self.follow = True
        self.last_log: str | None = None
        self.failure_log: str | None = None

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        idle = Adw.StatusPage(icon_name=icon("utilities-system-monitor-symbolic", "system-run-symbolic"),
                              title="Nothing Running",
                              description="Builds, updates and installs show their live progress here.")
        idle_buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        idle_buttons.append(button("Build Editor", None, "pill", "suggested-action", action="win.build-editor"))
        idle_buttons.append(button("Build Templates", None, "pill", action="win.build-templates"))
        idle.set_child(idle_buttons)
        self.stack.add_named(idle, "idle")

        # Active view -----------------------------------------------------------
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Gtk.Box(spacing=16, css_classes=["activity-header"])
        self.head_stack = Gtk.Stack(valign=Gtk.Align.CENTER)
        spinner = Adw.Spinner(width_request=36, height_request=36)
        self.head_icon = Gtk.Image(pixel_size=36)
        self.head_stack.add_named(spinner, "busy")
        self.head_stack.add_named(self.head_icon, "icon")
        header.append(self.head_stack)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.head_title = label("", "title-2", ellipsize=True)
        self.head_sub = label("", "dim-label", "activity-elapsed", ellipsize=True)
        text.append(self.head_title)
        text.append(self.head_sub)
        header.append(text)
        self.cancel_button = button("Cancel", "process-stop-symbolic", "destructive-action", "pill",
                                    on_click=self.win.cancel)
        self.again_button = button("Done", None, "pill", on_click=self.reset)
        header.append(self.cancel_button)
        header.append(self.again_button)
        outer.append(header)

        self.overall = Gtk.ProgressBar(margin_start=24, margin_end=24, margin_bottom=12)
        outer.append(self.overall)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, shrink_start_child=False,
                          shrink_end_child=False, wide_handle=True, position=250)
        jobs_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=96)
        self.jobs_scroll = jobs_scroll
        jobs_clamp = Adw.Clamp(maximum_size=1400, margin_start=24, margin_end=24, margin_bottom=12)
        jobs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.jobs = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        jobs_box.append(self.jobs)

        self.failure = Adw.PreferencesGroup(title="Build Failed", css_classes=["card", "failure-card"])
        self.failure.set_margin_top(0)
        self.failure_text = label("", "failure-text", "mono", wrap=True, selectable=True)
        self.failure_text.set_margin_end(8)
        self.failure_text.set_margin_bottom(14)
        self.failure.add(self.failure_text)
        self.failure.set_header_suffix(button("Open Full Log", None, "flat", on_click=self._open_failure_log))
        failure_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        failure_wrap.append(self.failure)
        self.failure.set_margin_start(0)
        self.failure_revealer = Gtk.Revealer(child=failure_wrap, reveal_child=False)
        jobs_box.append(self.failure_revealer)
        jobs_clamp.set_child(jobs_box)
        jobs_scroll.set_child(jobs_clamp)
        paned.set_start_child(jobs_scroll)

        # Output
        out_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=24, margin_end=24,
                          margin_bottom=18, margin_top=6)
        toolbar = Gtk.Box(spacing=6)
        self.output_title = label("Output", "heading", hexpand=True)
        toolbar.append(self.output_title)
        self.follow_btn = Gtk.ToggleButton(icon_name=icon("go-bottom-symbolic", "go-down-symbolic"),
                                           active=True, css_classes=["flat"], tooltip_text="Follow output")
        self.follow_btn.connect("toggled", lambda b: self._set_follow(b.get_active()))
        toolbar.append(self.follow_btn)
        self.wrap_btn = Gtk.ToggleButton(icon_name=icon("format-justify-left-symbolic", "text-wrap-symbolic"),
                                         css_classes=["flat"], tooltip_text="Wrap long lines")
        toolbar.append(self.wrap_btn)
        toolbar.append(button(None, "edit-copy-symbolic", "flat", tooltip="Copy output", on_click=self._copy_output))
        toolbar.append(button(None, "document-open-symbolic", "flat", tooltip="Open current log",
                              on_click=self._open_log))
        out_box.append(toolbar)

        frame = Gtk.Frame(css_classes=["view", "output-frame"], vexpand=True)
        self.scroll = Gtk.ScrolledWindow(vexpand=True)
        # A plain TextView takes its text colour from the theme (GtkSourceView
        # without a style scheme would force black text on the dark card).
        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.add_css_class("output-view")
        self.wrap_btn.connect("toggled", lambda b: self.view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if b.get_active() else Gtk.WrapMode.NONE))
        self.scroll.set_child(self.view)
        frame.set_child(self.scroll)
        out_box.append(frame)
        paned.set_end_child(out_box)
        outer.append(paned)

        self.end_mark = self.buffer.create_mark("end", self.buffer.get_end_iter(), False)
        self._make_tags()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._make_tags())
        Adw.StyleManager.get_default().connect("notify::accent-color", lambda *_: self._make_tags())

        # Scrolling up pauses following; reaching the end again resumes it.
        scroll_ctl = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctl.connect("scroll", self._on_user_scroll)
        self.view.add_controller(scroll_ctl)
        self.scroll.get_vadjustment().connect("value-changed", self._on_scrolled)

        self.stack.add_named(outer, "active")
        self.set_child(self.stack)
        GLib.timeout_add_seconds(1, self._tick)

    def _make_tags(self):
        dark = Adw.StyleManager.get_default().get_dark()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba().to_string()
        colors = {
            "error": "#ff7b72" if dark else "#c01c28",
            "warning": "#e5b14a" if dark else "#9c6e03",
            "ok": "#7ee2a8" if dark else "#1b8553",
            "info": accent,
            "heading": accent,
            "dim": "#9a9a9a" if dark else "#707070",
        }
        table = self.buffer.get_tag_table()
        for name, color in colors.items():
            tag = table.lookup(name)
            if tag is None:
                tag = self.buffer.create_tag(name)
            tag.set_property("foreground", color)
            if name == "heading":
                tag.set_property("weight", Pango.Weight.BOLD)
                tag.set_property("pixels-above-lines", 8)
            if name == "error":
                tag.set_property("weight", Pango.Weight.SEMIBOLD)

    def _set_follow(self, follow: bool):
        self.follow = follow
        if follow:
            self.view.scroll_to_mark(self.end_mark, 0, False, 0, 1)

    def _on_user_scroll(self, _ctl, _dx, dy):
        if dy < 0 and self.follow:
            self.follow_btn.set_active(False)
        return False

    def _on_scrolled(self, adj):
        at_bottom = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 4
        if at_bottom and not self.follow and not self.finished:
            self.follow_btn.set_active(True)

    # --- lifecycle ------------------------------------------------------------

    def begin(self, title: str, icon_name: str):
        self.reset_view()
        self.finished = False
        self.started_at = time.monotonic()
        self.head_title.set_label(title)
        self.head_icon.set_from_icon_name(icon_name)
        self.head_stack.set_visible_child_name("busy")
        self.cancel_button.set_visible(True)
        self.cancel_button.set_sensitive(True)
        self.again_button.set_visible(False)
        self.stack.set_visible_child_name("active")
        self._update_overall()
        self._tick()

    def begin_operation(self, op: Operation):
        self.append(f"▸ {op.title}", "heading")

    def end(self, ok: bool, interrupted: bool, title: str):
        self.finished = True
        elapsed = time.monotonic() - (self.started_at or time.monotonic())
        self.head_stack.set_visible_child_name("icon")
        if ok:
            self.head_icon.set_from_icon_name(icon("emblem-ok-symbolic"))
            self.head_icon.set_css_classes(["success"])
            self.head_title.set_label(f"{title} — finished")
        elif interrupted:
            self.head_icon.set_from_icon_name(icon("dialog-warning-symbolic"))
            self.head_icon.set_css_classes(["warning"])
            self.head_title.set_label(f"{title} — cancelled")
        else:
            self.head_icon.set_from_icon_name(icon("dialog-error-symbolic"))
            self.head_icon.set_css_classes(["error"])
            self.head_title.set_label(f"{title} — failed")
        self.head_sub.set_label(f"Took {human_duration(elapsed)}")
        for row in self.rows.values():
            if row.state in ("pending", "running"):
                row.set_state("interrupted" if interrupted else ("ok" if ok and row.state == "running"
                                                                  else "skipped"),
                              "Cancelled" if interrupted else ("" if ok else "Not run"))
        self.cancel_button.set_visible(False)
        self.again_button.set_visible(True)
        if ok:
            self.overall.set_fraction(1.0)
        self._flush()

    def reset(self):
        if self.finished:
            self.stack.set_visible_child_name("idle")
            self.reset_view()

    def reset_view(self):
        clear_box(self.jobs)
        self.rows.clear()
        self.current_task = None
        self.failure_revealer.set_reveal_child(False)
        self.failure_log = None
        self.buffer.set_text("")
        self.pending.clear()
        self.overall.set_fraction(0)
        self.jobs.set_visible(False)
        self.follow_btn.set_active(True)

    # --- events -----------------------------------------------------------------

    def _row(self, job_id: str, title: str, subtitle: str = "") -> JobRow:
        row = self.rows.get(job_id)
        if row is None:
            row = JobRow(job_id, title, subtitle)
            self.rows[job_id] = row
            self.jobs.append(row)
            self.jobs.set_visible(True)
        return row

    @staticmethod
    def job_title(job: dict) -> tuple[str, str]:
        if "title" in job and "platform" not in job:
            return job["title"], ""
        kind = {"editor": "Editor", "release": "Release template", "debug": "Debug template"}.get(
            job.get("kind"), job.get("kind", ""))
        title = f"{job.get('platform_label', '')} {kind} · {job.get('arch', '')}"
        bits = []
        if job.get("mono"):
            bits.append(".NET")
        if job.get("encrypt"):
            bits.append("encrypted")
        return title, " · ".join(bits)

    def handle(self, ev: dict):
        kind = ev.get("type")
        if kind == "plan":
            for job in ev.get("jobs", []):
                title, sub = self.job_title(job)
                row = self._row(job["id"], title, sub)
                row.expected_seconds = job.get("expected_seconds") or row.expected_seconds
                row.tick()
        elif kind == "task_start":
            ev["title"] = " ".join(ev.get("title", ev["id"]).split())
            row = self._row(ev["id"], ev["title"])
            row.expected_seconds = ev.get("expected_seconds") or row.expected_seconds
            row.set_state("running", "0s")
            row.log_path = ev.get("log")
            row.expected = ev.get("expected", 0)
            row.progress.set_fraction(0)
            self.current_task = ev["id"]
            self.last_log = ev.get("log")
            self.output_title.set_label(f"Output — {ev.get('title', '')}")
            self.append(f"── {ev.get('title', '')}", "heading")
        elif kind == "line":
            row = self.rows.get(ev.get("id"))
            text = ev.get("text", "")
            tag = "error" if ev.get("error") else ("warning" if "warning:" in text.lower() else None)
            self.append(text, tag)
            if row:
                expected = ev.get("expected") or 0
                steps = ev.get("steps") or 0
                if expected:
                    row.step_based = True
                    row.fraction = min(0.99, steps / expected)
                    row.progress.set_fraction(row.fraction)
                elif steps:
                    row.progress.pulse()
        elif kind == "task_end":
            row = self.rows.get(ev.get("id"))
            if row:
                if ev.get("interrupted"):
                    row.set_state("interrupted", "Cancelled")
                elif ev.get("ok"):
                    row.fraction = 1.0
                    row.set_state("ok", human_duration(time.monotonic() - (row.started or time.monotonic())))
                else:
                    row.set_state("failed", f"Exit {ev.get('code')}")
            self.current_task = None
        elif kind == "job":
            title, sub = self.job_title(ev)
            row = self._row(ev["id"], title, sub)
            status = ev.get("status")
            detail = human_duration(ev.get("seconds") or 0)
            if status == "ok" and ev.get("size"):
                detail = f"{human_size(ev['size'])} · {detail}"
            if status == "skipped":
                detail = "Skipped"
            row.set_state(status if status in ("ok", "failed", "interrupted", "skipped") else row.state, detail)
            if ev.get("log"):
                row.log_path = ev["log"]
        elif kind == "failure":
            row = self.rows.get(ev.get("id"))
            name = GLib.markup_escape_text(row.get_title()) if row else "Build"
            self.failure.set_title(f"{name} failed")
            self.failure.set_description(ev.get("label", "").capitalize() or None)
            self.failure_text.set_label("\n".join(ev.get("lines", [])[-40:]) or "See the log for details.")
            self.failure_log = ev.get("log")
            self.failure_revealer.set_reveal_child(True)

            def show_failure():
                adj = self.jobs_scroll.get_vadjustment()
                adj.set_value(adj.get_upper() - adj.get_page_size())
                return False

            GLib.timeout_add(self.failure_revealer.get_transition_duration() + 50, show_failure)
        elif kind == "message":
            level = ev.get("level", "info")
            prefix = {"ok": "✔ ", "error": "✖ ", "warning": "⚠ ", "info": "• ", "skip": "– ",
                      "detail": "   "}.get(level, "")
            tag = {"ok": "ok", "error": "error", "warning": "warning", "info": "info", "skip": "dim",
                   "detail": "dim"}.get(level)
            self.append(prefix + ev.get("text", ""), tag)
        elif kind == "heading":
            self.append(ev.get("text", ""), "heading")
        elif kind == "summary":
            ok = ev.get("succeeded", 0)
            total = len(ev.get("jobs", []))
            self.append(f"{ok}/{total} builds succeeded in {human_duration(ev.get('seconds', 0))}",
                        "ok" if ok == total else "warning")
        elif kind == "raw":
            self.append(ev.get("text", ""), "error" if ev.get("error") else None)
        self._update_overall()

    def _update_overall(self):
        rows = list(self.rows.values())
        if not rows:
            if not self.finished:
                self.overall.pulse()
            return
        done = sum(1 for r in rows if r.state in ("ok", "failed", "skipped", "interrupted"))
        running = sum(r.fraction for r in rows if r.state == "running")
        self.overall.set_fraction(min(1.0, (done + running) / len(rows)))
        self.win.sidebar_progress(self.overall.get_fraction(), self.head_title.get_label())

    def _tick(self):
        if not self.finished and self.started_at:
            elapsed = human_duration(time.monotonic() - self.started_at)
            done = sum(1 for r in self.rows.values() if r.state in ("ok", "failed", "skipped", "interrupted"))
            total = len(self.rows)
            jobs = f" · {done} of {total} steps" if total else ""
            eta = ""
            open_rows = [r for r in self.rows.values() if r.state in ("pending", "running")]
            known = [r.remaining() for r in open_rows]
            if open_rows and all(k is not None for k in known):
                eta = f" · {human_eta(sum(known))} left"
            elif any(k for k in known):
                eta = f" · at least {human_eta(sum(k for k in known if k))} left"
            self.head_sub.set_label(f"Running for {elapsed}{jobs}{eta}")
            for row in self.rows.values():
                row.tick()
        return True

    # --- output -----------------------------------------------------------------

    def append(self, text: str, tag: str | None = None):
        self.pending.append((text, tag))
        if not self.flush_id:
            self.flush_id = GLib.timeout_add(100, self._flush)

    def _flush(self):
        if self.pending:
            end = self.buffer.get_end_iter()
            for text, tag in self.pending:
                if self.buffer.get_char_count():
                    self.buffer.insert(end, "\n")
                if tag:
                    self.buffer.insert_with_tags_by_name(end, text, tag)
                else:
                    self.buffer.insert(end, text)
            self.pending.clear()
            lines = self.buffer.get_line_count()
            if lines > OUTPUT_MAX_LINES:
                start = self.buffer.get_start_iter()
                cut = self.buffer.get_iter_at_line(lines - OUTPUT_MAX_LINES)[1]
                self.buffer.delete(start, cut)
            if self.follow:
                self.buffer.move_mark(self.end_mark, self.buffer.get_end_iter())
                self.view.scroll_to_mark(self.end_mark, 0, False, 0, 1)
        self.flush_id = 0
        return False

    def _copy_output(self):
        start, end = self.buffer.get_bounds()
        self.win.get_clipboard().set(self.buffer.get_text(start, end, False))
        self.win.toast("Output copied")

    def _open_log(self):
        row = self.rows.get(self.current_task) if self.current_task else None
        path = (row.log_path if row else None) or self.last_log
        if path:
            self.win.logs.select_path(path)
        else:
            self.win.toast("No log for this step")

    def _open_failure_log(self):
        if self.failure_log:
            self.win.logs.select_path(self.failure_log)


# ------------------------------------------------------------------------------
#  Logs
# ------------------------------------------------------------------------------

class LogsPage(Page):
    name = "logs"
    title = "Build Logs"

    def __init__(self, win):
        super().__init__(win)
        self.logs: list[dict] = []
        self.current: str | None = None
        self.pending_select: str | None = None

        paned = Gtk.Paned(position=290, shrink_start_child=False, resize_start_child=False)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.filter = Gtk.SearchEntry(placeholder_text="Filter logs", margin_start=10, margin_end=10,
                                      margin_top=10, margin_bottom=6)
        self.filter.connect("search-changed", lambda *_: self.list.invalidate_filter())
        left.append(self.filter)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar", "log-list"])
        self.list.set_filter_func(self._filter)
        self.list.connect("row-selected", self._on_selected)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.list)
        left.append(scroll)
        foot = Gtk.Box(spacing=6, margin_start=10, margin_end=10, margin_bottom=10, margin_top=6)
        self.count_label = label("", "dim-label", "caption", hexpand=True)
        foot.append(self.count_label)
        foot.append(button(None, "folder-open-symbolic", "flat", tooltip="Open logs folder",
                           on_click=lambda: open_path(self.win, self.status.get("paths", {}).get("logs", "/"))))
        left.append(foot)
        paned.set_start_child(left)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=10, margin_bottom=6)
        self.log_title = label("Select a log", "heading", hexpand=True, ellipsize=True)
        bar.append(self.log_title)
        self.search = Gtk.SearchEntry(placeholder_text="Find in log", width_chars=16)
        self.search.connect("search-changed", self._on_search)
        self.search.connect("activate", lambda *_: self._find(True))
        self.search.connect("next-match", lambda *_: self._find(True))
        self.search.connect("previous-match", lambda *_: self._find(False))
        bar.append(self.search)
        nav = Gtk.Box(css_classes=["linked"])
        nav.append(button(None, "go-up-symbolic", tooltip="Previous match", on_click=lambda: self._find(False)))
        nav.append(button(None, "go-down-symbolic", tooltip="Next match", on_click=lambda: self._find(True)))
        bar.append(nav)
        self.match_label = label("", "dim-label", "caption")
        bar.append(self.match_label)
        bar.append(button(None, "document-open-symbolic", "flat", tooltip="Open in text editor",
                          on_click=lambda: self.current and open_path(self.win, self.current)))
        right.append(bar)

        if GtkSource:
            self.buffer = GtkSource.Buffer()
            self.view = GtkSource.View(buffer=self.buffer, show_line_numbers=True)
            self.search_settings = GtkSource.SearchSettings(wrap_around=True, case_sensitive=False)
            self.search_ctx = GtkSource.SearchContext(buffer=self.buffer, settings=self.search_settings)
            self._sync_scheme()
            Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._sync_scheme())
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer)
            self.search_ctx = None
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.add_css_class("output-view")
        self.view_stack = Gtk.Stack()
        self.view_stack.add_named(Adw.StatusPage(icon_name=icon("document-open-recent-symbolic"),
                                                 title="No Log Selected",
                                                 description="Pick a build log from the list."), "empty")
        frame = Gtk.ScrolledWindow(vexpand=True, child=self.view)
        self.view_stack.add_named(frame, "view")
        right.append(self.view_stack)
        paned.set_end_child(right)
        self.set_child(paned)

    def _sync_scheme(self):
        manager = GtkSource.StyleSchemeManager.get_default()
        dark = Adw.StyleManager.get_default().get_dark()
        for name in (("Adwaita-dark", "oblivion") if dark else ("Adwaita", "classic")):
            scheme = manager.get_scheme(name)
            if scheme:
                self.buffer.set_style_scheme(scheme)
                break

    def update(self, status):
        super().update(status)
        logs = status.get("logs", [])
        if [l["path"] for l in logs] == [l["path"] for l in self.logs] and \
                [l.get("exit_code") for l in logs] == [l.get("exit_code") for l in self.logs]:
            return
        self.logs = logs
        selected = self.current
        clear_box(self.list)
        for log in logs:
            code = log.get("exit_code")
            state = "ok" if code == 0 else ("pending" if code is None else ("warning" if code == 130 else "error"))
            row = Gtk.ListBoxRow()
            row.log = log
            box = Gtk.Box(spacing=10, margin_start=4, margin_end=4)
            box.append(state_icon(state))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            text.append(label(log.get("title", log["name"]), ellipsize=True))
            text.append(label(f"{human_ago(log.get('modified'))} · {human_size(log.get('size'))}",
                              "dim-label", "caption", ellipsize=True))
            box.append(text)
            row.set_child(box)
            row.set_tooltip_text(log["name"])
            self.list.append(row)
        self.count_label.set_label(f"{len(logs)} log{'s' if len(logs) != 1 else ''}")
        target = self.pending_select or selected
        if target:
            self._select_row(target)

    def _filter(self, row) -> bool:
        query = self.filter.get_text().strip().lower()
        if not query:
            return True
        log = getattr(row, "log", {})
        return query in log.get("title", "").lower() or query in log.get("name", "").lower()

    def _select_row(self, path: str) -> bool:
        row = self.list.get_first_child()
        while row:
            if getattr(row, "log", {}).get("path") == path:
                self.list.select_row(row)
                return True
            row = row.get_next_sibling()
        return False

    def select_path(self, path: str):
        self.win.navigate("logs")
        self.pending_select = path
        if not self._select_row(path):
            self.win.refresh()
        self._load(path)

    def _on_selected(self, _list, row):
        if row is not None:
            self._load(row.log["path"])

    def _load(self, path: str):
        if path == self.current and self.view_stack.get_visible_child_name() == "view" and self.pending_select is None:
            return
        self.pending_select = None
        self.current = path
        self.log_title.set_label(Path(path).name)
        gfile = Gio.File.new_for_path(path)

        def done(file, result):
            try:
                _ok, data, _etag = file.load_contents_finish(result)
                text = data.decode("utf-8", errors="replace")
            except GLib.Error as exc:
                text = f"Could not read {path}: {exc.message}"
            self.buffer.set_text(text)
            self.view_stack.set_visible_child_name("view")
            end = self.buffer.get_end_iter()
            self.buffer.place_cursor(end)
            GLib.idle_add(lambda: self.view.scroll_to_iter(self.buffer.get_end_iter(), 0, False, 0, 1) and False)
            self._on_search()

        gfile.load_contents_async(None, done)

    def _on_search(self, *_):
        if not self.search_ctx:
            return
        text = self.search.get_text()
        self.search_settings.set_search_text(text or None)
        GLib.timeout_add(150, self._update_count)

    def _update_count(self):
        if not self.search_ctx:
            return False
        count = self.search_ctx.get_occurrences_count()
        self.match_label.set_label("" if not self.search.get_text() else
                                   ("No matches" if count == 0 else f"{count} matches" if count > 0 else ""))
        return False

    def _find(self, forward: bool):
        if not self.search_ctx or not self.search.get_text():
            return
        mark = self.buffer.get_insert()
        it = self.buffer.get_iter_at_mark(mark)
        if forward:
            found, start, end, _wrapped = self.search_ctx.forward(it)
        else:
            sel = self.buffer.get_selection_bounds()
            if sel:
                it = sel[0]
            found, start, end, _wrapped = self.search_ctx.backward(it)
        if found:
            # The insert mark is where the next search starts from.
            if forward:
                self.buffer.select_range(end, start)
            else:
                self.buffer.select_range(start, end)
            self.view.scroll_to_iter(start, 0.2, False, 0, 0.5)


# ------------------------------------------------------------------------------
#  Diagnostics
# ------------------------------------------------------------------------------

class DiagnosticsPage(Page):
    name = "diagnostics"
    title = "Diagnostics"

    def __init__(self, win):
        super().__init__(win)
        self.ran = False
        self.running = False
        self.groups: dict[str, Adw.PreferencesGroup] = {}
        self.counts = {"ok": 0, "warning": 0, "error": 0}

        header = Adw.PreferencesGroup(title="System Check",
                                      description="Verifies compilers, SDKs, cross toolchains, the encryption key "
                                                  "and the desktop integration.")
        self.run_button = button("Run Checks", "view-refresh-symbolic", "flat", action="win.run-doctor")
        header.set_header_suffix(self.run_button)
        self.summary_box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self.summary_stack = Gtk.Stack()
        busy = Gtk.Box(spacing=10)
        busy.append(Adw.Spinner())
        busy.append(label("Running checks…", "dim-label"))
        self.summary_stack.add_named(busy, "busy")
        self.summary_stack.add_named(self.summary_box, "summary")
        header.add(self.summary_stack)

        self.chains = Adw.PreferencesGroup(
            title="Cross-Compile Toolchains",
            description="Everything needed to build Windows and macOS templates on Linux. Downloads and builds "
                        "go into the workspace's toolchains folder.")
        self.chain_rows: dict[str, tuple[Adw.ActionRow, Gtk.Image, Gtk.Button]] = {}
        for key, title, handler in (
                ("mingw", "Windows · llvm-mingw", self._install_mingw),
                ("osxcross", "macOS · osxcross", self._install_osxcross),
                ("moltenvk", "macOS · MoltenVK (Vulkan)", self._install_moltenvk)):
            row = Adw.ActionRow(title=title)
            row.set_subtitle_lines(3)
            state = Gtk.Image()
            row.add_prefix(state)
            install = button("Install", None, on_click=handler)
            install.set_valign(Gtk.Align.CENTER)
            row.add_suffix(install)
            self.chains.add(row)
            self.chain_rows[key] = (row, state, install)

        tools = Adw.PreferencesGroup(title="Fix-ups")
        deps = action_row("Download Windows build dependencies",
                          "ANGLE, Direct3D 12 Agility SDK, Mesa NIR, PIX and AccessKit",
                          icon("folder-download-symbolic", "document-save-symbolic"), chevron=True)
        deps.set_activatable(True)
        deps.connect("activated", lambda *_: self.win.run_heavy(
            [Operation("Download dependencies", ["deps"], icon_name="folder-download-symbolic")]))
        tools.add(deps)
        integ = action_row("Reinstall desktop integration", "Launcher entry, right-click actions, icons and "
                                                            "the .godot file type",
                           icon("application-x-addon-symbolic", "applications-system-symbolic"), chevron=True,
                           on_activate=lambda: self.win.run_integrate(False))
        tools.add(integ)

        self.page = self.make_page(header, self.chains, tools)
        self.tools_group = tools
        self.set_child(self.page)

    def shown(self):
        if not self.ran and not self.running:
            self.run()

    def update(self, status):
        super().update(status)
        chains = status.get("toolchains") or {}
        for key, (row, state, install) in self.chain_rows.items():
            item = chains.get(key, {})
            installed = item.get("installed", False)
            if key == "mingw":
                text = (f"{item.get('path')} {item.get('version', '')}" if installed else
                        "Not installed · downloads the latest llvm-mingw release (~85 MB), "
                        "then Godot's Windows dependencies")
            elif key == "osxcross":
                missing = item.get("missing_tools") or []
                text = (f"{item.get('path')} · SDK {item.get('sdk') or '?'}" if installed else
                        "Not installed · built from Apple's macOS SDK (Xcode download), takes 5–20 minutes")
                if missing and not installed:
                    text += f"\nNeeds {', '.join(missing)}" + (f": {item['packages']}" if item.get("packages") else "")
            else:
                text = (f"{item.get('version', '')} · Intel Macs get Vulkan instead of OpenGL" if installed else
                        "Not installed · Vulkan for macOS templates (~57 MB). Without it Intel Macs fall back "
                        "to OpenGL; Apple Silicon uses Metal either way")
                if installed and not item.get("enabled", True):
                    text = f"{item.get('version', '')} · turned off in Settings (Vulkan on macOS)"
            row.set_subtitle(GLib.markup_escape_text(text.strip()))
            state.set_from_icon_name(icon("emblem-ok-symbolic", "object-select-symbolic") if installed
                                     else icon("dialog-warning-symbolic"))
            for css in ("success", "warning"):
                state.remove_css_class(css)
            state.add_css_class("success" if installed else "warning")
            install.set_label("Reinstall" if installed else "Install")
            install.remove_css_class("suggested-action")
            if not installed:
                install.add_css_class("suggested-action")

    def _install_mingw(self):
        ops = [Operation("Install llvm-mingw", ["toolchain", "install", "mingw"],
                         icon_name="folder-download-symbolic")]
        if not getattr(self.win, "needs_setup", False):
            ops.append(Operation("Download Windows build dependencies", ["deps"], icon_name="folder-download-symbolic"))
        self.win.run_heavy(ops, "Set Up Windows Toolchain")

    def _install_moltenvk(self):
        self.win.run_heavy([Operation("Install MoltenVK", ["toolchain", "install", "moltenvk"],
                                      icon_name="folder-download-symbolic")])

    def _install_osxcross(self):
        item = (self.status.get("toolchains") or {}).get("osxcross", {})
        archives = item.get("sdk_archives") or []
        dialog = Adw.AlertDialog(
            heading="Install osxcross",
            body="osxcross needs Apple's macOS SDK, which Apple only distributes inside Xcode. Download Xcode "
                 "(a .xip file, free Apple account) from Apple's developer site, then choose it here. A "
                 "MacOSX<version>.sdk.tar.xz packaged on a Mac with osxcross's gen_sdk_package.sh works too.\n\n"
                 "Building takes 5–20 minutes. Apple's licence allows the SDK only for building on or for "
                 "Apple hardware.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("xcode", "_Get Xcode")
        if archives:
            dialog.add_response("existing", f"_Use {archives[-1]}")
        dialog.add_response("choose", "_Choose File…")
        dialog.set_response_appearance("choose", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("choose")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "xcode":
                Gtk.UriLauncher.new("https://developer.apple.com/download/all/?q=xcode").launch(self.win, None, None)
            elif response == "existing":
                self._run_osxcross(None)
            elif response == "choose":
                self._choose_sdk()

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _choose_sdk(self):
        sdk_filter = Gtk.FileFilter(name="Xcode or macOS SDK")
        for pattern in ("*.xip", "MacOSX*.sdk.tar.*"):
            sdk_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(sdk_filter)
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home())
        dialog = Gtk.FileDialog(title="Choose Xcode or a macOS SDK", filters=filters, default_filter=sdk_filter,
                                initial_folder=Gio.File.new_for_path(downloads))

        def chosen(dlg, result):
            try:
                self._run_osxcross(dlg.open_finish(result).get_path())
            except GLib.Error:
                return

        dialog.open(self.win, None, chosen)

    def _run_osxcross(self, sdk: str | None):
        args = ["toolchain", "install", "osxcross"] + (["--sdk", sdk] if sdk else [])
        self.win.run_heavy([Operation("Build osxcross", args, icon_name="system-run-symbolic")],
                           "Set Up macOS Toolchain")

    def run(self):
        if self.running:
            return
        self.running = True
        self.ran = True
        self.win.set_action_enabled("run-doctor", False)
        for group in self.groups.values():
            self.page.remove(group)
        self.groups.clear()
        self.counts = {"ok": 0, "warning": 0, "error": 0}
        self.summary_stack.set_visible_child_name("busy")
        BackendProcess(["doctor"], self._on_event, self._on_exit)

    def _on_event(self, ev):
        if ev["type"] != "check":
            return
        section = ev.get("section") or "General"
        group = self.groups.get(section)
        if group is None:
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(section))
            self.groups[section] = group
            self.page.remove(self.tools_group)
            self.page.add(group)
            self.page.add(self.tools_group)
        state = ev.get("state", "ok")
        self.counts[state] = self.counts.get(state, 0) + 1
        row = Adw.ActionRow(title=GLib.markup_escape_text(ev.get("label", "")),
                            subtitle=GLib.markup_escape_text(ev.get("text", "")))
        row.set_subtitle_lines(3)
        row.set_subtitle_selectable(True)
        row.add_prefix(state_icon(state))
        group.add(row)

    def _on_exit(self, _code):
        self.running = False
        self.win.set_action_enabled("run-doctor", True)
        clear_box(self.summary_box)
        ok, warn, err = self.counts["ok"], self.counts["warning"], self.counts["error"]
        self.summary_box.append(chip(f"{ok} passed", "success"))
        if warn:
            self.summary_box.append(chip(f"{warn} warning{'s' if warn != 1 else ''}", "warning"))
        if err:
            self.summary_box.append(chip(f"{err} problem{'s' if err != 1 else ''}", "error"))
        self.summary_box.append(chip(f"checked {dt.datetime.now():%H:%M}"))
        self.summary_stack.set_visible_child_name("summary")
        self.win.set_sidebar_badge("diagnostics", err or None)


# ------------------------------------------------------------------------------
#  Settings
# ------------------------------------------------------------------------------

class SettingsPage(Page):
    name = "settings"
    title = "Settings"

    def __init__(self, win):
        super().__init__(win)
        self.config_group = Adw.PreferencesGroup(
            title="Build Configuration",
            description="Toggles in custom.py. Changes apply to the next build; the file is backed up once to "
                        "custom.py.bak.")
        self.config_group.set_header_suffix(button(None, "document-edit-symbolic", "flat",
                                                   tooltip="Open custom.py",
                                                   on_click=lambda: open_path(self.win, self.status.get(
                                                       "paths", {}).get("custom_py", ""))))
        self.config_rows: dict[str, Adw.PreferencesRow] = {}
        self._syncing = False
        self.template_row = Adw.ActionRow(
            title="Install the recommended custom.py",
            subtitle="Production defaults (LTO, compiler cache, per-platform toolchains) plus the toggles this "
                     "page edits. An existing custom.py is backed up first.")
        self.template_row.add_prefix(Gtk.Image(icon_name=icon("document-new-symbolic", "text-x-generic-symbolic")))
        self.template_row.add_suffix(button("Install", None, "suggested-action",
                                            on_click=lambda: self._confirm_install_custom_py()))
        self.config_group.add(self.template_row)
        self.manager_group = Adw.PreferencesGroup(
            title="Build Manager",
            description="Folders, launcher and update checks. Opening Godot from your launcher checks for engine "
                        "updates in the background (at most once per interval) and notifies you about new changes.")

        app = Adw.PreferencesGroup(title="Application")
        self.sw_notify = Adw.SwitchRow(title="Desktop notifications",
                                       subtitle="Notify when a build finishes while the window is in the background",
                                       active=self.win.prefs["notifications"])
        self.sw_notify.connect("notify::active",
                               lambda r, _p: self.win.prefs.__setitem__("notifications", r.get_active()))
        self.sw_follow = Adw.SwitchRow(title="Show Activity when a build starts",
                                       active=self.win.prefs["follow_activity"])
        self.sw_follow.connect("notify::active",
                               lambda r, _p: self.win.prefs.__setitem__("follow_activity", r.get_active()))
        app.add(self.sw_notify)
        app.add(self.sw_follow)

        desktop = Adw.PreferencesGroup(
            title="Desktop Integration",
            description="Adds Godot to your app launcher with “Build Manager” and “Check for Engine Updates” "
                        "right-click actions, links ~/.local/bin/godot and registers .godot project files.")
        self.row_desktop = action_row("Launcher entry", "", icon("view-app-grid-symbolic",
                                                                  "application-x-executable-symbolic"))
        self.install_btn = button("Install", None, "flat", on_click=lambda: self.win.run_integrate(False))
        self.remove_btn = button("Remove", None, "flat", on_click=self._confirm_remove)
        self.row_desktop.add_suffix(self.install_btn)
        self.row_desktop.add_suffix(self.remove_btn)
        desktop.add(self.row_desktop)

        self.folders = Adw.PreferencesGroup(title="Folders")
        self.folder_rows: dict[str, Adw.ActionRow] = {}
        for key, title in (("config", "Configuration"), ("dist", "Staged output (Dist)"), ("logs", "Build logs"),
                           ("templates", "Installed export templates"), ("nuget", "Local NuGet feed")):
            row = action_row(title, "", icon("folder-symbolic"), selectable=True)
            row.add_suffix(button(None, "folder-open-symbolic", "flat", tooltip="Open",
                                  on_click=lambda k=key: self._open_folder(k)))
            self.folders.add(row)
            self.folder_rows[key] = row

        maint = Adw.PreferencesGroup(title="Maintenance")
        for flag, title, subtitle, destructive in (
            ("--logs", "Delete build logs", "Removes every log in the logs folder", False),
            ("--dist", "Delete staged templates", "Removes the Dist folder contents and .tpz packages", False),
            ("--objects", "Delete object files", "Forces the next build to compile everything from scratch", True),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_suffix(button("Delete…", None, "destructive-action" if destructive else "flat",
                                  on_click=lambda f=flag, t=title: self._confirm_clean(f, t)))
            maint.add(row)

        self.set_child(self.make_page(self.manager_group, self.config_group, app, desktop, self.folders, maint))

    def _open_folder(self, key):
        path = self.status.get("paths", {}).get(key)
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            open_path(self.win, path)

    def update(self, status):
        super().update(status)
        launcher = status.get("launcher", {})
        installed = launcher.get("desktop_entry")
        set_row_text(self.row_desktop, subtitle="Installed · right-click Godot in your launcher" if installed
                     else "Not installed")
        self.install_btn.set_label("Reinstall" if installed else "Install")
        if not installed:
            self.install_btn.add_css_class("suggested-action")
        else:
            self.install_btn.remove_css_class("suggested-action")
        self.remove_btn.set_visible(bool(installed))
        for key, row in self.folder_rows.items():
            set_row_text(row, subtitle=status.get("paths", {}).get(key, ""))
        settings = status.get("settings", [])
        self._apply_settings(settings)
        # Without the template's toggles in custom.py there is nothing to edit here.
        missing = not any(s.get("present") for s in settings)
        self.template_row.set_visible(missing)
        for s in settings:
            row = self.config_rows.get(s["name"])
            if row:
                row.set_visible(not missing)
        self._apply_settings(status.get("manager", []), self.manager_group)

    def _apply_settings(self, settings: list[dict], group: Adw.PreferencesGroup | None = None):
        self._syncing = True
        try:
            for s in settings:
                row = self.config_rows.get(s["name"])
                if row is None:
                    row = self._make_row(s)
                    self.config_rows[s["name"]] = row
                    (group or self.config_group).add(row)
                row.set_sensitive(s.get("present", True))
                if s["type"] == "bool":
                    row.set_active(bool(s["value"]))
                elif s["type"] == "int":
                    row.set_value(int(s["value"] or 0))
                else:
                    if row.get_text() != (s["value"] or ""):
                        row.set_text(s["value"] or "")
        finally:
            self._syncing = False

    def _make_row(self, s: dict):
        name = s["name"]
        if s["type"] == "bool":
            row = Adw.SwitchRow(title=s["title"], subtitle=s["description"])
            row.connect("notify::active", lambda r, _p: self._set(name, "true" if r.get_active() else "false"))
        elif s["type"] == "int":
            # Manager counts (hours, snapshots) need at least 1; the cache limit allows 0 = unlimited.
            row = Adw.SpinRow.new_with_range(0 if name.startswith("_") else 1, 500, 1)
            row.set_title(s["title"])
            row.set_subtitle(s["description"])
            row._debounce = 0

            def changed(r, _p):
                if self._syncing:
                    return
                if r._debounce:
                    GLib.source_remove(r._debounce)

                def fire():
                    r._debounce = 0
                    self._set(name, str(int(r.get_value())))
                    return False

                r._debounce = GLib.timeout_add(700, fire)

            row.connect("notify::value", changed)
        else:
            row = Adw.EntryRow(title=s["title"], show_apply_button=True)
            row.set_tooltip_text(s["description"])
            row.connect("apply", lambda r: self._set(name, r.get_text()))
            if s["type"] == "path":
                pick = button(None, "folder-open-symbolic", "flat", tooltip="Choose folder",
                              on_click=lambda r=row, n=name: self._pick_folder(r, n))
                row.add_suffix(pick)
        return row

    def _pick_folder(self, row, name: str):
        current = row.get_text().strip()
        start = Path(current).expanduser() if current and Path(current).expanduser().is_dir() else Path.home()
        dialog = Gtk.FileDialog(title=row.get_title(), initial_folder=Gio.File.new_for_path(str(start)))

        def chosen(dlg, result):
            try:
                path = dlg.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            row.set_text(path)
            self._set(name, path)

        dialog.select_folder(self.win, None, chosen)

    def _install_custom_py(self, force: bool):
        errors: list[str] = []

        def on_event(ev):
            if ev["type"] == "message" and ev.get("level") == "error":
                errors.append(ev.get("text", ""))

        def on_exit(code):
            self.win.toast("Recommended custom.py installed" if code == 0
                           else (errors[0] if errors else "Could not install custom.py"), 5)
            self.win.refresh()

        BackendProcess(["config", "install-template", *(["--force"] if force else [])], on_event, on_exit)

    def _confirm_install_custom_py(self):
        exists = Path(self.status.get("paths", {}).get("custom_py", "")).is_file()
        if not exists:
            self._install_custom_py(False)
            return
        dialog = Adw.AlertDialog(heading="Replace custom.py?",
                                 body="Your current custom.py is kept as a timestamped .bak file next to it.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("replace", "_Replace")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "replace" and self._install_custom_py(True))
        dialog.present(self.win)

    def _set(self, name: str, value: str):
        if self._syncing:
            return
        errors: list[str] = []

        def on_event(ev):
            if ev["type"] == "config":
                self._apply_settings(ev.get("settings", []))
                self._apply_settings(ev.get("manager", []), self.manager_group)
            elif ev["type"] == "message" and ev.get("level") == "error":
                errors.append(ev.get("text", ""))

        def on_exit(code):
            if code == 0:
                manager = not name.startswith("_")
                self.win.toast("Setting saved" if manager else "custom.py updated")
            else:
                self.win.toast(errors[0] if errors else "Could not update custom.py")
            self.win.refresh()

        BackendProcess(["config", "set", name, value], on_event, on_exit)

    def _confirm_clean(self, flag: str, title: str):
        dialog = Adw.AlertDialog(heading=f"{title}?", body="This cannot be undone.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("delete", "_Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "delete" and self.win.run_heavy(
            [Operation(title, ["clean", flag], icon_name="user-trash-symbolic")]))
        dialog.present(self.win)

    def _confirm_remove(self):
        dialog = Adw.AlertDialog(heading="Remove Desktop Integration?",
                                 body="Godot disappears from your app launcher and ~/.local/bin/godot is removed. "
                                      "Built binaries are kept.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("remove", "_Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "remove" and self.win.run_integrate(True))
        dialog.present(self.win)


class SnapshotArea(Gtk.Widget):
    """Minimal custom-drawn widget using GtkSnapshot (no pycairo dependency)."""

    def __init__(self, draw, height: int):
        super().__init__(hexpand=True)
        self._draw = draw
        self.set_size_request(-1, height)

    def do_snapshot(self, snapshot):
        self._draw(self, snapshot, self.get_width(), self.get_height())


# ------------------------------------------------------------------------------
#  Build history
# ------------------------------------------------------------------------------

class HistoryPage(Page):
    """Build history: per-step duration chart, stat tiles and a list of recent runs."""

    name = "history"
    title = "Build History"

    OUTCOMES = {"ok": "succeeded", "warning": "cancelled", "error": "failed"}
    CHART_RUNS = 40
    RECENT_RUNS = 25
    TICK_STEPS = (5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800)
    PAD_L, PAD_R, PAD_T, PAD_B = 42, 10, 12, 22

    def __init__(self, win):
        super().__init__(win)
        self.entries: list[dict] = []
        self.steps: list[tuple[str, str]] = []      # (key, label), most recently run first
        self.selected_key: str | None = None
        self.runs: list[dict] = []                  # runs of the selected step shown in the chart
        self.bar_rects: list[tuple[float, float]] = []
        self.loaded = False
        self.loading = False
        self.reload_pending = False
        self.proc = None
        self._incoming: list[dict] = []
        self._updating = False
        self._recent_rows: list[Adw.ActionRow] = []

        # --- header group: step picker, tiles, chart -------------------------------
        self.header = Adw.PreferencesGroup(title="Build History")
        self.step_model = Gtk.StringList()
        self.dropdown = Gtk.DropDown(model=self.step_model, valign=Gtk.Align.CENTER)
        self.dropdown.set_enable_search(True)
        self.dropdown.connect("notify::selected", self._on_step_selected)
        self.header.set_header_suffix(self.dropdown)

        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 min_children_per_line=1, max_children_per_line=2,
                                 column_spacing=12, row_spacing=12)
        self.tile_values: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, title in (("last", "LAST RUN"), ("typical", "TYPICAL"),
                           ("fastest", "FASTEST"), ("cache", "CACHE HIT RATE")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["card", "stat-card"])
            value = label("—", "stat-value", "numeric")
            caption = label("", "stat-caption", wrap=True)
            for widget in (label(title, "stat-label"), value, caption):
                box.append(widget)
            self.tile_values[key] = (value, caption)
            self.tiles.append(box)
        self.header.add(self.tiles)

        self.chart = SnapshotArea(self._draw_chart, height=220)
        self.chart.set_has_tooltip(True)
        self.chart.connect("query-tooltip", self._on_query_tooltip)
        frame = Gtk.Box(css_classes=["card"], margin_top=12)
        frame.append(self.chart)
        self.header.add(frame)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self.chart.queue_draw())
        style.connect("notify::accent-color-rgba", lambda *_: self.chart.queue_draw())

        # --- recent runs ----------------------------------------------------------
        self.recent = Adw.PreferencesGroup(title="Recent Runs",
                                           description="The latest builds across every step. Select one to open its log.")

        self.content = self.make_page(self.header, self.recent)

        # --- loading / empty / content -------------------------------------------
        spinner_box = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner_box.append(Adw.Spinner(width_request=32, height_request=32))
        empty = Adw.StatusPage(title="No Builds Yet", icon_name=icon("document-open-recent-symbolic",
                                                                     "folder-symbolic"),
                               description="Completed builds will appear here with their timings.")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(spinner_box, "loading")
        self.stack.add_named(empty, "empty")
        self.stack.add_named(self.content, "content")
        self.stack.set_visible_child_name("loading")
        self.set_child(self.stack)

    # ------------------------------------------------------------------ loading
    def shown(self):
        if not self.loaded and not self.loading:
            self.reload()

    def reload(self):
        """Fetch the history again (the lead calls this after builds finish)."""
        if self.loading:
            self.reload_pending = True
            return
        self.loading = True
        self._incoming = []
        if not self.loaded:
            self.stack.set_visible_child_name("loading")
        try:
            self.proc = BackendProcess(["history", "--limit", "1000"], self._on_event, self._on_exit)
        except GLib.Error as exc:
            self.loading = False
            self.win.toast(f"Could not load history: {exc.message}")

    def _on_event(self, ev):
        if ev.get("type") == "history":
            self._incoming = list(ev.get("entries") or [])

    def _on_exit(self, _code):
        self.loading = False
        self.loaded = True
        self.entries = sorted(self._incoming, key=lambda e: to_datetime(e.get("time")) or dt.datetime.min.astimezone())
        self._rebuild()
        if self.reload_pending:
            self.reload_pending = False
            self.reload()

    # ------------------------------------------------------------------ model
    @staticmethod
    def _key(entry) -> str:
        return entry.get("key") or entry.get("title") or "?"

    def _rebuild(self):
        if not self.entries:
            self.stack.set_visible_child_name("empty")
            return
        newest_first = list(reversed(self.entries))
        steps: dict[str, str] = {}
        for entry in newest_first:
            steps.setdefault(self._key(entry), entry.get("title") or self._key(entry))
        self.steps = list(steps.items())

        total = sum(float(e.get("seconds") or 0) for e in self.entries)
        count = len(self.entries)
        self.header.set_description(f"{count} run{'s' if count != 1 else ''} recorded · "
                                    f"{human_duration(total)} total build time")

        if self.selected_key not in steps:
            self.selected_key = self.steps[0][0]
        self._updating = True
        self.step_model.splice(0, self.step_model.get_n_items(), [text for _k, text in self.steps])
        self.dropdown.set_selected([k for k, _t in self.steps].index(self.selected_key))
        self._updating = False

        for row in self._recent_rows:
            self.recent.remove(row)
        self._recent_rows.clear()
        for entry in newest_first[:self.RECENT_RUNS]:
            row = self._recent_row(entry)
            self._recent_rows.append(row)
            self.recent.add(row)

        self._select_step()
        self.stack.set_visible_child_name("content")

    @staticmethod
    def _state(entry) -> str:
        if entry.get("ok"):
            return "ok"
        return "warning" if entry.get("code") == 130 else "error"

    @staticmethod
    def _cache_rate(entries) -> float | None:
        hits = misses = 0
        for entry in entries:
            cc = entry.get("ccache")
            if cc:
                hits += int(cc.get("hits") or 0)
                misses += int(cc.get("misses") or 0)
        return hits / (hits + misses) if hits + misses > 0 else None

    def _recent_row(self, entry) -> Adw.ActionRow:
        sub = f"{human_ago(entry.get('time'))} · {human_duration(entry.get('seconds') or 0)}"
        if entry.get("commit"):
            sub += f" · {entry['commit']}"
        rate = self._cache_rate([entry])
        if rate is not None:
            sub += f" · {round(rate * 100)}% cached"
        row = action_row(entry.get("title") or self._key(entry), sub, chevron=True)
        row.add_prefix(state_icon(self._state(entry)))
        log = entry.get("log")
        if log:
            row.set_activatable(True)
            row.connect("activated", lambda *_: self.win.logs.select_path(log))
        return row

    # ------------------------------------------------------------------ selected step
    def _on_step_selected(self, *_):
        if self._updating or not self.steps:
            return
        index = self.dropdown.get_selected()
        if 0 <= index < len(self.steps):
            self.selected_key = self.steps[index][0]
            self._select_step()

    def _select_step(self):
        runs = [e for e in self.entries if self._key(e) == self.selected_key]
        self.runs = runs[-self.CHART_RUNS:]
        good = sorted(float(e.get("seconds") or 0) for e in runs if e.get("ok"))
        last = runs[-1]

        def put(key, value, caption=""):
            self.tile_values[key][0].set_text(value)
            self.tile_values[key][1].set_text(caption)

        state = self.OUTCOMES[self._state(last)]
        put("last", human_duration(last.get("seconds") or 0), f"{human_ago(last.get('time'))} · {state}")
        if good:
            mid = len(good) // 2
            median = good[mid] if len(good) % 2 else (good[mid - 1] + good[mid]) / 2
            put("typical", human_duration(median), f"median of {len(good)} successful")
            put("fastest", human_duration(good[0]), "best successful run")
        else:
            put("typical", "—", "no successful runs")
            put("fastest", "—", "no successful runs")
        rate = self._cache_rate(runs)
        put("cache", f"{rate * 100:.0f}%" if rate is not None else "—",
            "ccache hits across runs" if rate is not None else "no ccache data")
        self.chart.queue_draw()

    # ------------------------------------------------------------------ chart
    def _nice_scale(self, peak: float) -> tuple[float, float]:
        for step in self.TICK_STEPS:
            if peak / step <= 4:
                return step, max(step, step * -(-peak // step))
        step = self.TICK_STEPS[-1]
        return step, step * -(-peak // step)

    @staticmethod
    def _tick_text(value: float) -> str:
        if value == 0:
            return "0"
        return f"{value:g}s" if value < 60 else f"{value / 60:g}m"

    def _bar_color(self, entry, dark: bool, accent) -> tuple:
        state = self._state(entry)
        if state == "ok":
            return accent.red, accent.green, accent.blue
        hexes = {"error": ("#ff7b72", "#c01c28"), "warning": ("#e5b14a", "#9c6e03")}[state]
        h = hexes[0 if dark else 1].lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    @staticmethod
    def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
        color = Gdk.RGBA()
        color.red, color.green, color.blue, color.alpha = r, g, b, a
        return color

    def _text(self, area, snapshot, text: str, x: float, y: float, color: Gdk.RGBA, align_right=False):
        layout = area.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string("9"))
        width = layout.get_pixel_size()[0]
        snapshot.save()
        point = Graphene.Point()
        point.init(x - width if align_right else x, y)
        snapshot.translate(point)
        snapshot.append_layout(layout, color)
        snapshot.restore()

    def _draw_chart(self, area, snapshot, width, height):
        self.bar_rects = []
        runs = self.runs
        if not runs:
            return
        fg = area.get_color()
        style = Adw.StyleManager.get_default()
        dark = style.get_dark()
        accent = style.get_accent_color_rgba()

        left, right, top, bottom = self.PAD_L, width - self.PAD_R, self.PAD_T, height - self.PAD_B
        plot_w, plot_h = max(1, right - left), max(1, bottom - top)
        peak = max(float(e.get("seconds") or 0) for e in runs) or 1.0
        step, ymax = self._nice_scale(peak)

        grid = self._rgba(fg.red, fg.green, fg.blue, 0.14)
        text = self._rgba(fg.red, fg.green, fg.blue, 0.65)
        value = 0.0
        while value <= ymax + 1e-6:
            y = round(bottom - plot_h * value / ymax)
            snapshot.append_color(grid, Graphene.Rect().init(left, y, plot_w, 1))
            self._text(area, snapshot, self._tick_text(value), left - 6, y - 7, text, align_right=True)
            value += step

        slot = plot_w / max(len(runs), 1)
        bar_w = max(2.0, min(slot * 0.72, 28.0))
        for i, entry in enumerate(runs):
            x = left + slot * i + (slot - bar_w) / 2
            h = max(2.0, plot_h * float(entry.get("seconds") or 0) / ymax)
            r, g, b = self._bar_color(entry, dark, accent)
            rect = Graphene.Rect().init(x, bottom - h, bar_w, h)
            radius = min(3.0, bar_w / 2, h / 2)
            rounded = Gsk.RoundedRect()
            corner = Graphene.Size().init(radius, radius)
            flat = Graphene.Size().init(0, 0)
            rounded.init(rect, corner, corner, flat, flat)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(self._rgba(r, g, b, 0.95), rect)
            snapshot.pop()
            self.bar_rects.append((left + slot * i, left + slot * (i + 1)))

        first, last = to_datetime(runs[0].get("time")), to_datetime(runs[-1].get("time"))
        if first:
            self._text(area, snapshot, f"{first:%d %b}", left, height - 16, text)
        if last and len(runs) > 1:
            self._text(area, snapshot, f"{last:%d %b}", right, height - 16, text, align_right=True)

    def _on_query_tooltip(self, _area, x, _y, _keyboard, tooltip):
        for entry, (x0, x1) in zip(self.runs, self.bar_rects):
            if x0 <= x < x1:
                outcome = self.OUTCOMES[self._state(entry)]
                lines = [human_date(entry.get("time")),
                         f"{human_duration(entry.get('seconds') or 0)} · {outcome}"]
                if entry.get("commit"):
                    lines.append(f"Commit {entry['commit']}")
                cc = entry.get("ccache")
                if cc and (cc.get("hits") or 0) + (cc.get("misses") or 0) > 0:
                    lines.append(f"ccache: {cc.get('hits', 0)} hits / {cc.get('misses', 0)} misses")
                if entry.get("scons_cache_hits"):
                    lines.append(f"SCons cache hits: {entry['scons_cache_hits']}")
                tooltip.set_text("\n".join(lines))
                return True
        return False


# ------------------------------------------------------------------------------
#  Storage
# ------------------------------------------------------------------------------

def _plain_levelbar(css_class: str, offsets: tuple[tuple[str, float], ...] = ()) -> Gtk.LevelBar:
    """Continuous LevelBar without Adwaita's default low/high/full colouring."""
    bar = Gtk.LevelBar(min_value=0, max_value=1, value=0, valign=Gtk.Align.CENTER,
                       css_classes=[css_class])
    bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
    for name in ("low", "high", "full"):
        bar.remove_offset_value(name)
    for name, value in offsets:
        bar.add_offset_value(name, value)
    return bar


class StoragePage(Page):
    name = "storage"
    title = "Storage"
    STALE_SECONDS = 60

    def __init__(self, win):
        super().__init__(win)
        self.loading = False
        self.reload_pending = False
        self.loaded_at: float | None = None
        self.proc: BackendProcess | None = None
        self.got_data = False
        self.data_rows: list[Gtk.Widget] = []
        self.template_rows: list[Gtk.Widget] = []

        # Disk group ---------------------------------------------------------
        self.disk_group = Adw.PreferencesGroup(title="Disk")
        self.spinner = Adw.Spinner(visible=False)
        self.status_label = label("Measuring…", "dim-label", "caption", visible=False)
        self.refresh_button = button("", "view-refresh-symbolic", "flat", tooltip="Measure again",
                                     on_click=self.reload)
        suffix = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        for widget in (self.spinner, self.status_label, self.refresh_button):
            suffix.append(widget)
        self.disk_group.set_header_suffix(suffix)

        disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.disk_bar = _plain_levelbar("disk-bar", (("disk-ok", 0.85), ("disk-warn", 0.95), ("disk-full", 1.0)))
        self.disk_bar.set_hexpand(True)
        self.disk_caption = label("—", "dim-label", wrap=True)
        self.chips = Adw.WrapBox(child_spacing=6, line_spacing=6)
        for widget in (self.disk_bar, self.disk_caption, self.chips):
            disk_box.append(widget)
        self.disk_group.add(disk_box)

        # Data groups --------------------------------------------------------
        self.data_group = Adw.PreferencesGroup(title="Build Data",
                                               description="Caches, outputs and logs kept by the Build Manager.")
        self.template_group = Adw.PreferencesGroup(title="Export Templates",
                                                   description="Installed export template versions.")
        self.page = self.make_page(self.disk_group, self.data_group, self.template_group)
        self.set_child(self.page)

    # -- loading ---------------------------------------------------------------

    def shown(self):
        if self.loaded_at is None or time.monotonic() - self.loaded_at > self.STALE_SECONDS:
            self.reload()

    def reload(self):
        if self.loading:
            self.reload_pending = True
            return
        self.loading = True
        self.reload_pending = False
        self.got_data = False
        self.spinner.set_visible(True)
        self.status_label.set_visible(True)
        self.refresh_button.set_sensitive(False)
        self.proc = BackendProcess(["storage"], self._on_event, self._on_exit)

    def _on_event(self, ev):
        if ev.get("type") == "storage":
            self.got_data = True
            self._render(ev)

    def _on_exit(self, code):
        self.loading = False
        self.spinner.set_visible(False)
        self.status_label.set_visible(False)
        self.refresh_button.set_sensitive(True)
        if self.got_data:
            self.loaded_at = time.monotonic()
        else:
            self.win.toast("Could not measure storage usage")
        if self.reload_pending:
            self.reload()

    # -- rendering -------------------------------------------------------------

    def _render(self, ev):
        items = ev.get("items") or []
        self._render_disk(ev.get("disk"), ev.get("ccache"), items)

        for row in self.data_rows:
            self.data_group.remove(row)
        for row in self.template_rows:
            self.template_group.remove(row)
        self.data_rows, self.template_rows = [], []

        largest = max((i.get("size") or 0 for i in items), default=0) or 1
        build = sorted((i for i in items if i.get("group") != "templates"),
                       key=lambda i: i.get("size") or 0, reverse=True)
        for item in build:
            row = self._build_row(item, largest)
            self.data_group.add(row)
            self.data_rows.append(row)

        templates = [i for i in items if i.get("group") == "templates"]
        for item in templates:
            row = self._template_row(item)
            self.template_group.add(row)
            self.template_rows.append(row)
        if not templates:
            empty = action_row("No export templates installed", "Install them from the Export Templates page.",
                               "package-x-generic-symbolic")
            empty.add_css_class("dim-label")
            self.template_group.add(empty)
            self.template_rows.append(empty)

    def _render_disk(self, disk, ccache, items):
        clear_box(self.chips)
        if disk and disk.get("total"):
            total, used = disk["total"], disk.get("used") or 0
            self.disk_bar.set_value(min(1.0, used / total))
            self.disk_caption.set_text(f"{human_size(disk.get('free'))} free of {human_size(total)}")
            self.disk_bar.set_tooltip_text(disk.get("path") or "")
        else:
            self.disk_bar.set_value(0)
            self.disk_caption.set_text("Disk usage unavailable")
        managed = sum(i.get("size") or 0 for i in items)
        self.chips.append(chip(f"{human_size(managed)} managed", "accent"))
        if ccache:
            hits, misses = ccache.get("hits") or 0, ccache.get("misses") or 0
            if hits + misses:
                rate = round(100 * hits / (hits + misses))
                cc = chip(f"ccache {rate}% hits", "success" if rate >= 50 else "")
                cc.set_tooltip_text(f"{human_size(ccache.get('size'))} of {human_size(ccache.get('max_size'))}"
                                    f" used, {ccache.get('files') or 0} files")
                self.chips.append(cc)

    def _build_row(self, item, largest):
        size = item.get("size") or 0
        row = action_row(item.get("label", item.get("id", "")), item.get("description", ""))
        row.set_subtitle_lines(3)
        row.set_tooltip_text(item.get("path") or "")
        row.add_suffix(label(human_size(size), "numeric", "dim-label"))
        bar = _plain_levelbar("size-bar")
        bar.set_value(size / largest)
        bar.set_size_request(90, -1)
        row.add_suffix(bar)
        if item.get("clearable"):
            row.add_suffix(button("Clear…", None, "flat", on_click=lambda i=item: self._confirm_clear(i)))
        self._make_openable(row, item)
        return row

    def _template_row(self, item):
        row = action_row(item.get("label", item.get("id", "")), item.get("description", ""),
                         "package-x-generic-symbolic")
        row.set_subtitle_lines(2)
        row.set_tooltip_text(item.get("path") or "")
        if item.get("current"):
            row.add_suffix(chip("current", "accent"))
        row.add_suffix(label(human_size(item.get("size")), "numeric", "dim-label"))
        row.add_suffix(button("", "user-trash-symbolic", "flat", tooltip="Delete this template version",
                              on_click=lambda i=item: self._confirm_clear(i, delete=True)))
        self._make_openable(row, item)
        return row

    def _make_openable(self, row, item):
        path = item.get("path")
        if path:
            row.set_activatable(True)
            row.connect("activated", lambda *_: open_path(self.win, path))

    # -- clearing --------------------------------------------------------------

    def _confirm_clear(self, item, delete: bool = False):
        name = item.get("label", item.get("id", ""))
        body = f"{item.get('description', '')} Frees about {human_size(item.get('size'))}.".strip()
        if item.get("warning"):
            body += " The next build will take much longer."
        dialog = Adw.AlertDialog(heading=f"{'Delete' if delete else 'Clear'} {name}?",
                                 body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Delete" if delete else "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "clear" and self._clear(item, name))
        dialog.present(self.win)

    def _clear(self, item, name):
        self.win.run_heavy([Operation(f"Clear {name}", ["storage", "clear", item["id"]],
                                      icon_name="user-trash-symbolic")])


# ------------------------------------------------------------------------------
#  Main window
# ------------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "BuildManagerApp"):
        super().__init__(application=app, title=APP_NAME, default_width=1180, default_height=800,
                         width_request=360, height_request=420)
        self.app = app
        self.prefs = app.prefs
        self.status: dict = {}
        self.refreshing = False
        self.refresh_again = False
        self.busy = False
        self.queue: list[Operation] = []
        self.process: BackendProcess | None = None
        self.chain_title = ""
        self.chain_failed = False
        self.chain_interrupted = False
        self.cancel_timer = 0
        self.hidden_while_busy = False

        self._install_actions()

        # Sidebar ---------------------------------------------------------------
        self.sidebar_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.sidebar_list.set_header_func(self._sidebar_header)
        self.sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        self.sidebar_badges: dict[str, Gtk.Label] = {}
        for name, title, icons, section in PAGES:
            row = Gtk.ListBoxRow()
            row.page_name = name
            row.section = section
            box = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=2, margin_bottom=2)
            box.append(Gtk.Image(icon_name=icon(*icons)))
            box.append(label(title, hexpand=True, ellipsize=True))
            badge = label("", "sidebar-badge", xalign=0.5)
            badge.set_visible(False)
            box.append(badge)
            if name == "activity":
                self.sidebar_spinner = Adw.Spinner(visible=False)
                box.append(self.sidebar_spinner)
            row.set_child(box)
            self.sidebar_list.append(row)
            self.sidebar_rows[name] = row
            self.sidebar_badges[name] = badge
        self.sidebar_list.connect("row-activated", lambda _l, row: self.navigate(row.page_name))

        side_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         child=self.sidebar_list)
        side_box.append(side_scroll)

        # Mini activity card
        card = Gtk.Button(css_classes=["flat", "activity-card"])
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(spacing=8)
        top.append(Adw.Spinner())
        self.mini_title = label("Building…", "caption-heading", ellipsize=True, hexpand=True)
        top.append(self.mini_title)
        card_box.append(top)
        self.mini_progress = Gtk.ProgressBar()
        card_box.append(self.mini_progress)
        card.set_child(card_box)
        card.connect("clicked", lambda *_: self.navigate("activity"))
        self.mini_revealer = Gtk.Revealer(child=card, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        side_box.append(self.mini_revealer)

        side_toolbar = Adw.ToolbarView(content=side_box)
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(title="Godot", subtitle="Build Manager"))
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("_Refresh", "win.refresh")
        section.append("Run _Diagnostics", "win.run-doctor")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("_Keyboard Shortcuts", "win.shortcuts")
        section.append("_About Build Manager", "win.about")
        menu.append_section(None, section)
        side_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                            tooltip_text="Main Menu", primary=True))
        side_toolbar.add_top_bar(side_header)
        sidebar_page = Adw.NavigationPage(title="Godot Build Manager", tag="sidebar", child=side_toolbar)

        # Content -------------------------------------------------------------------
        self.overview = OverviewPage(self)
        self.source = SourcePage(self)
        self.editor = EditorPage(self)
        self.templates = TemplatesPage(self)
        self.encryption = EncryptionPage(self)
        self.activity = ActivityPage(self)
        self.logs = LogsPage(self)
        self.history = HistoryPage(self)
        self.diagnostics = DiagnosticsPage(self)
        self.storage = StoragePage(self)
        self.settings = SettingsPage(self)
        self.pages: dict[str, Page] = {p.name: p for p in (
            self.overview, self.source, self.editor, self.templates, self.encryption, self.activity,
            self.logs, self.history, self.diagnostics, self.storage, self.settings)}

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        for name, page in self.pages.items():
            self.stack.add_named(page, name)

        self.content_title = Adw.WindowTitle(title="Overview")
        content_header = Adw.HeaderBar()
        content_header.set_title_widget(self.content_title)
        refresh = button(None, "view-refresh-symbolic", "flat", tooltip="Refresh (Ctrl+R)", action="win.refresh")
        content_header.pack_end(refresh)
        self.header_spinner = Adw.Spinner(visible=False)
        content_header.pack_end(self.header_spinner)

        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner)
        self.banner_action = None

        self.needs_setup = False
        self.setup_page = Adw.StatusPage(
            icon_name=icon(APP_ID, "applications-engineering-symbolic", "system-run-symbolic"),
            title="Welcome to Godot Build Manager",
            description="Choose the Godot source checkout to build from, or clone the engine from GitHub. "
                        "Everything else can be changed later in Settings.")
        setup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        for text, icon_name, classes, handler in (
                ("Choose Godot Source Folder…", "folder-open-symbolic", ("suggested-action", "pill"),
                 self._choose_source),
                ("Clone Godot from GitHub…", "folder-download-symbolic", ("pill",), self._clone_source)):
            btn = button(text, icon_name, *classes, on_click=handler)
            btn.set_halign(Gtk.Align.CENTER)
            btn.set_size_request(300, -1)
            setup_box.append(btn)
        self.setup_hint = label("", "dim-label", "caption", wrap=True, xalign=0.5)
        self.setup_hint.set_max_width_chars(60)
        setup_box.append(self.setup_hint)
        self.setup_page.set_child(setup_box)
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.add_named(self.stack, "main")
        self.root_stack.add_named(self.setup_page, "setup")

        content_toolbar = Adw.ToolbarView(content=self.root_stack)
        content_toolbar.add_top_bar(content_header)
        content_toolbar.add_top_bar(self.banner)
        self.content_page = Adw.NavigationPage(title="Overview", tag="content", child=content_toolbar)

        self.split = Adw.NavigationSplitView(sidebar=sidebar_page, content=self.content_page,
                                             min_sidebar_width=220, max_sidebar_width=280)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.connect("close-request", self._on_close_request)
        self.navigate("overview")
        self.refresh()

    # --- actions ----------------------------------------------------------------

    def _install_actions(self):
        for name, callback in (
            ("refresh", lambda: self.refresh()),
            ("launch", lambda: self.launch(False)),
            ("launch-with-key", lambda: self.launch(True)),
            ("open-bin", self._open_bin),
            ("check-updates", lambda: (self.navigate("source"), self.source.check())),
            ("update", lambda: self.run_heavy(self.source.update_operations(), "Update Godot")),
            ("update-rebuild", self._update_rebuild),
            ("build-editor", lambda: self.run_heavy([self.editor.operation()])),
            ("build-templates", self._build_templates),
            ("run-doctor", lambda: (self.navigate("diagnostics"), self.diagnostics.run())),
            ("about", self._about),
            ("shortcuts", self._shortcuts),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)

    def set_action_enabled(self, name: str, enabled: bool):
        action = self.lookup_action(name)
        if action:
            action.set_enabled(enabled)

    def _sync_busy_actions(self):
        for name in ("update", "update-rebuild", "build-editor"):
            self.set_action_enabled(name, not self.busy)
        self.templates._sync()
        has_editor = bool(self.status.get("editor", {}).get("path"))
        self.set_action_enabled("launch", has_editor)
        self.set_action_enabled("launch-with-key", has_editor and self.status.get("key", {}).get("exists", False))
        self.set_action_enabled("open-bin", has_editor)

    def _update_rebuild(self):
        ops = [Operation("Update source", ["update"], icon_name="software-update-available-symbolic"),
               self.editor.operation()]
        self.run_heavy(ops, "Update and rebuild")

    def _build_templates(self):
        op = self.templates.operation()
        if op:
            self.run_heavy([op])
        else:
            self.navigate("templates")
            self.toast("Select at least one platform and target")

    # --- navigation ---------------------------------------------------------------

    def _sidebar_header(self, row, before):
        if before is None or before.section != row.section:
            header = label(row.section.upper(), "dim-label", "caption-heading")
            header.set_margin_start(14)
            header.set_margin_top(6 if before is None else 14)
            header.set_margin_bottom(4)
            row.set_header(header)
        else:
            row.set_header(None)

    def navigate(self, name: str):
        if name not in self.pages:
            return
        # Before a source tree is configured only pages that work without one are reachable.
        self.root_stack.set_visible_child_name(
            "setup" if self.needs_setup and name not in ("activity", "settings", "logs") else "main")
        self.stack.set_visible_child_name(name)
        page = self.pages[name]
        self.content_title.set_title(page.title)
        self.content_page.set_title(page.title)
        row = self.sidebar_rows[name]
        if self.sidebar_list.get_selected_row() is not row:
            self.sidebar_list.select_row(row)
        self.split.set_show_content(True)
        page.shown()

    def set_sidebar_badge(self, name: str, value):
        badge = self.sidebar_badges.get(name)
        if badge:
            badge.set_label(str(value) if value else "")
            badge.set_visible(bool(value))

    def toast(self, text: str, timeout: int = 3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    # --- status -----------------------------------------------------------------

    def refresh(self):
        if self.refreshing:
            self.refresh_again = True
            return
        self.refreshing = True
        self.header_spinner.set_visible(True)
        snapshot: dict = {}
        errors: list[str] = []
        setup: dict = {}

        def on_event(ev):
            if ev["type"] == "status":
                snapshot.update(ev)
            elif ev["type"] == "setup_required":
                setup.update(ev)
            elif ev["type"] in ("raw",) or (ev["type"] == "message" and ev.get("level") == "error"):
                errors.append(ev.get("text", ""))

        def on_exit(code):
            self.refreshing = False
            self.header_spinner.set_visible(False)
            if snapshot:
                self.apply_status(snapshot)
            elif setup:
                self.show_setup(setup)
            elif code != 0:
                self.toast(errors[-1] if errors else "Could not read the build status", 6)
            if self.refresh_again:
                self.refresh_again = False
                self.refresh()

        try:
            BackendProcess(["status"], on_event, on_exit)
        except GLib.Error as exc:
            self.refreshing = False
            self.header_spinner.set_visible(False)
            self.toast(f"Could not start godot_build.py: {exc.message}", 8)

    def show_setup(self, info: dict):
        self.needs_setup = True
        source = info.get("source")
        self.setup_hint.set_label(
            (f"The configured folder {source} is not a Godot checkout.\n" if source else "")
            + f"Settings are stored in {info.get('config', '')}")
        self.settings._apply_settings(info.get("settings", []), self.settings.manager_group)
        for name in ("update", "update-rebuild", "build-editor", "build-templates", "launch", "launch-with-key",
                     "open-bin", "check-updates"):
            self.set_action_enabled(name, False)
        self.banner.set_revealed(False)
        current = self.stack.get_visible_child_name()
        self.navigate(current if current in ("activity", "settings", "logs") else "overview")

    def _choose_source(self):
        dialog = Gtk.FileDialog(title="Choose the Godot Source Folder",
                                initial_folder=Gio.File.new_for_path(str(Path.home())))

        def chosen(dlg, result):
            try:
                path = dlg.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            errors: list[str] = []

            def on_event(ev):
                if ev["type"] == "message" and ev.get("level") == "error":
                    errors.append(ev.get("text", ""))

            def on_exit(code):
                if code == 0:
                    self.toast("Source tree set")
                    self.refresh()
                else:
                    self.toast(errors[0] if errors else "That folder is not a Godot checkout", 6)

            BackendProcess(["setup", "--source-dir", path], on_event, on_exit)

        dialog.select_folder(self, None, chosen)

    def _clone_source(self):
        dialog = Gtk.FileDialog(title="Choose Where to Clone Godot",
                                initial_folder=Gio.File.new_for_path(str(Path.home())))

        def chosen(dlg, result):
            try:
                parent = Path(dlg.select_folder_finish(result).get_path())
            except GLib.Error:
                return
            dest = parent / "godot"
            n = 2
            while dest.exists() and not (dest / "SConstruct").is_file():
                dest = parent / f"godot-{n}"
                n += 1
            self.run_heavy([Operation("Clone Godot", ["clone", str(dest)], icon_name="folder-download-symbolic")])

        dialog.select_folder(self, None, chosen)

    def apply_status(self, status: dict):
        if self.needs_setup:
            self.needs_setup = False
            self.root_stack.set_visible_child_name("main")
        for log in status.get("logs", []):
            log["title"] = " ".join(str(log.get("title") or log.get("name", "")).split())
        self.status = status
        for page in self.pages.values():
            try:
                page.update(status)
            except Exception as exc:
                print(f"{page.name}: update failed: {exc!r}", file=sys.stderr)
        behind = status.get("git", {}).get("behind")
        self.set_sidebar_badge("source", behind or None)
        version = status.get("version", {}).get("display", "")
        self._update_banner(status)
        self._sync_busy_actions()
        self.set_title(f"{APP_NAME} — Godot {version}" if version else APP_NAME)

    def _update_banner(self, status: dict):
        editor = status.get("editor", {})
        launcher = status.get("launcher", {})
        if self.busy:
            title, action, label_text = None, None, None
        elif not editor.get("path"):
            title, action, label_text = "No editor build found in bin/", "build-editor", "Build Editor"
        elif editor.get("mono") and not editor.get("godotsharp"):
            title, action, label_text = (".NET editor is missing its C# glue and assemblies", "build-editor",
                                         "Build Editor")
        elif not launcher.get("desktop_entry"):
            title, action, label_text = "Godot is not in your app launcher yet", "integrate", "Install"
        elif launcher.get("snapshot"):
            title, action, label_text = (f"Godot launches snapshot {launcher['snapshot'].split('_')[-1]}, "
                                         "not the latest build", "use-latest", "Use Latest")
        elif not launcher.get("current"):
            title, action, label_text = "~/.local/bin/godot does not point at the latest build", "integrate", "Repair"
        elif status.get("key", {}).get("exists") and not status.get("key", {}).get("backed_up"):
            title, action, label_text = ("Your encryption key has no backup — losing it means you can never "
                                         "patch games exported with it", "backup-key", "Back Up")
        else:
            title = None
        if title:
            self.banner.set_title(title)
            self.banner.set_button_label(label_text)
            self.banner_action = action
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _on_banner(self, _banner):
        if self.banner_action == "integrate":
            self.run_integrate(False)
        elif self.banner_action == "use-latest":
            self.editor._snapshot_cmd(["snapshot", "use", "latest"], "Godot launches the latest build again")
        elif self.banner_action == "backup-key":
            self.navigate("encryption")
            self.encryption.backup()
        elif self.banner_action:
            self.activate_action(f"win.{self.banner_action}", None)

    # --- heavy operations ---------------------------------------------------------

    def run_heavy(self, ops: list[Operation], title: str | None = None):
        ops = [op for op in ops if op]
        if not ops:
            return
        if self.busy:
            self.toast("Another operation is already running")
            self.navigate("activity")
            return
        self.busy = True
        self.queue = list(ops)
        self.chain_title = title or ops[0].title
        self.chain_failed = False
        self.chain_interrupted = False
        self.activity.begin(self.chain_title, ops[0].icon_name)
        self.sidebar_spinner.set_visible(True)
        self.mini_title.set_label(self.chain_title)
        self.mini_progress.set_fraction(0)
        self.mini_revealer.set_reveal_child(True)
        self.banner.set_revealed(False)
        self._sync_busy_actions()
        if self.prefs["follow_activity"]:
            self.navigate("activity")
        self._next_operation()

    def _next_operation(self):
        if not self.queue:
            self._chain_done()
            return
        op = self.queue.pop(0)
        self.activity.begin_operation(op)
        try:
            self.process = BackendProcess(op.args, self.activity.handle, lambda code, o=op: self._op_done(o, code))
        except GLib.Error as exc:
            self.activity.append(f"Could not start godot_build.py: {exc.message}", "error")
            self.chain_failed = True
            self.queue.clear()
            self._chain_done()

    def _op_done(self, op: Operation, code: int):
        self.process = None
        if self.cancel_timer:
            GLib.source_remove(self.cancel_timer)
            self.cancel_timer = 0
        if code == 130:
            self.chain_interrupted = True
            self.queue.clear()
        elif code != 0:
            self.chain_failed = True
            self.activity.append(f"{op.title} exited with code {code}", "error")
            self.queue.clear()
        self._next_operation()

    def _chain_done(self):
        self.busy = False
        ok = not (self.chain_failed or self.chain_interrupted)
        self.activity.end(ok, self.chain_interrupted, self.chain_title)
        self.sidebar_spinner.set_visible(False)
        self.mini_revealer.set_reveal_child(False)
        if ok:
            self.toast(f"{self.chain_title} finished")
        elif self.chain_interrupted:
            self.toast(f"{self.chain_title} cancelled")
        else:
            toast = Adw.Toast(title=f"{self.chain_title} failed", button_label="View", timeout=6)
            toast.connect("button-clicked", lambda *_: self.navigate("activity"))
            self.toasts.add_toast(toast)
        self._notify(ok)
        self.refresh()
        self.history.reload()
        if self.storage.loaded_at is not None:
            self.storage.reload()
        if self.hidden_while_busy:
            self.hidden_while_busy = False
            self.app.release()

    def _notify(self, ok: bool):
        if not self.prefs["notifications"] or (self.is_active() and self.get_visible()):
            return
        note = Gio.Notification.new(f"{self.chain_title} {'finished' if ok else 'failed'}"
                                    if not self.chain_interrupted else f"{self.chain_title} cancelled")
        note.set_body("Open the Build Manager for details." if not ok else
                      f"Completed in {self.activity.head_sub.get_label().replace('Took ', '')}.")
        note.set_icon(Gio.ThemedIcon.new(APP_ID))
        note.set_default_action("app.show-activity")
        note.set_priority(Gio.NotificationPriority.NORMAL if ok else Gio.NotificationPriority.HIGH)
        self.app.send_notification("build-finished", note)

    def sidebar_progress(self, fraction: float, title: str):
        self.mini_progress.set_fraction(fraction)

    def cancel(self):
        if not self.process:
            return
        self.activity.append("Cancelling… waiting for the compiler to stop", "warning")
        self.activity.cancel_button.set_sensitive(False)
        self.queue.clear()
        self.process.interrupt()

        def force():
            self.cancel_timer = 0
            if self.process and self.process.running:
                self.activity.append("Build did not stop in time, killing it", "error")
                self.process.kill()
            return False

        self.cancel_timer = GLib.timeout_add_seconds(CANCEL_GRACE_SECONDS, force)

    def run_integrate(self, remove: bool):
        args = ["integrate"] + (["--remove"] if remove else [])
        self.run_heavy([Operation("Remove desktop integration" if remove else "Install desktop integration", args,
                                  icon_name="view-app-grid-symbolic")])

    # --- misc -------------------------------------------------------------------

    def launch(self, with_key: bool):
        launcher = self.status.get("launcher", {})
        editor = self.status.get("editor", {})
        target = launcher.get("path") if launcher.get("current") else editor.get("path")
        if not target:
            self.toast("No editor build to launch")
            return
        env = dict(os.environ)
        if with_key:
            key = self.encryption.read_key()
            if not key:
                self.toast("No encryption key to pass to the editor")
                return
            env["GODOT_SCRIPT_ENCRYPTION_KEY"] = key
        try:
            subprocess.Popen([target], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(Path.home()))
            self.toast("Launching Godot with the encryption key" if with_key else "Launching Godot")
        except OSError as exc:
            self.toast(f"Could not launch Godot: {exc.strerror}")

    def _open_bin(self):
        path = self.status.get("editor", {}).get("path")
        if path:
            open_path(self, path, folder=True)

    def _about(self):
        version = self.status.get("version", {}).get("display", "")
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=APP_VERSION,
            developer_name="Ashton Tink", comments="Builds the Godot editor, export templates for Linux, Windows "
                                                   "and macOS, and manages PCK encryption — from your own source tree.",
            website=REPO_URL, issue_url=f"{REPO_URL}/issues",
            license_type=Gtk.License.MIT_X11,
            debug_info=json.dumps({k: self.status.get(k) for k in ("version", "git", "editor", "platforms")},
                                  indent=2, default=str),
            debug_info_filename="godot-build-manager-debug.json")
        about.add_link("Godot Engine", "https://godotengine.org")
        about.add_link("Compiling Godot", "https://docs.godotengine.org/en/stable/engine_details/development/compiling/")
        about.add_link("Compiling with PCK encryption",
                       "https://docs.godotengine.org/en/stable/engine_details/development/compiling/"
                       "compiling_with_script_encryption_key.html")
        about.set_release_notes_version(APP_VERSION)
        about.set_release_notes(RELEASE_NOTES)
        if version:
            about.set_comments(about.get_comments() + f"\n\nManaging Godot {version} at {self.status.get('source', '')}")
        about.present(self)

    def _shortcuts(self):
        if not hasattr(Adw, "ShortcutsDialog"):
            return
        dialog = Adw.ShortcutsDialog()
        section = Adw.ShortcutsSection(title="General")
        for title, accel in (("Refresh status", "<Control>r"), ("Launch Godot", "<Control>l"),
                             ("Build editor", "<Control>b"), ("Build templates", "<Control><Shift>b"),
                             ("Check for updates", "<Control>u"), ("Go to page 1–9", "<Alt>1...9"),
                             ("Keyboard shortcuts", "<Control>question"), ("Quit", "<Control>q")):
            section.add(Adw.ShortcutsItem(title=title, accelerator=accel))
        dialog.add(section)
        dialog.present(self)

    def _on_close_request(self, _win):
        if not self.busy:
            return False
        dialog = Adw.AlertDialog(heading="A Build Is Running",
                                 body="You can keep it running in the background and get a notification when "
                                      "it finishes, or cancel it now.")
        dialog.add_response("stay", "_Keep Window Open")
        dialog.add_response("cancel", "_Cancel Build")
        dialog.add_response("background", "Run in _Background")
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("background", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("background")
        dialog.set_close_response("stay")

        def on_response(_d, response):
            if response == "background":
                self.hidden_while_busy = True
                self.app.hold()
                self.set_visible(False)
            elif response == "cancel":
                self.cancel()

        dialog.connect("response", on_response)
        dialog.present(self)
        return True


# ------------------------------------------------------------------------------
#  Application
# ------------------------------------------------------------------------------

class BuildManagerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)
        self.prefs = Prefs()
        self.window: MainWindow | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.window.close() if self.window else self.quit())
        self.add_action(quit_action)
        show_activity = Gio.SimpleAction.new("show-activity", None)
        show_activity.connect("activate", lambda *_: self._present("activity"))
        self.add_action(show_activity)

        for action, accels in (
            ("app.quit", ["<Control>q"]),
            ("win.refresh", ["<Control>r", "F5"]),
            ("win.launch", ["<Control>l"]),
            ("win.build-editor", ["<Control>b"]),
            ("win.build-templates", ["<Control><Shift>b"]),
            ("win.check-updates", ["<Control>u"]),
            ("win.shortcuts", ["<Control>question"]),
        ):
            self.set_accels_for_action(action, accels)
        for i, (name, *_rest) in enumerate(PAGES[:9], 1):
            act = Gio.SimpleAction.new(f"page-{name}", None)
            act.connect("activate", lambda *_a, n=name: self.window and self.window.navigate(n))
            self.add_action(act)
            self.set_accels_for_action(f"app.page-{name}", [f"<Alt>{i}"])

    def _present(self, page: str | None = None):
        if self.window is None:
            self.window = MainWindow(self)
        if not self.window.get_visible() and self.window.hidden_while_busy:
            self.window.hidden_while_busy = False
            self.release()
        if page:
            self.window.navigate(page)
        self.window.present()

    def do_activate(self):
        self._present()

    def do_command_line(self, command_line):
        parser = argparse.ArgumentParser(prog="godot_build_gui.py", add_help=False, exit_on_error=False)
        parser.add_argument("--page", choices=[p[0] for p in PAGES])
        parser.add_argument("--check-updates", action="store_true")
        try:
            args, _unknown = parser.parse_known_args(command_line.get_arguments()[1:])
        except (argparse.ArgumentError, SystemExit):
            args = argparse.Namespace(page=None, check_updates=False)
        self._present(args.page)
        if args.check_updates:
            self.window.navigate("source")
            self.window.source.check()
        return 0


def main() -> int:
    if (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) < (1, 7):
        print(f"error: libadwaita 1.7 or newer is required (found {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION})",
              file=sys.stderr)
        return 1
    if not BACKEND.is_file():
        print(f"error: backend not found: {BACKEND}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return BuildManagerApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
