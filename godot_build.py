#!/usr/bin/env python3
"""
godot_build.py - Godot Engine production build pipeline
=======================================================

Part of Godot Build Manager: https://github.com/T1nkiePlayz/godot-build-manager

One tool for everything between a Godot source checkout and a shippable game:

  * Optimized editor build for this machine (with .NET glue + assemblies)
  * Export templates for Linux, Windows (llvm-mingw) and macOS (osxcross,
    Universal 2), release + debug, packaged exactly as the editor expects
  * AES-256 PCK encryption: key generation, injection at build time, and
    verification that every template really carries the key
  * Install into the editor's template folder, or package a .tpz
  * Manifest with git commit, key fingerprint and SHA-256 of every file

Build options themselves (LTO, optimization, toolchains) live in the source
tree's custom.py; this script decides *what* to build and packages it.

Paths (source tree, workspace, cross toolchains) live in
$XDG_CONFIG_HOME/godot-build-manager/config.json; set them with
`godot_build.py setup --source-dir PATH` or from the GUI.

Usage:
    godot_build.py                       interactive menu
    godot_build.py setup --source-dir PATH   point the tool at a Godot checkout
    godot_build.py clone DIR             clone Godot from GitHub and use it
    godot_build.py doctor                check toolchains and dependencies
    godot_build.py editor                build the Linux editor
    godot_build.py templates --install   build every available platform
    godot_build.py templates -p windows -t release
    godot_build.py key generate          create the PCK encryption key
    godot_build.py all                   editor + templates + install
    godot_build.py <command> --help      options for a command

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import mmap
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# ==============================================================================
#  Configuration
# ==============================================================================

APP_SLUG = "godot-build-manager"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
CONFIG_DIR = Path(os.environ.get("GBM_CONFIG_DIR") or XDG_CONFIG / APP_SLUG)
MANAGER_FILE = CONFIG_DIR / "config.json"


def _stored_config() -> dict:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(MANAGER_FILE.read_text())
        if isinstance(data, dict):
            return data
    return {}


def _path_setting(env: str, key: str, default: str = "") -> Path | None:
    value = os.environ.get(env) or _stored_config().get(key) or default
    return Path(value).expanduser() if value else None


def _first_dir(*candidates: object) -> Path | None:
    for candidate in candidates:
        if candidate and Path(str(candidate)).expanduser().is_dir():
            return Path(str(candidate)).expanduser()
    return None


# Godot checkout to build. Empty until `setup` (or the GUI) configures it.
SOURCE_DIR = _path_setting("GODOT_SOURCE", "source_dir") or Path()
# Everything this tool produces: logs, staged templates, snapshots, the key.
WORKSPACE = _path_setting("GBM_WORKSPACE", "workspace_dir") or XDG_DATA / APP_SLUG
GODOT_ROOT = WORKSPACE  # disk-usage reports are measured here

KEY_FILE = WORKSPACE / "keys" / "godot.gdkey"
LOG_DIR = WORKSPACE / "logs"
STATE_FILE = LOG_DIR / ".state.json"
DIST_DIR = WORKSPACE / "dist"
NUGET_LOCAL_DIR = WORKSPACE / "nuget"
TOOLCHAIN_DIR = WORKSPACE / "toolchains"  # what `toolchain install` downloads and builds

_mingw_clang = shutil.which("x86_64-w64-mingw32-clang")
LLVM_MINGW_DIR = (_path_setting("MINGW_PREFIX", "llvm_mingw")
                  or _first_dir(TOOLCHAIN_DIR / "llvm-mingw", "/opt/llvm-mingw", Path.home() / "llvm-mingw")
                  or (Path(_mingw_clang).resolve().parent.parent if _mingw_clang else Path("/opt/llvm-mingw")))
OSXCROSS_CANDIDATES = [
    os.environ.get("OSXCROSS_ROOT", ""),
    str(_stored_config().get("osxcross_root", "")),
    str(TOOLCHAIN_DIR / "osxcross"),
    str(Path.home() / "osxcross"),
]

USER_TEMPLATES_DIR = XDG_DATA / "godot" / "export_templates"

_MACHINE = os.uname().machine.lower()
HOST_ARCH = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(_MACHINE, _MACHINE)

MIN_FREE_DISK_GB = 25
FAILURE_CONTEXT_LINES = 25

# Desktop integration (launcher symlink, .desktop entries, icons, MIME type).
LOCAL_BIN = Path.home() / ".local" / "bin"
APPS_DIR = XDG_DATA / "applications"
DESKTOP_FILE = APPS_DIR / "org.godotengine.Godot.desktop"
EDITOR_ALIAS_FILE = APPS_DIR / "org.godotengine.Editor.desktop"  # Godot's Wayland app_id once a project is open
MANAGER_APP_ID = "io.github.t1nkieplayz.GodotBuildManager"
MANAGER_DESKTOP_FILE = APPS_DIR / f"{MANAGER_APP_ID}.desktop"
ICONS_DIR = XDG_DATA / "icons" / "hicolor" / "scalable"
MIME_DIR = XDG_DATA / "mime"
INTEGRATION_RECORD = WORKSPACE / ".integration.json"
GUI_SCRIPT = Path(__file__).resolve().with_name("godot_build_gui.py")
BACKEND_SCRIPT = Path(__file__).resolve()
CUSTOM_PY_TEMPLATE = Path(__file__).resolve().with_name("custom.py.example")
SNAPSHOT_DIR = WORKSPACE / "snapshots"
HISTORY_FILE = LOG_DIR / "history.jsonl"


# ==============================================================================
#  Terminal output
# ==============================================================================


class Style:
    enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    @classmethod
    def wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def bold(cls, t: str) -> str: return cls.wrap("1", t)
    @classmethod
    def dim(cls, t: str) -> str: return cls.wrap("2", t)
    @classmethod
    def red(cls, t: str) -> str: return cls.wrap("31", t)
    @classmethod
    def green(cls, t: str) -> str: return cls.wrap("32", t)
    @classmethod
    def yellow(cls, t: str) -> str: return cls.wrap("33", t)
    @classmethod
    def blue(cls, t: str) -> str: return cls.wrap("34", t)
    @classmethod
    def magenta(cls, t: str) -> str: return cls.wrap("35", t)
    @classmethod
    def cyan(cls, t: str) -> str: return cls.wrap("36", t)


S = Style
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ICON = {"ok": "✔", "fail": "✖", "warn": "▲", "info": "●", "skip": "○", "arrow": "›"}


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def term_width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns, 140))


# --json switches every message to one JSON object per line on stdout, so a
# front end (godot_build_gui.py) can drive the pipeline without scraping text.
JSON_MODE = False


def emit(event_type: str, /, **data: object) -> None:
    if JSON_MODE:
        sys.stdout.write(json.dumps({"type": event_type, **data}, default=str) + "\n")
        sys.stdout.flush()


def _say(level: str, text: str, rendered: str) -> None:
    if JSON_MODE:
        emit("message", level=level, text=ANSI_RE.sub("", text))
    else:
        print(rendered)


def ok(msg: str) -> None: _say("ok", msg, f"  {S.green(ICON['ok'])} {msg}")
def fail(msg: str) -> None: _say("error", msg, f"  {S.red(ICON['fail'])} {msg}")
def warn(msg: str) -> None: _say("warning", msg, f"  {S.yellow(ICON['warn'])} {msg}")
def info(msg: str) -> None: _say("info", msg, f"  {S.blue(ICON['info'])} {msg}")
def skip(msg: str) -> None: _say("skip", msg, f"  {S.dim(ICON['skip'] + ' ' + msg)}")
def detail(msg: str) -> None: _say("detail", msg, f"    {S.dim(msg)}")


def heading(title: str) -> None:
    if JSON_MODE:
        emit("heading", text=title)
        return
    width = term_width()
    print()
    print(S.bold(S.magenta(f"━━ {title} ")) + S.magenta("━" * max(0, width - len(title) - 4)))


def banner(ctx: "Context") -> None:
    if JSON_MODE:
        return
    width = min(term_width(), 78)
    inner = width - 4
    rows = [
        S.bold("Godot Build Pipeline") + S.dim("  ·  production"),
        "",
        f"{S.dim('version ')} {ctx.version.display}",
        f"{S.dim('source  ')} {ctx.source}",
        f"{S.dim('commit  ')} {ctx.git_summary}",
        f"{S.dim('key     ')} {key_status_line()}",
    ]
    print(S.magenta("╭" + "─" * (width - 2) + "╮"))
    for row in rows:
        overflow = visible_len(row) - inner
        if overflow > 0:  # long paths: keep the start and the meaningful tail
            plain = ANSI_RE.sub("", row)
            row = plain[:10] + "…" + plain[10 + overflow + 1:]
        pad = inner - visible_len(row)
        print(S.magenta("│ ") + row + " " * max(0, pad) + S.magenta(" │"))
    print(S.magenta("╰" + "─" * (width - 2) + "╯"))


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GB"


def confirm(question: str, ctx: "Context", default: bool = False) -> bool:
    if ctx.assume_yes:
        return True
    if not sys.stdin.isatty():
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {S.yellow('?')} {question} {S.dim(f'[{hint}]')} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")


class SetupRequired(Exception):
    """No usable Godot source tree is configured yet."""

    def __init__(self, source: Path) -> None:
        configured = bool(str(source)) and str(source) != "."
        super().__init__(f"Not a Godot source tree: {source}" if configured else
                         "No Godot source tree configured. Run: godot_build.py setup --source-dir PATH "
                         "(or godot_build.py clone DIR)")
        self.source = source if configured else None


class BuildError(Exception):
    """A failure that has already been explained to the user."""


# ==============================================================================
#  Platforms and build jobs
# ==============================================================================


@dataclass(frozen=True)
class Platform:
    key: str  # CLI name
    scons: str  # SCons platform name
    label: str
    default_arches: tuple[str, ...]
    supported_arches: tuple[str, ...]


PLATFORMS: dict[str, Platform] = {
    "linux": Platform("linux", "linuxbsd", "Linux", (HOST_ARCH,), (HOST_ARCH,)),
    "windows": Platform("windows", "windows", "Windows", ("x86_64",), ("x86_64", "arm64")),
    "macos": Platform("macos", "macos", "macOS", ("arm64", "x86_64"), ("arm64", "x86_64")),
}

TARGETS = {"release": "template_release", "debug": "template_debug"}


@dataclass
class Job:
    platform: Platform
    target: str  # editor | template_release | template_debug
    arch: str
    mono: bool
    encrypt: bool
    status: str = "pending"  # pending | ok | failed | skipped | interrupted
    seconds: float = 0.0
    output: Path | None = None
    log: Path | None = None

    @property
    def kind(self) -> str:
        return {"template_release": "release", "template_debug": "debug"}.get(self.target, self.target)

    @property
    def state_key(self) -> str:
        return f"{self.platform.scons}.{self.target}.{self.arch}{'.mono' if self.mono else ''}"

    def describe(self) -> str:
        return f"{self.platform.label:<8} {self.target:<17} {self.arch:<7}"

    def as_dict(self) -> dict:
        return {
            "id": self.state_key, "platform": self.platform.key, "platform_label": self.platform.label,
            "target": self.target, "kind": self.kind, "arch": self.arch, "mono": self.mono,
            "encrypt": self.encrypt, "status": self.status, "seconds": round(self.seconds, 1),
            "output": str(self.output) if self.output else None,
            "size": self.output.stat().st_size if self.output and self.output.exists() else None,
            "log": str(self.log) if self.log else None,
        }

    def scons_args(self, ctx: "Context") -> list[str]:
        args = [
            f"platform={self.platform.scons}",
            f"target={self.target}",
            f"arch={self.arch}",
            f"module_mono_enabled={'yes' if self.mono else 'no'}",
            "progress=no",
        ]
        if self.platform.key == "macos":
            if ctx.osxcross_sdk:
                args.append(f"osxcross_sdk={ctx.osxcross_sdk}")
            args += macos_vulkan_args(ctx.source)
        if ctx.jobs:
            args.append(f"-j{ctx.jobs}")
        return args + list(ctx.extra_scons_args)


# ==============================================================================
#  Version, git and toolchain detection
# ==============================================================================


@dataclass
class Version:
    major: int
    minor: int
    patch: int
    status: str
    module_config: str

    @classmethod
    def read(cls, source: Path) -> "Version":
        values: dict[str, object] = {}
        tree = ast.parse((source / "version.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                with contextlib.suppress(ValueError):
                    values[node.targets[0].id] = ast.literal_eval(node.value)
        status = os.environ.get("GODOT_VERSION_STATUS") or str(values.get("status", "custom"))
        return cls(
            int(values["major"]), int(values["minor"]), int(values.get("patch", 0)),
            status, str(values.get("module_config", "")),
        )

    @property
    def number(self) -> str:
        base = f"{self.major}.{self.minor}"
        return f"{base}.{self.patch}" if self.patch else base

    @property
    def display(self) -> str:
        return f"{self.number}-{self.status}"

    def template_dir_name(self, mono: bool) -> str:
        """Folder name the editor looks for, e.g. '4.8.beta.mono'."""
        return f"{self.number}.{self.status}{self.module_config}{'.mono' if mono else ''}"


def git(source: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(source), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def find_osxcross() -> tuple[Path | None, str]:
    for candidate in OSXCROSS_CANDIDATES:
        if not candidate:
            continue
        root = Path(candidate).expanduser()
        clangs = sorted((root / "target" / "bin").glob("x86_64-apple-darwin*-clang"))
        if clangs:
            match = re.search(r"(darwin[\d.]+)", clangs[-1].name)
            return root, match.group(1) if match else ""
    return None, ""


def custom_py_flag(source: Path, name: str, default: bool) -> bool:
    """Read a `_NAME = True/False` toggle from custom.py without executing it."""
    with contextlib.suppress(OSError):
        match = re.search(rf"^{name}\s*=\s*(True|False)\b", (source / "custom.py").read_text(), re.M)
        if match:
            return match.group(1) == "True"
    return default


def custom_py_value(source: Path, name: str) -> str:
    with contextlib.suppress(OSError):
        match = re.search(rf'^{name}\s*=\s*"([^"]*)"', (source / "custom.py").read_text(), re.M)
        if match:
            return match.group(1)
    return ""


@dataclass
class Context:
    source: Path
    jobs: int | None
    verbose: bool
    dry_run: bool
    assume_yes: bool
    extra_scons_args: Sequence[str] = ()
    version: Version = field(init=False)
    git_summary: str = field(init=False)
    git_commit: str = field(init=False)
    osxcross_root: Path | None = field(init=False)
    osxcross_sdk: str = field(init=False)
    mono_default: bool = field(init=False)

    def __post_init__(self) -> None:
        if not str(self.source) or str(self.source) == "." or not (self.source / "SConstruct").is_file():
            raise SetupRequired(self.source)
        self.version = Version.read(self.source)
        self.git_commit = git(self.source, "rev-parse", "HEAD")
        branch = git(self.source, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        dirty = " " + S.yellow("(modified)") if git(self.source, "status", "--porcelain", "-uno") else ""
        self.git_summary = f"{self.git_commit[:10] or 'unknown'} on {branch}{dirty}"
        self.osxcross_root, self.osxcross_sdk = find_osxcross()
        self.mono_default = custom_py_flag(self.source, "_DOTNET", False)

    @property
    def bin_dir(self) -> Path:
        return self.source / "bin"

    def platform_available(self, platform: Platform) -> tuple[bool, str]:
        if platform.key == "linux":
            return (bool(shutil.which("clang") or shutil.which("gcc")), "no C++ compiler (clang or gcc) found")
        if platform.key == "windows":
            clang = LLVM_MINGW_DIR / "bin" / "x86_64-w64-mingw32-clang"
            return (clang.exists(), "llvm-mingw not installed (Diagnostics → Toolchains, or: godot-build toolchain install mingw)")
        if platform.key == "macos":
            if not self.osxcross_root:
                return (False, "osxcross not installed (Diagnostics → Toolchains, or: godot-build toolchain install osxcross)")
            if not lipo_tool(self):
                return (False, "no lipo tool (osxcross lipo or llvm-lipo)")
            return (True, "")
        return (False, "unknown platform")

    def build_env(self, job: Job | None) -> dict[str, str]:
        env = os.environ.copy()
        # SCons is Python: without this its output arrives in 8 KB bursts when
        # piped, and the live progress line would freeze.
        env["PYTHONUNBUFFERED"] = "1"
        # Never let a key leak into builds that shouldn't carry it.
        for var in ("SCRIPT_AES256_ENCRYPTION_KEY", "GODOT_SCRIPT_ENCRYPTION_KEY"):
            env.pop(var, None)
        if job and job.encrypt:
            env["SCRIPT_AES256_ENCRYPTION_KEY"] = read_key()
        if job and job.platform.key == "windows":
            env["MINGW_PREFIX"] = str(LLVM_MINGW_DIR)
        if job and job.platform.key == "macos" and self.osxcross_root:
            env["OSXCROSS_ROOT"] = str(self.osxcross_root)
            env["PATH"] = f"{self.osxcross_root / 'target' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        return env


def lipo_tool(ctx: Context) -> str | None:
    if ctx.osxcross_root:
        for cand in sorted((ctx.osxcross_root / "target" / "bin").glob("*-apple-darwin*-lipo")):
            return str(cand)
    return shutil.which("lipo") or shutil.which("llvm-lipo")


# ==============================================================================
#  Encryption key
# ==============================================================================

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def read_key() -> str:
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not HEX64.match(key):
        raise BuildError(f"Encryption key in {KEY_FILE} is not 64 hex characters.")
    return key.lower()


def key_exists() -> bool:
    return KEY_FILE.is_file()


def key_fingerprint(key: str | None = None) -> str:
    """Short, non-reversible identifier for comparing keys without revealing them."""
    key = key or read_key()
    return hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16]


def key_status_line() -> str:
    if not key_exists():
        return S.yellow("none") + S.dim("  (templates will not be encrypted)")
    try:
        return S.green("present") + S.dim(f"  fingerprint {key_fingerprint()}")
    except BuildError:
        return S.red("invalid")


def binary_contains_key(path: Path, key: str) -> bool:
    needle = bytes.fromhex(key)
    with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        return mm.find(needle) != -1


# ==============================================================================
#  Build runner (live progress, logs, failure summaries)
# ==============================================================================

PROGRESS_RE = re.compile(r"^(Compiling|Linking|Generating|Building|Ranlib|Archiving)\b")
ERROR_RE = re.compile(
    r"(error[:\s]|fatal error|undefined (symbol|reference)|\*\*\* \[|Error \d+|Traceback|ERROR:)", re.I
)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def load_state() -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


class LiveStatus:
    """Single repainting status line for one SCons run (TTY only)."""

    def __init__(self, title: str, expected_steps: int):
        self.title = title
        self.expected = expected_steps
        self.steps = 0
        self.message = "starting"
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.tty = sys.stdout.isatty()

    def __enter__(self) -> "LiveStatus":
        if self.tty:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self.tty:
            self._thread.join()
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    def update(self, line: str, steps: int) -> None:
        with self._lock:
            self.steps = steps
            self.message = line

    def _render(self, frame: int) -> str:
        width = term_width()
        elapsed = fmt_duration(time.monotonic() - self.started)
        spin = S.cyan(SPINNER[frame % len(SPINNER)])
        with self._lock:
            steps, msg = self.steps, self.message
        if self.expected:
            pct = min(steps / self.expected, 0.99)
            filled = int(pct * 20)
            bar = S.cyan("█" * filled) + S.dim("░" * (20 - filled))
            progress = f"{bar} {int(pct * 100):3d}%"
        else:
            progress = S.dim(f"{steps:>5} steps ")
        head = f"  {spin} {S.bold(self.title)}  {progress}  {S.dim(elapsed)}  "
        room = width - visible_len(head) - 1
        msg = msg.replace(str(SOURCE_DIR) + "/", "")
        tail = S.dim(msg[: max(0, room - 1)] + ("…" if len(msg) > room else "")) if room > 10 else ""
        return head + tail

    def _loop(self) -> None:
        frame = 0
        while not self._stop.wait(0.12):
            sys.stdout.write("\r\033[2K" + self._render(frame))
            sys.stdout.flush()
            frame += 1


def run_logged(cmd: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, title: str,
               state_key: str | None, ctx: Context, task_id: str | None = None) -> bool:
    """Run a command, tee to a log, show live progress. Returns success."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state()
    expected = int(state.get("steps", {}).get(state_key, 0)) if state_key else 0
    task_id = task_id or state_key or title
    tail: list[str] = []
    errors: list[str] = []
    steps = 0
    started = time.monotonic()
    is_scons = bool(cmd) and Path(cmd[0]).name == "scons"
    cache_before = ccache_stats() if is_scons else None
    retrieved = 0

    def remember(code: int) -> None:
        cache = None
        if cache_before is not None and (after := ccache_stats()):
            cache = {"hits": after["hits"] - cache_before["hits"],
                     "misses": after["misses"] - cache_before["misses"]}
        record_history({"time": dt.datetime.now().isoformat(timespec="seconds"), "id": task_id,
                        "key": state_key, "title": " ".join(title.split()), "ok": code == 0, "code": code,
                        "seconds": round(time.monotonic() - started, 1), "steps": steps,
                        "commit": ctx.git_commit[:10], "ccache": cache, "scons_cache_hits": retrieved,
                        "log": str(log_path)})

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# {dt.datetime.now().isoformat(timespec='seconds')}\n# title: {title}\n"
                  f"# cwd: {cwd}\n# $ {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, start_new_session=True,
        )
        emit("task_start", id=task_id, title=title, log=str(log_path), expected=expected,
             expected_seconds=expected_seconds(state_key))
        status = LiveStatus(title, expected) if not (ctx.verbose or JSON_MODE) else None
        if not JSON_MODE and (ctx.verbose or not sys.stdout.isatty()):
            info(f"{title} {S.dim('… log: ' + str(log_path))}")
        try:
            with status or contextlib.nullcontext():
                assert proc.stdout is not None
                for raw in proc.stdout:
                    log.write(raw)
                    line = ANSI_RE.sub("", raw).rstrip("\r\n")
                    if not line.strip():
                        continue
                    if PROGRESS_RE.match(line):
                        steps += 1
                    elif RETRIEVED_RE.match(line):
                        retrieved += 1
                    tail = (tail + [line])[-FAILURE_CONTEXT_LINES:]
                    is_error = bool(ERROR_RE.search(line))
                    if is_error and len(errors) < 40:
                        errors.append(line)
                    if JSON_MODE:
                        emit("line", id=task_id, text=line, steps=steps, expected=expected, error=is_error)
                    elif ctx.verbose:
                        print("    " + S.dim(line))
                    elif status:
                        status.update(line, steps)
                code = proc.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            log.write("\n# interrupted by user\n")
            emit("task_end", id=task_id, ok=False, code=130, interrupted=True)
            remember(130)
            raise

        log.write(f"\n# exit code {code}\n")

    remember(code)
    emit("task_end", id=task_id, ok=code == 0, code=code, steps=steps)
    if code == 0:
        if state_key and steps > expected:
            state.setdefault("steps", {})[state_key] = steps
            save_state(state)
        return True

    fail(f"{title} failed (exit {code})")
    shown = errors or tail
    label = "first errors" if errors else "last output"
    if JSON_MODE:
        emit("failure", id=task_id, label=label, lines=shown[:FAILURE_CONTEXT_LINES], log=str(log_path))
        return False
    print(f"    {S.dim('┌─ ' + label)}")
    for line in shown[:FAILURE_CONTEXT_LINES]:
        print(f"    {S.dim('│')} {S.red(line) if ERROR_RE.search(line) else line}")
    print(f"    {S.dim('└─ full log:')} {log_path}")
    return False


def new_log_path(name: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return LOG_DIR / f"{stamp}_{name}.log"


@contextlib.contextmanager
def build_lock(ctx: Context) -> Iterator[None]:
    """Prevent two builds from racing in the same source tree."""
    ctx.bin_dir.mkdir(exist_ok=True)
    lock_path = ctx.bin_dir / ".godot_build.lock"
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another build is already running in this source tree.")
            raise SystemExit(1)
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def notify(summary: str, body: str) -> None:
    if JSON_MODE:  # the GUI shows its own notification
        return
    if shutil.which("notify-send"):
        with contextlib.suppress(OSError):
            subprocess.Popen(["notify-send", "-a", "Godot Build", summary, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ==============================================================================
#  Locating build outputs
# ==============================================================================

IGNORED_SUFFIXES = (".debug", ".pdb", ".exp", ".lib", ".a", ".dSYM", ".zip")


def find_binary(ctx: Context, job: Job, console: bool = False) -> Path | None:
    prefix = f"godot.{job.platform.scons}.{job.target}.{job.arch}"
    found: list[Path] = []
    for path in ctx.bin_dir.glob(prefix + "*"):
        name = path.name
        rest = name[len(prefix):]
        if not path.is_file() or name.endswith(IGNORED_SUFFIXES):
            continue
        if any(tag in rest for tag in (".double", ".dev", ".san", ".fat", ".universal")):
            continue
        if (".mono" in rest) != job.mono:
            continue
        if job.platform.key == "windows":
            if not name.endswith(".exe") or (".console" in rest) != console:
                continue
        found.append(path)
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# ==============================================================================
#  Template staging, manifest and packaging
# ==============================================================================


def staging_dir(ctx: Context, mono: bool) -> Path:
    return DIST_DIR / "templates" / ctx.version.template_dir_name(mono)


def work_dir(ctx: Context, mono: bool) -> Path:
    return DIST_DIR / "work" / ctx.version.template_dir_name(mono)


def manifest_path(ctx: Context, mono: bool) -> Path:
    return DIST_DIR / f"{ctx.version.template_dir_name(mono)}.manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(ctx: Context, mono: bool) -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(manifest_path(ctx, mono).read_text())
    return {"files": {}}


def record_file(ctx: Context, mono: bool, path: Path, encrypted: bool) -> None:
    manifest = load_manifest(ctx, mono)
    manifest.update({
        "version": ctx.version.template_dir_name(mono),
        "git_commit": ctx.git_commit,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    manifest["files"][path.name] = {
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "key_fingerprint": key_fingerprint() if encrypted else None,
        "git_commit": ctx.git_commit,
        "built": dt.datetime.now().isoformat(timespec="seconds"),
    }
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path(ctx, mono).write_text(json.dumps(manifest, indent=2, sort_keys=True))


def copy_executable(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    tmp.chmod(0o755)
    tmp.replace(dst)
    os.utime(dst)  # staged-at time: a partial install picks only this run's files


def stage_job(ctx: Context, job: Job) -> None:
    """Copy one finished template into the staging folder under its export name."""
    assert job.output is not None
    stage = staging_dir(ctx, job.mono)
    if job.platform.key == "linux":
        dst = stage / f"linux_{job.kind}.{job.arch}"
        copy_executable(job.output, dst)
        record_file(ctx, job.mono, dst, job.encrypt)
    elif job.platform.key == "windows":
        dst = stage / f"windows_{job.kind}_{job.arch}.exe"
        copy_executable(job.output, dst)
        record_file(ctx, job.mono, dst, job.encrypt)
        console = find_binary(ctx, job, console=True)
        if console:
            con_dst = stage / f"windows_{job.kind}_{job.arch}_console.exe"
            copy_executable(console, con_dst)
            record_file(ctx, job.mono, con_dst, job.encrypt)
    elif job.platform.key == "macos":
        # Per-arch slices wait in the work folder until the bundle is assembled.
        dst = work_dir(ctx, job.mono) / "macos" / f"godot_macos_{job.kind}.{job.arch}"
        copy_executable(job.output, dst)
        meta = dst.with_suffix(dst.suffix + ".json")
        meta.write_text(json.dumps({"key_fingerprint": key_fingerprint() if job.encrypt else None,
                                    "git_commit": ctx.git_commit}))


def assemble_macos(ctx: Context, mono: bool) -> bool:
    """lipo per-arch slices into Universal 2 binaries and build macos.zip."""
    slices_dir = work_dir(ctx, mono) / "macos"
    if not slices_dir.is_dir():
        return True
    lipo = lipo_tool(ctx)
    universal: dict[str, Path] = {}
    fingerprints: set[str | None] = set()
    for kind in ("release", "debug"):
        slices = sorted(p for p in slices_dir.glob(f"godot_macos_{kind}.*")
                        if p.suffix in (".arm64", ".x86_64"))
        if not slices:
            continue
        for s in slices:
            with contextlib.suppress(OSError, ValueError):
                fingerprints.add(json.loads(Path(str(s) + ".json").read_text()).get("key_fingerprint"))
        out = slices_dir / f"godot_macos_{kind}.universal"
        if len(slices) == 1:
            warn(f"macOS {kind}: only {slices[0].suffix[1:]} built, template will not be universal")
            copy_executable(slices[0], out)
        else:
            if not lipo:
                fail("No lipo tool available to create Universal 2 binaries.")
                return False
            result = subprocess.run([lipo, "-create", *map(str, slices), "-output", str(out)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                fail(f"lipo failed: {result.stderr.strip()}")
                return False
        universal[kind] = out

    if not universal:
        return True
    if len(fingerprints) > 1:
        warn("macOS slices were built with different encryption keys - rebuild macOS templates")

    app_name = "macos_template.app"
    zip_path = staging_dir(ctx, mono) / "macos.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    template_app = ctx.source / "misc" / "dist" / app_name
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(template_app.rglob("*")):
            if path.is_file():
                zf.write(path, f"{app_name}/{path.relative_to(template_app)}")
        for kind, binary in universal.items():
            zi = zipfile.ZipInfo(f"{app_name}/Contents/MacOS/godot_macos_{kind}.universal",
                                 date_time=time.localtime(binary.stat().st_mtime)[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o100755 << 16)  # regular file, rwxr-xr-x
            with binary.open("rb") as src, zf.open(zi, "w") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    tmp_zip.replace(zip_path)
    fp = next(iter(fingerprints)) if len(fingerprints) == 1 else None
    record_file(ctx, mono, zip_path, encrypted=fp is not None)
    ok(f"macOS bundle  {S.dim('→')} {zip_path.name}  ({', '.join(sorted(universal))})")
    return True


def write_version_file(ctx: Context, mono: bool) -> Path:
    stage = staging_dir(ctx, mono)
    stage.mkdir(parents=True, exist_ok=True)
    version_file = stage / "version.txt"
    version_file.write_text(ctx.version.template_dir_name(mono) + "\n")
    return version_file


def check_key_consistency(ctx: Context, mono: bool) -> None:
    files = load_manifest(ctx, mono).get("files", {})
    prints = {meta.get("key_fingerprint") for meta in files.values()}
    if len(prints) > 1:
        warn("Staged templates were built with different encryption keys (or mixed encrypted/plain):")
        for name, meta in sorted(files.items()):
            detail(f"{name:<36} {meta.get('key_fingerprint') or 'not encrypted'}")
        warn("Rebuild the outdated platforms before shipping, or exports will fail with ERR_FILE_CORRUPT.")


def package_tpz(ctx: Context, mono: bool) -> Path:
    stage = staging_dir(ctx, mono)
    write_version_file(ctx, mono)
    out = DIST_DIR / f"godot-{ctx.version.template_dir_name(mono)}-custom.tpz"
    tmp = out.with_suffix(".tpz.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(stage.iterdir()):
            if path.is_file():
                zi = zipfile.ZipInfo(f"templates/{path.name}", date_time=time.localtime(path.stat().st_mtime)[:6])
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = ((0o100755 if os.access(path, os.X_OK) else 0o100644) << 16)
                with path.open("rb") as src, zf.open(zi, "w") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    tmp.replace(out)
    return out


def install_templates(ctx: Context, mono: bool, since: float | None = None) -> Path | None:
    """Copy staged templates into Godot's template folder (only files newer than `since` when given)."""
    stage = staging_dir(ctx, mono)
    files = [p for p in stage.glob("*") if p.is_file()] if stage.is_dir() else []
    if since is not None:
        files = [p for p in files if p.stat().st_mtime >= since]
    if not files:
        skip(f"nothing staged for {ctx.version.template_dir_name(mono)}")
        return None
    write_version_file(ctx, mono)
    dest = USER_TEMPLATES_DIR / ctx.version.template_dir_name(mono)
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(files):
        shutil.copy2(path, dest / path.name)
    return dest


# ==============================================================================
#  Commands
# ==============================================================================


def cmd_doctor(ctx: Context, args: argparse.Namespace | None = None) -> int:
    problems = 0
    section = ""

    def heading(title: str) -> None:  # noqa: F811 - tracks the section for JSON events
        nonlocal section
        section = title
        globals()["heading"](title)

    heading("Toolchain")

    def check(label: str, good: bool, ok_text: str, bad_text: str, required: bool = True) -> None:
        nonlocal problems
        state = "ok" if good else "error" if required else "warning"
        problems += state == "error"
        if JSON_MODE:
            emit("check", section=section, label=label, state=state, text=ok_text if good else bad_text)
            return
        if good:
            ok(f"{label:<22} {S.dim(ok_text)}")
        elif required:
            fail(f"{label:<22} {bad_text}")
        else:
            warn(f"{label:<22} {S.dim(bad_text)}")

    def version_of(cmd: list[str]) -> str:
        with contextlib.suppress(OSError, subprocess.CalledProcessError):
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            return out.strip().splitlines()[0] if out.strip() else ""
        return ""

    check("python", sys.version_info >= (3, 10), sys.version.split()[0], "3.10+ required")
    scons_ver = ""
    with contextlib.suppress(OSError):
        out = subprocess.run(["scons", "--version"], capture_output=True, text=True).stdout
        match = re.search(r"SCons: v?([\d.]*\d)", out)
        scons_ver = f"SCons {match.group(1)}" if match else "found"
    check("scons", bool(shutil.which("scons")), scons_ver, "not found (pipx install scons)")
    compiler = "clang" if shutil.which("clang") else "gcc" if shutil.which("gcc") else ""
    check("compiler", bool(compiler), version_of([compiler, "--version"]) if compiler else "",
          "neither clang nor gcc found")
    linker = "mold" if shutil.which("mold") else "lld" if shutil.which("ld.lld") else ""
    check("fast linker", bool(linker), linker, "neither mold nor lld (optional, speeds up links)", required=False)
    launcher = shutil.which("sccache") or shutil.which("ccache")
    check("compiler cache", bool(launcher), Path(launcher).name if launcher else "",
          "none (install ccache for faster rebuilds)", required=False)
    check("git", bool(shutil.which("git")), version_of(["git", "--version"]), "not found")
    check("dotnet", bool(shutil.which("dotnet")), version_of(["dotnet", "--version"]),
          "not found (needed for .NET builds)", required=ctx.mono_default)

    heading("Platforms")
    for platform in PLATFORMS.values():
        available, reason = ctx.platform_available(platform)
        extra = ""
        if platform.key == "windows" and available:
            extra = str(LLVM_MINGW_DIR)
        elif platform.key == "macos" and available:
            extra = f"{ctx.osxcross_root} ({ctx.osxcross_sdk or 'sdk?'})"
        elif platform.key == "linux":
            extra = "native"
        check(platform.label, available, extra, reason, required=platform.key == "linux")

    deps = ctx.bin_dir / "build_deps"
    win_deps = {
        "ANGLE": [deps / "angle-x86_64-llvm"],
        "D3D12 Agility SDK": [deps / "agility_sdk"],
        "Mesa NIR (D3D12)": [deps / "mesa-x86_64-llvm"],
        "AccessKit": [deps / "accesskit"],
    }
    heading("Dependencies")
    for name, paths in win_deps.items():
        present = all(p.exists() for p in paths)
        check(name, present, str(paths[0].relative_to(ctx.source)), "missing (run: godot_build.py deps)",
              required=False)
    mvk = custom_py_value(ctx.source, "_MOLTENVK_SDK") or str(moltenvk_dir() or "")
    vulkan_on = bool(custom_py_value(ctx.source, "_MOLTENVK_SDK")) or manager_settings().get("macos_vulkan", True)
    check("MoltenVK (macOS)", bool(mvk) and vulkan_on, mvk,
          ("not installed: macOS templates fall back to OpenGL on Intel Macs "
           "(Diagnostics → Toolchains, or: godot-build toolchain install moltenvk)") if vulkan_on else
          "Vulkan on macOS is turned off in Settings (macos_vulkan)", required=False)

    heading("Machine")
    free_gb = shutil.disk_usage(ctx.source).free / 1024**3
    check("disk space", free_gb >= MIN_FREE_DISK_GB, f"{free_gb:.0f} GB free",
          f"only {free_gb:.0f} GB free (want {MIN_FREE_DISK_GB}+)", required=False)
    with contextlib.suppress(OSError, ValueError, IndexError):
        mem_kb = int(Path("/proc/meminfo").read_text().split("MemTotal:")[1].split()[0])
        check("memory", mem_kb >= 12 * 1024**2, f"{mem_kb / 1024**2:.0f} GB",
              f"{mem_kb / 1024**2:.0f} GB: lower -j if links run out of memory", required=False)
    check("cpu threads", True, str(os.cpu_count()), "")

    heading("Encryption")
    if key_exists():
        try:
            fp = key_fingerprint()
            mode = KEY_FILE.stat().st_mode & 0o777
            check("key file", True, f"{KEY_FILE}  fingerprint {fp}", "")
            check("key permissions", mode == 0o600, "0600", f"{oct(mode)} (should be 0600: chmod 600 {KEY_FILE})",
                  required=False)
        except BuildError as exc:
            check("key file", False, "", str(exc))
    else:
        check("key file", False, "", "none yet (godot_build.py key generate)", required=False)

    heading("Desktop")
    link = launcher_link()
    check("launcher", link.is_symlink() and link.exists(), f"{link} → {link.resolve().name}" if link.exists() else "",
          f"{link} missing (godot_build.py integrate)", required=False)
    check("desktop entry", DESKTOP_FILE.is_file(), str(DESKTOP_FILE), "not installed (godot_build.py integrate)",
          required=False)

    if not JSON_MODE:
        print()
    if problems:
        fail(f"{problems} required item(s) missing")
        return 1
    ok("Ready to build")
    return 0


def resolve_mono(ctx: Context, args: argparse.Namespace) -> bool:
    return ctx.mono_default if args.mono is None else args.mono


def build_editor(ctx: Context, args: argparse.Namespace) -> int:
    mono = resolve_mono(ctx, args)
    linux = PLATFORMS["linux"]
    job = Job(linux, "editor", HOST_ARCH, mono, encrypt=False)

    heading("Editor")
    info(f"{linux.label} editor · {HOST_ARCH} · {'.NET' if mono else 'GDScript only'}")
    cmd = ["scons", *job.scons_args(ctx)]
    if ctx.dry_run:
        detail("$ " + " ".join(cmd))
        return 0

    history = read_history()
    steps = [{**job.as_dict(), "expected_seconds": expected_seconds(job.state_key, history)}]
    if mono and not args.no_glue:
        steps += [{"id": key, "title": name, "expected_seconds": expected_seconds(key, history)}
                  for key, name in (("mono_glue", "C# glue"), ("mono_assemblies", ".NET assemblies"))]
    emit("plan", jobs=steps)

    job.log = new_log_path(job.state_key)
    start = time.monotonic()
    try:
        built = run_logged(cmd, cwd=ctx.source, env=ctx.build_env(None), log_path=job.log,
                           title=f"Linux editor {HOST_ARCH}", state_key=job.state_key, ctx=ctx)
    except KeyboardInterrupt:
        job.status, job.seconds = "interrupted", time.monotonic() - start
        emit("job", **job.as_dict())
        raise
    job.seconds = time.monotonic() - start
    job.output = find_binary(ctx, job) if built else None
    job.status = "ok" if built and job.output else "failed"
    emit("job", **job.as_dict())
    if job.status != "ok":
        return 1
    ok(f"editor built in {fmt_duration(job.seconds)}  {S.dim('→')} {job.output.name}")
    refresh_launcher_link(ctx, quiet=True, unpin=True)

    if mono and not args.no_glue:
        if build_dotnet(ctx, job.output) != 0:
            return 1
    heading("Snapshot")
    snapshot_editor(ctx, job.output)
    notify("Godot editor built", f"{job.output.name} in {fmt_duration(time.monotonic() - start)}")
    return 0


def build_dotnet(ctx: Context, editor: Path) -> int:
    heading(".NET")
    glue_cmd = [str(editor), "--headless", "--generate-mono-glue", "modules/mono/glue"]
    if not run_logged(glue_cmd, cwd=ctx.source, env=ctx.build_env(None), log_path=new_log_path("mono_glue"),
                      title="Generating C# glue", state_key="mono_glue", ctx=ctx):
        return 1
    ok("C# glue generated")

    NUGET_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    asm_cmd = [sys.executable, "modules/mono/build_scripts/build_assemblies.py",
               "--godot-output-dir=./bin", "--godot-platform=linuxbsd",
               f"--push-nupkgs-local={NUGET_LOCAL_DIR}"]
    if not run_logged(asm_cmd, cwd=ctx.source, env=ctx.build_env(None), log_path=new_log_path("mono_assemblies"),
                      title="Building .NET assemblies", state_key="mono_assemblies", ctx=ctx):
        return 1
    ok(f"assemblies built, NuGet packages in {NUGET_LOCAL_DIR}")

    with contextlib.suppress(OSError):
        sources = subprocess.run(["dotnet", "nuget", "list", "source"], capture_output=True, text=True).stdout
        if str(NUGET_LOCAL_DIR) not in sources:
            warn("Register the local NuGet feed once so C# projects resolve this Godot version:")
            detail(f'dotnet nuget add source "{NUGET_LOCAL_DIR}" --name GodotLocal')
    return 0


def plan_jobs(ctx: Context, args: argparse.Namespace) -> list[Job]:
    mono = resolve_mono(ctx, args)
    requested = [p.strip() for p in args.platforms.split(",")] if args.platforms != "all" else list(PLATFORMS)
    targets = [t.strip() for t in args.targets.split(",")]
    for name in requested:
        if name not in PLATFORMS:
            raise BuildError(f"Unknown platform '{name}' (choose from {', '.join(PLATFORMS)}, all)")
    for target in targets:
        if target not in TARGETS:
            raise BuildError(f"Unknown target '{target}' (choose from release, debug)")

    encrypt = not args.no_encrypt
    if encrypt and not key_exists():
        if args.require_encryption:
            raise BuildError("No encryption key. Run `godot_build.py key generate` or pass --no-encrypt.")
        warn("No encryption key found: templates will be built WITHOUT PCK encryption.")
        detail("Create one with `godot_build.py key generate`, or pass --no-encrypt to silence this.")
        encrypt = False

    # --arch is either global ("x86_64,arm64") or per platform ("windows:x86_64,windows:arm64,macos:arm64").
    global_arches: list[str] = []
    per_platform: dict[str, list[str]] = {}
    for entry in filter(None, (a.strip() for a in (args.arch or "").split(","))):
        if ":" in entry:
            plat, arch = entry.split(":", 1)
            per_platform.setdefault(plat.strip(), []).append(arch.strip())
        else:
            global_arches.append(entry)

    jobs: list[Job] = []
    for name in requested:
        platform = PLATFORMS[name]
        available, reason = ctx.platform_available(platform)
        if not available:
            if args.platforms == "all":
                skip(f"{platform.label}: {reason}")
                continue
            raise BuildError(f"{platform.label} requested but unavailable: {reason}")
        arches = per_platform.get(name) or global_arches or list(platform.default_arches)
        for arch in arches:
            if arch not in platform.supported_arches:
                raise BuildError(f"{platform.label} does not support arch '{arch}' here "
                                 f"(supported: {', '.join(platform.supported_arches)})")
            for target in targets:
                jobs.append(Job(platform, TARGETS[target], arch, mono, encrypt))
    return jobs


def build_templates(ctx: Context, args: argparse.Namespace) -> int:
    heading("Plan")
    jobs = plan_jobs(ctx, args)
    if not jobs:
        fail("Nothing to build.")
        return 1
    for i, job in enumerate(jobs, 1):
        lock = S.green("encrypted") if job.encrypt else S.yellow("plain")
        info(f"{S.dim(f'{i:>2}.')} {job.describe()}  {lock}{S.dim('  .NET' if job.mono else '')}")
    mono = jobs[0].mono
    detail(f"staging → {staging_dir(ctx, mono)}")
    history = read_history()
    emit("plan", jobs=[{**j.as_dict(), "expected_seconds": expected_seconds(j.state_key, history)} for j in jobs])

    if ctx.dry_run:
        heading("Commands (dry run)")
        for job in jobs:
            env_note = "SCRIPT_AES256_ENCRYPTION_KEY=•••• " if job.encrypt else ""
            detail(f"$ {env_note}scons {' '.join(job.scons_args(ctx))}")
        return 0

    heading("Build")
    started = time.monotonic()
    wall_started = time.time()
    key = read_key() if any(j.encrypt for j in jobs) else None
    interrupted = False
    for job in jobs:
        job.log = new_log_path(job.state_key)
        t0 = time.monotonic()
        try:
            success = run_logged(["scons", *job.scons_args(ctx)], cwd=ctx.source, env=ctx.build_env(job),
                                 log_path=job.log, title=job.describe().strip(), state_key=job.state_key, ctx=ctx)
        except KeyboardInterrupt:
            job.status = "interrupted"
            job.seconds = time.monotonic() - t0
            interrupted = True
            emit("job", **job.as_dict())
            if not JSON_MODE:
                print()
            warn("Interrupted - stopping after cleaning up SCons.")
            break
        job.seconds = time.monotonic() - t0

        if success:
            job.output = find_binary(ctx, job)
            if not job.output:
                success = False
                fail(f"{job.describe()}  built, but no output binary found in {ctx.bin_dir}")
            elif key and not binary_contains_key(job.output, key):
                success = False
                fail(f"{job.describe()}  encryption key NOT embedded in {job.output.name}")
                detail("Exports would fail with ERR_FILE_CORRUPT. Check the log for the key notice.")

        if success:
            job.status = "ok"
            stage_job(ctx, job)
            size = fmt_size(job.output.stat().st_size) if job.output else ""
            ok(f"{job.describe()}  {S.dim(fmt_duration(job.seconds))}  {size}"
               + (f"  {S.green('🔒')}" if job.encrypt and S.enabled else ""))
        else:
            job.status = "failed"
        emit("job", **job.as_dict())
        if job.status == "failed" and not args.keep_going:
            break

    for job in jobs:
        if job.status == "pending":
            job.status = "skipped"
            emit("job", **job.as_dict())

    # Package whatever succeeded, even if later jobs failed.
    if any(j.platform.key == "macos" and j.status == "ok" for j in jobs):
        heading("Package")
        assemble_macos(ctx, mono)
    write_version_file(ctx, mono)
    check_key_consistency(ctx, mono)

    print_summary(jobs, time.monotonic() - started)
    all_ok = all(j.status == "ok" for j in jobs)

    if all_ok and (args.install or args.tpz):
        heading("Install")
        if args.install:
            dest = install_templates(ctx, mono)
            if dest:
                ok(f"installed  {S.dim('→')} {dest}")
        if args.tpz:
            ok(f"packaged   {S.dim('→')} {package_tpz(ctx, mono)}")
    elif args.install and any(j.status == "ok" for j in jobs) and not interrupted:
        # Install what did build so it is usable now; failed targets keep their previously installed files.
        heading("Install")
        dest = install_templates(ctx, mono, since=wall_started)
        if dest:
            ok(f"installed the templates that built  {S.dim('→')} {dest}")
        failed = ", ".join(f"{j.platform.label} {j.kind}" + (f" {j.arch}" if j.arch else "")
                           for j in jobs if j.status != "ok")
        warn(f"Not installed (did not build): {failed}")
        if args.tpz:
            warn("Skipping the .tpz package because not every build succeeded.")
    elif args.install or args.tpz:
        warn("Skipping install/package because not every build succeeded.")

    notify("Godot templates " + ("built" if all_ok else "FAILED"),
           f"{sum(j.status == 'ok' for j in jobs)}/{len(jobs)} succeeded in "
           f"{fmt_duration(time.monotonic() - started)}")
    if interrupted:
        return 130
    return 0 if all_ok else 1


def print_summary(jobs: list[Job], total: float) -> None:
    if JSON_MODE:
        emit("summary", jobs=[j.as_dict() for j in jobs], seconds=round(total, 1),
             succeeded=sum(j.status == "ok" for j in jobs))
        return
    heading("Summary")
    marks = {"ok": S.green(ICON["ok"]), "failed": S.red(ICON["fail"]),
             "skipped": S.dim(ICON["skip"]), "interrupted": S.yellow(ICON["warn"])}
    for job in jobs:
        size = fmt_size(job.output.stat().st_size) if job.output and job.output.exists() else ""
        time_s = fmt_duration(job.seconds) if job.seconds else ""
        line = f"  {marks[job.status]} {job.describe()}  {time_s:>7}  {size:>9}"
        print(line if job.status == "ok" else S.dim(line) if job.status == "skipped" else line)
        if job.status in ("failed", "interrupted") and job.log:
            detail(f"log: {job.log}")
    good = sum(j.status == "ok" for j in jobs)
    colour = S.green if good == len(jobs) else S.yellow if good else S.red
    print(f"\n  {colour(f'{good}/{len(jobs)} succeeded')}  {S.dim('in ' + fmt_duration(total))}")


def cmd_editor(ctx: Context, args: argparse.Namespace) -> int:
    with build_lock(ctx):
        return build_editor(ctx, args)


def cmd_templates(ctx: Context, args: argparse.Namespace) -> int:
    with build_lock(ctx):
        return build_templates(ctx, args)


def cmd_all(ctx: Context, args: argparse.Namespace) -> int:
    if cmd_doctor(ctx) != 0 and not confirm("Doctor found problems. Continue anyway?", ctx):
        return 1
    with build_lock(ctx):
        if build_editor(ctx, args) != 0:
            return 1
        args.install = True
        return build_templates(ctx, args)


def cmd_install(ctx: Context, args: argparse.Namespace) -> int:
    mono = resolve_mono(ctx, args)
    heading("Install")
    if any((work_dir(ctx, mono) / "macos").glob("godot_macos_*")):
        assemble_macos(ctx, mono)
    check_key_consistency(ctx, mono)
    dest = install_templates(ctx, mono)
    if dest:
        ok(f"installed  {S.dim('→')} {dest}")
    if args.tpz:
        ok(f"packaged   {S.dim('→')} {package_tpz(ctx, mono)}")
    return 0 if dest else 1


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    heading("Update source")
    if git(ctx.source, "status", "--porcelain", "-uno"):
        warn("Source tree has local modifications; pulling with --ff-only.")
    branch = git(ctx.source, "rev-parse", "--abbrev-ref", "HEAD")
    upstream = git(ctx.source, "rev-parse", "--abbrev-ref", "@{upstream}")
    if not upstream:
        fail(f"Branch '{branch}' has no upstream to pull from.")
        return 1
    info(f"fetching {upstream} …")
    if error := fetch_source(ctx):
        fail("git fetch failed: " + error)
        return 1
    commits = incoming_changes(ctx, upstream)
    total = git(ctx.source, "rev-list", "--count", f"HEAD..{upstream}")
    emit("incoming", upstream=upstream, commits=commits, commit_count=int(total) if total.isdigit() else len(commits))
    if not commits:
        ok("Already up to date.")
        return 0
    info(f"{len(commits)} change(s) ({total} commits) on {upstream}:")
    for c in commits[:15]:
        tag = f"#{c['pr']}" if c["pr"] else c["short"]
        areas = ", ".join(a["label"] for a in c["areas"][:2])
        star = S.yellow(" ★") if c["watched"] else ""
        detail(f"{tag:<8} {c['title'][:70]}  {S.dim(areas)}{star}")
    if len(commits) > 15:
        detail(f"… and {len(commits) - 15} more")
    if args.check or ctx.dry_run or not confirm("Fast-forward now?", ctx, default=True):
        return 0
    merge = subprocess.run(["git", "-C", str(ctx.source), "merge", "--ff-only", "--quiet", upstream],
                           capture_output=True, text=True)
    if merge.returncode != 0:
        fail("Fast-forward failed (diverged history?). Resolve manually.")
        return 1
    ok(f"Updated to {git(ctx.source, 'rev-parse', '--short', 'HEAD')}  ·  version {Version.read(ctx.source).display}")
    return 0


def cmd_deps(ctx: Context, args: argparse.Namespace) -> int:
    heading("Third-party SDKs")
    scripts = [
        ("AccessKit", ["misc/scripts/install_accesskit.py"]),
        ("ANGLE", ["misc/scripts/install_angle.py"]),
        ("D3D12 Agility SDK + Mesa", ["misc/scripts/install_d3d12_sdk_windows.py",
                                      f"--mingw_prefix={LLVM_MINGW_DIR}"]),
    ]
    info("These official Godot scripts download prebuilt SDKs into bin/build_deps.")
    if not confirm("Download / refresh them now?", ctx, default=True):
        return 0
    failed = 0
    for name, script in scripts:
        if not (ctx.source / script[0]).exists():
            skip(f"{name}: {script[0]} not in this Godot version")
            continue
        if ctx.dry_run:
            detail(f"$ python {' '.join(script)}")
            continue
        if run_logged([sys.executable, *script], cwd=ctx.source, env=ctx.build_env(None),
                      log_path=new_log_path(f"deps_{name.split()[0].lower()}"), title=name,
                      state_key=None, ctx=ctx):
            ok(name)
        else:
            failed += 1
    return 1 if failed else 0


def cmd_key(ctx: Context, args: argparse.Namespace) -> int:
    action = args.action
    if action == "generate":
        heading("Encryption key")
        if key_exists():
            old_fp = key_fingerprint() if HEX64.match(KEY_FILE.read_text().strip()) else "invalid"
            warn(f"A key already exists (fingerprint {old_fp}).")
            detail("Replacing it invalidates every template built with it and every PCK exported with it.")
            if not args.force:
                fail("Refusing to overwrite without --force.")
                return 1
            if not confirm("Really replace the encryption key?", ctx):
                return 1
            backup = KEY_FILE.with_name(f"godot.{dt.datetime.now():%Y%m%d-%H%M%S}.gdkey.bak")
            KEY_FILE.replace(backup)
            backup.chmod(0o600)
            info(f"old key backed up to {backup}")
            for stale in (DIST_DIR / "templates", DIST_DIR / "work"):
                if stale.exists():
                    shutil.rmtree(stale)
            for manifest in DIST_DIR.glob("*.manifest.json"):
                manifest.unlink()
            info("cleared staged templates built with the old key")
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.parent.chmod(0o700)
        key = secrets.token_hex(32)
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        ok(f"New AES-256 key written to {KEY_FILE} (mode 0600)")
        detail(f"fingerprint {key_fingerprint(key)}")
        emit("key", exists=True, fingerprint=key_fingerprint(key), path=str(KEY_FILE))
        if not JSON_MODE:
            print()
        info("Next steps:")
        detail("1. Rebuild templates:  godot_build.py templates --install")
        detail("2. In each export preset → Encryption: enable 'Encrypt Exported PCK',")
        detail("   add include filters (e.g. *.gd, *.tscn, *.tres), and either paste the key")
        detail("   or launch the editor with GODOT_SCRIPT_ENCRYPTION_KEY set (godot_build.py key env).")
        detail("3. Back the key file up somewhere safe. Never commit it.")
        return 0

    if action == "backup":
        heading("Key backup")
        return key_backup(args)
    if action == "restore":
        heading("Restore key")
        return key_restore(args)
    if action == "check-backup":
        heading("Check key backup")
        return key_check_backup(args)
    if not key_exists():
        fail("No key yet. Run: godot_build.py key generate")
        return 1
    key = read_key()
    if action == "show":
        print(key if args.reveal else f"{key[:4]}{'•' * 56}{key[-4:]}")
        if not args.reveal:
            detail(f"fingerprint {key_fingerprint(key)}  ·  use --reveal to print the full key")
        return 0
    if action == "path":
        print(KEY_FILE)
        return 0
    if action == "env":
        shell = args.shell or Path(os.environ.get("SHELL", "bash")).name
        if shell == "fish":
            print(f"set -gx GODOT_SCRIPT_ENCRYPTION_KEY {key}")
        else:
            print(f"export GODOT_SCRIPT_ENCRYPTION_KEY={key}")
        return 0
    if action == "verify":
        heading("Verify templates")
        bad = 0
        for mono in (False, True):
            stage = staging_dir(ctx, mono)
            if not stage.is_dir():
                continue
            for path in sorted(stage.iterdir()):
                # Console wrappers are tiny launchers for the main .exe and
                # never contain engine code (or the key).
                if path.name == "version.txt" or path.name.endswith("_console.exe") or not path.is_file():
                    continue
                if path.suffix == ".zip":
                    with zipfile.ZipFile(path) as zf:
                        names = [n for n in zf.namelist() if n.endswith(".universal")]
                        needle = bytes.fromhex(key)
                        found = all(needle in zf.read(n) for n in names) if names else False
                else:
                    found = binary_contains_key(path, key)
                emit("verify", name=f"{stage.name}/{path.name}", ok=found)
                if not JSON_MODE:
                    (ok if found else fail)(f"{stage.name}/{path.name}")
                bad += not found
        return 1 if bad else 0
    return 1


def cmd_clean(ctx: Context, args: argparse.Namespace) -> int:
    heading("Clean")
    targets: list[Path] = []
    if args.dist or args.everything:
        targets.append(DIST_DIR)
    if args.logs or args.everything:
        targets.extend(p for p in LOG_DIR.glob("*.log"))
    if args.objects or args.everything:
        targets.append(ctx.bin_dir / "obj")
    if not targets:
        info("Nothing selected. Use --dist, --logs, --objects or --everything.")
        return 0
    for path in targets[:10]:
        detail(str(path))
    if len(targets) > 10:
        detail(f"… and {len(targets) - 10} more")
    if args.objects or args.everything:
        warn("Removing bin/obj forces a full rebuild of every platform.")
    if ctx.dry_run or not confirm("Delete these?", ctx):
        return 0
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    ok("Cleaned.")
    return 0


# ==============================================================================
#  Desktop integration
# ==============================================================================

MANAGER_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5a9fd6"/><stop offset="1" stop-color="#36709f"/>
    </linearGradient>
    <linearGradient id="badge" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3ddc84"/><stop offset="1" stop-color="#1f9e5b"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="112" height="112" rx="26" fill="url(#bg)"/>
  <rect x="8" y="8" width="112" height="56" rx="26" fill="#fff" opacity=".08"/>
  <g transform="translate(56 56)">
    <circle r="31" fill="none" stroke="#fff" stroke-width="13" stroke-dasharray="12.17 12.17"/>
    <circle r="26" fill="#fff"/>
    <circle r="11" fill="#36709f"/>
    <path d="M-5-2h10l-5 7z" fill="#fff" opacity=".9"/>
  </g>
  <circle cx="96" cy="96" r="27" fill="url(#badge)" stroke="#fff" stroke-width="4"/>
  <path d="M96 82v22m-9-9 9 9 9-9" fill="none" stroke="#fff" stroke-width="5"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def launcher_link() -> Path:
    return LOCAL_BIN / "godot"


def find_editor(ctx: Context) -> Path | None:
    """Newest editor binary, preferring the .NET flavour custom.py selects."""
    candidates: list[Path] = []
    prefix = f"godot.linuxbsd.editor.{HOST_ARCH}"
    for path in ctx.bin_dir.glob(prefix + "*"):
        rest = path.name[len(prefix):]
        if path.is_file() and not path.name.endswith(IGNORED_SUFFIXES) and not any(
                tag in rest for tag in (".double", ".dev", ".san")):
            candidates.append(path)
    if not candidates:
        return None
    preferred = [p for p in candidates if (".mono" in p.name) == ctx.mono_default] or candidates
    return max(preferred, key=lambda p: p.stat().st_mtime)


def refresh_launcher_link(ctx: Context, quiet: bool = False, unpin: bool = False) -> bool:
    """Point ~/.local/bin/godot at the newest editor build (atomic swap).

    A launcher pinned to a snapshot is left alone unless `unpin` is set
    (a fresh editor build always switches back to the latest).
    """
    editor = find_editor(ctx)
    link = launcher_link()
    pinned = pinned_snapshot_id()
    if pinned and not unpin:
        if not quiet:
            info(f"launcher is pinned to snapshot {pinned}; leaving it")
        return True
    if not editor:
        if not quiet:
            fail(f"No editor binary in {ctx.bin_dir}. Build the editor first.")
        return False
    if link.exists() and not link.is_symlink():
        warn(f"{link} is a real file, not a symlink: leaving it alone.")
        return False
    if link.is_symlink() and link.resolve() == editor.resolve():
        if not quiet:
            ok(f"launcher already points at {editor.name}")
        return True
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(".godot.tmp-link")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    tmp.symlink_to(editor)
    tmp.replace(link)
    ok(f"launcher {S.dim(str(link))} → {editor.name}")
    return True


def desktop_quote(value: str) -> str:
    """Quote an Exec argument per the Desktop Entry spec when needed."""
    if re.search(r'[\s"`$\\]', value):
        return '"' + re.sub(r'(["`$\\])', r"\\\1", value) + '"'
    return value


def desktop_entries(ctx: Context) -> dict[Path, str]:
    link = desktop_quote(str(launcher_link()))
    # The system interpreter is the one with PyGObject/GTK; a venv python may not be.
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    gui = f"{desktop_quote(python)} {desktop_quote(str(GUI_SCRIPT))}"
    # Godot starts through `launch`, which also runs the post-launch update check.
    run = f"{desktop_quote(python)} {desktop_quote(str(BACKEND_SCRIPT))} launch --"
    icon = "godot-mono" if ctx.mono_default else "godot"
    version = f"{ctx.version.display}{' .NET' if ctx.mono_default else ''}"
    common = (
        "Type=Application\n"
        "Name=Godot Engine\n"
        "GenericName=Game Engine\n"
        f"Comment=Multi-platform 2D and 3D game engine · custom {version} build\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Development;IDE;\n"
        "X-GodotBuild-Managed=true\n"
    )
    main = (
        "[Desktop Entry]\n" + common +
        "Keywords=game development;development;IDE;game engine;godot;gdscript;\n"
        f"Exec={run} %f\n"
        f"TryExec={launcher_link()}\n"
        "MimeType=application/x-godot-project;\n"
        "StartupNotify=true\n"
        "StartupWMClass=org.godotengine.ProjectManager\n"
        "Actions=ProjectManager;BuildManager;CheckUpdates;\n"
        "\n[Desktop Action ProjectManager]\n"
        "Name=Project Manager\n"
        f"Exec={run} --project-manager\n"
        "\n[Desktop Action BuildManager]\n"
        "Name=Build Manager\n"
        f"Icon={MANAGER_APP_ID}\n"
        f"Exec={gui}\n"
        "\n[Desktop Action CheckUpdates]\n"
        "Name=Check for Engine Updates\n"
        "Icon=software-update-available\n"
        f"Exec={gui} --page source --check-updates\n"
    )
    # Hidden aliases so docks/taskbars show the right name and icon for each
    # window's Wayland app_id without adding extra launcher entries.
    editor_alias = (
        "[Desktop Entry]\n" + common +
        f"Exec={run} %f\n"
        "NoDisplay=true\n"
        "StartupWMClass=org.godotengine.Editor\n"
    )
    manager = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Godot Build Manager\n"
        "Comment=Build, encrypt and install custom Godot editors and export templates\n"
        f"Icon={MANAGER_APP_ID}\n"
        f"Exec={gui}\n"
        "Terminal=false\n"
        f"NoDisplay={'false' if manager_settings()['manager_in_launcher'] else 'true'}\n"
        "Categories=Development;\n"
        "StartupNotify=true\n"
        "X-GodotBuild-Managed=true\n"
    )
    return {DESKTOP_FILE: main, EDITOR_ALIAS_FILE: editor_alias, MANAGER_DESKTOP_FILE: manager}


def cmd_integrate(ctx: Context, args: argparse.Namespace) -> int:
    heading("Desktop integration")
    record = {}
    with contextlib.suppress(OSError, ValueError):
        record = json.loads(INTEGRATION_RECORD.read_text())

    if args.remove:
        removed = 0
        for path in map(Path, record.get("files", [])):
            if path.exists():
                path.unlink()
                removed += 1
        link = launcher_link()
        if link.is_symlink() and str(ctx.bin_dir) in os.readlink(link):
            link.unlink()
            removed += 1
        INTEGRATION_RECORD.unlink(missing_ok=True)
        for tool, target in (("update-desktop-database", APPS_DIR), ("update-mime-database", MIME_DIR)):
            if shutil.which(tool) and target.exists():
                subprocess.run([tool, str(target)], capture_output=True)
        ok(f"removed {removed} file(s)")
        return 0

    if ctx.dry_run:
        for path in desktop_entries(ctx):
            detail(f"would write {path}")
        return 0

    written: list[str] = []
    refresh_launcher_link(ctx)

    APPS_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in desktop_entries(ctx).items():
        if path.exists() and "X-GodotBuild-Managed=true" not in path.read_text(errors="replace"):
            backup = path.with_name(path.name + ".bak")
            path.replace(backup)
            warn(f"existing {path.name} backed up to {backup.name}")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        written.append(str(path))
        ok(f"desktop entry {S.dim(str(path))}")

    icons = {
        ICONS_DIR / "apps" / "godot.svg": (ctx.source / "misc" / "logo" / "icon.svg").read_text()
        if (ctx.source / "misc" / "logo" / "icon.svg").exists() else None,
        ICONS_DIR / "apps" / f"{MANAGER_APP_ID}.svg": MANAGER_ICON_SVG,
        ICONS_DIR / "mimetypes" / "application-x-godot-project.svg":
            (ctx.source / "misc" / "dist" / "document_icons" / "project.svg").read_text()
            if (ctx.source / "misc" / "dist" / "document_icons" / "project.svg").exists() else None,
    }
    for path, svg in icons.items():
        if svg is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        written.append(str(path))
    ok(f"icons {S.dim(str(ICONS_DIR))}")

    mime_src = ctx.source / "misc" / "dist" / "linux" / "org.godotengine.Godot.xml"
    if mime_src.exists():
        mime_dst = MIME_DIR / "packages" / mime_src.name
        mime_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mime_src, mime_dst)
        written.append(str(mime_dst))
        if shutil.which("update-mime-database"):
            subprocess.run(["update-mime-database", str(MIME_DIR)], capture_output=True)
        ok("project.godot files open with this build")

    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(APPS_DIR)], capture_output=True)
    if shutil.which("gtk-update-icon-cache") and (ICONS_DIR.parent / "index.theme").exists():
        subprocess.run(["gtk-update-icon-cache", "-qtf", str(ICONS_DIR.parent)], capture_output=True)

    INTEGRATION_RECORD.parent.mkdir(parents=True, exist_ok=True)
    INTEGRATION_RECORD.write_text(json.dumps({"files": written, "installed": dt.datetime.now().isoformat(
        timespec="seconds")}, indent=2))
    info("Right-click Godot in your launcher for Build Manager and update checks.")
    return 0


# ==============================================================================
#  custom.py settings
# ==============================================================================

CONFIG_SPEC: list[tuple[str, str, str, str]] = [
    ("_NATIVE_EDITOR", "bool", "Tune editor for this CPU",
     "Schedules editor code for your processor (-mtune). Templates are never tuned."),
    ("_DOTNET", "bool", ".NET (C#) support",
     "Build the editor and templates with C# support by default."),
    ("_SEPARATE_DEBUG_SYMBOLS", "bool", "Separate debug symbols",
     "Keep symbols in .debug files for readable crash backtraces. Slower links."),
    ("_SLIM_2D_TEMPLATES", "bool", "2D-only export templates",
     "Strip 3D, XR and 3D physics from templates. Smaller, only for 2D games."),
    ("_CACHE_DIR", "str", "SCons cache folder", "Object cache that speeds up rebuilds. Empty disables it."),
    ("_CACHE_LIMIT_GIB", "int", "Cache size limit (GiB)", "0 means unlimited."),
    ("_LLVM_MINGW", "str", "llvm-mingw toolchain", "Windows cross-compiler folder."),
    ("_MOLTENVK_SDK", "str", "MoltenVK SDK", "Enables Vulkan on macOS. Empty builds macOS with Metal/OpenGL."),
]


def config_line_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<head>{re.escape(name)}\s*=\s*)(?P<value>.+?)(?P<tail>\s*(#.*)?)$", re.M)


def read_config(ctx: Context) -> list[dict]:
    text = (ctx.source / "custom.py").read_text(encoding="utf-8")
    result = []
    for name, kind, title, description in CONFIG_SPEC:
        match = config_line_re(name).search(text)
        value: object = None
        if match:
            with contextlib.suppress(ValueError, SyntaxError):
                value = ast.literal_eval(match.group("value"))
        result.append({"name": name, "type": kind, "title": title, "description": description,
                       "value": value, "present": bool(match)})
    return result


def write_config(ctx: Context, name: str, raw: str) -> object:
    if name in {n for n, *_ in MANAGER_SPEC}:
        return write_manager(name, raw)
    spec = {n: k for n, k, *_ in CONFIG_SPEC}
    if name not in spec:
        raise BuildError(f"Unknown setting '{name}'. Known: {', '.join(spec)}")
    kind = spec[name]
    if kind == "bool":
        if raw.lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            raise BuildError(f"{name} expects true/false")
        value: object = raw.lower() in ("true", "yes", "1", "on")
    elif kind == "int":
        if not raw.isdigit():
            raise BuildError(f"{name} expects a whole number")
        value = int(raw)
    else:
        value = raw
    path = ctx.source / "custom.py"
    text = path.read_text(encoding="utf-8")
    pattern = config_line_re(name)
    if not pattern.search(text):
        raise BuildError(f"{name} is not defined in custom.py")
    new_text = pattern.sub(lambda m: m.group("head") + repr(value) + m.group("tail"), text, count=1)
    ast.parse(new_text)  # never write a custom.py SCons can't load
    backup = path.with_name("custom.py.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name("custom.py.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return value


def cmd_config(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "install-template":
        install_custom_py(ctx, args.force)
        emit("config", settings=read_config(ctx), manager=read_manager())
        return 0
    if args.action == "set":
        if not args.name or args.value is None:
            raise BuildError("usage: config set NAME VALUE")
        value = write_config(ctx, args.name, args.value)
        emit("config", settings=read_config(ctx), manager=read_manager())
        ok(f"{args.name} = {value!r}")
        return 0
    settings = read_config(ctx)
    emit("config", settings=settings, manager=read_manager())
    if not JSON_MODE:
        heading("custom.py")
        for s in settings:
            print(f"  {S.cyan(s['name']):<34} {s['value']!r:<40} {S.dim(s['title'])}")
    return 0


# ==============================================================================
#  Manager settings (manager.json, shared by the CLI and the GUI)
# ==============================================================================

MANAGER_SPEC = [
    ("source_dir", "path", "", "Godot source tree",
     "The Godot checkout to build (the folder containing SConstruct)."),
    ("workspace_dir", "path", "", "Workspace folder",
     "Logs, staged templates, snapshots and the encryption key. Empty = ~/.local/share/godot-build-manager. "
     "Changing it does not move existing files."),
    ("llvm_mingw", "path", "", "llvm-mingw toolchain",
     "Windows cross-compiler. Empty = auto-detect (/opt/llvm-mingw, ~/llvm-mingw, PATH)."),
    ("osxcross_root", "path", "", "osxcross folder",
     "macOS cross toolchain (the osxcross checkout). Empty = auto-detect."),
    ("moltenvk_sdk", "path", "", "MoltenVK folder",
     "The MoltenVK.xcframework used for Vulkan on macOS. Empty = the one `toolchain install moltenvk` downloads."),
    ("macos_vulkan", "bool", True, "Vulkan on macOS (MoltenVK)",
     "Link MoltenVK into macOS templates when it is installed, so Intel Macs get Vulkan instead of OpenGL. "
     "Apple Silicon uses Metal either way."),
    ("manager_in_launcher", "bool", True, "Show Build Manager in the app launcher",
     "Off: it is only reachable by right-clicking Godot in the launcher. Applies on the next integrate."),
    ("auto_check", "bool", True, "Check for updates when Godot opens",
     "Opening Godot from the launcher fetches the source in the background and notifies you about new changes."),
    ("auto_check_hours", "int", 6, "Hours between update checks",
     "Godot launches within this window of the last check don't fetch again."),
    ("watch_paths", "str", "modules/mono,editor/export,platform/linuxbsd,platform/windows,core/crypto",
     "Watched source areas",
     "Comma-separated source paths. Incoming changes that touch them are highlighted."),
    ("snapshot_keep", "int", 5, "Editor snapshots to keep",
     "Every successful editor build is kept as a snapshot. Older ones are pruned; the one in use never is."),
]


def manager_settings() -> dict:
    values = {name: default for name, _kind, default, *_ in MANAGER_SPEC}
    with contextlib.suppress(OSError, ValueError):
        stored = json.loads(MANAGER_FILE.read_text())
        values.update({k: v for k, v in stored.items() if k in values})
    return values


def read_manager() -> list[dict]:
    values = manager_settings()
    return [{"name": name, "type": kind, "title": title, "description": description,
             "value": values[name], "present": True}
            for name, kind, _default, title, description in MANAGER_SPEC]


def write_manager(name: str, raw: str) -> object:
    kinds = {n: k for n, k, *_ in MANAGER_SPEC}
    kind = kinds[name]
    if kind == "bool":
        if raw.lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            raise BuildError(f"{name} expects true/false")
        value: object = raw.lower() in ("true", "yes", "1", "on")
    elif kind == "int":
        if not raw.isdigit() or int(raw) < 1:
            raise BuildError(f"{name} expects a whole number of at least 1")
        value = int(raw)
    elif kind == "path":
        value = str(Path(raw).expanduser().resolve()) if raw.strip() else ""
        if name == "source_dir" and value:
            value = str(validate_source(Path(value)))
    else:
        value = ",".join(part.strip().strip("/") for part in raw.split(",") if part.strip())
    stored = {**_stored_config(), **manager_settings()}
    stored[name] = value
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANAGER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, indent=2, sort_keys=True))
    tmp.replace(MANAGER_FILE)
    return value


# ==============================================================================
#  Editor snapshots
# ==============================================================================


def copy_fast(src: Path, dst: Path) -> None:
    """Copy a file or tree, sharing blocks (reflink) where the filesystem can."""
    if shutil.which("cp"):
        result = subprocess.run(["cp", "-a", "--reflink=auto", str(src), str(dst)], capture_output=True, text=True)
        if result.returncode == 0:
            return
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def pinned_snapshot_id() -> str | None:
    """Snapshot the launcher points at, or None when it follows the latest build."""
    link = launcher_link()
    if not link.is_symlink():
        return None
    target = Path(os.readlink(link))
    if not target.is_absolute():
        target = link.parent / target
    return target.parent.name if SNAPSHOT_DIR in target.parents else None


def list_snapshots() -> list[dict]:
    result: list[dict] = []
    if not SNAPSHOT_DIR.is_dir():
        return result
    pinned = pinned_snapshot_id()
    for folder in SNAPSHOT_DIR.iterdir():
        if folder.name.startswith("."):
            continue
        with contextlib.suppress(OSError, ValueError, KeyError):
            meta = json.loads((folder / "snapshot.json").read_text())
            binary = folder / meta["binary"]
            if binary.is_file():
                meta.update(path=str(binary), dir=str(folder), pinned=meta["id"] == pinned)
                result.append(meta)
    result.sort(key=lambda m: m.get("created", ""), reverse=True)
    return result


def create_snapshot(ctx: Context, editor: Path) -> dict | None:
    """Keep a copy of a freshly built editor (plus its .NET assemblies)."""
    digest = sha256_file(editor)
    existing = list_snapshots()
    if existing and existing[0].get("sha256") == digest:
        skip(f"snapshot {existing[0]['id']} already holds this build")
        return existing[0]
    commit = ctx.git_commit or "unknown"
    sid = f"{dt.datetime.now():%Y%m%d-%H%M%S}_{commit[:10]}"
    partial = SNAPSHOT_DIR / f".{sid}.partial"
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    partial.mkdir()
    try:
        copy_fast(editor, partial / editor.name)
        sharp = editor.parent / "GodotSharp"
        has_sharp = ".mono" in editor.name and sharp.is_dir()
        if has_sharp:
            copy_fast(sharp, partial / "GodotSharp")
        size = sum(f.stat().st_size for f in partial.rglob("*") if f.is_file() and not f.is_symlink())
        meta = {
            "id": sid, "binary": editor.name, "commit": commit, "short": commit[:10],
            "subject": git(ctx.source, "log", "-1", "--format=%s"),
            "commit_date": git(ctx.source, "log", "-1", "--format=%cI"),
            "branch": git(ctx.source, "rev-parse", "--abbrev-ref", "HEAD"),
            "version": ctx.version.display, "mono": ".mono" in editor.name, "godotsharp": has_sharp,
            "created": dt.datetime.now().isoformat(timespec="seconds"), "size": size, "sha256": digest,
        }
        (partial / "snapshot.json").write_text(json.dumps(meta, indent=2))
        partial.rename(SNAPSHOT_DIR / sid)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    ok(f"snapshot {sid} saved {S.dim('(' + fmt_size(size) + ')')}")
    return meta


def prune_snapshots(keep: int) -> list[str]:
    removed = []
    for meta in list_snapshots()[max(1, keep):]:
        if meta["pinned"]:
            continue
        shutil.rmtree(meta["dir"], ignore_errors=True)
        removed.append(meta["id"])
    for leftover in SNAPSHOT_DIR.glob(".*.partial") if SNAPSHOT_DIR.is_dir() else ():
        shutil.rmtree(leftover, ignore_errors=True)
    if removed:
        detail(f"pruned {len(removed)} old snapshot(s)")
    return removed


def snapshot_editor(ctx: Context, editor: Path) -> None:
    """Snapshot after a successful build; never fails the build itself."""
    try:
        create_snapshot(ctx, editor)
        prune_snapshots(int(manager_settings()["snapshot_keep"]))
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not snapshot the editor: {exc}")


def point_launcher_at(target: Path) -> None:
    link = launcher_link()
    if link.exists() and not link.is_symlink():
        raise BuildError(f"{link} is a real file, not a symlink: leaving it alone.")
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(".godot.tmp-link")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(link)


def cmd_snapshot(ctx: Context, args: argparse.Namespace) -> int:
    snaps = list_snapshots()
    if args.action == "list":
        emit("snapshots", snapshots=snaps, pinned=pinned_snapshot_id())
        if not JSON_MODE:
            heading("Editor snapshots")
            if not snaps:
                info("No snapshots yet. One is saved after every successful editor build.")
            for s in snaps:
                mark = S.green(" ← in use") if s["pinned"] else ""
                print(f"  {S.cyan(s['id'])}  {s['short']}  {fmt_size(s['size']):>9}  {s['subject'][:60]}{mark}")
        return 0
    if args.action == "create":
        editor = find_editor(ctx)
        if not editor:
            raise BuildError("No editor build to snapshot.")
        create_snapshot(ctx, editor)
        prune_snapshots(int(manager_settings()["snapshot_keep"]))
        return 0
    if not args.id:
        raise BuildError(f"usage: snapshot {args.action} ID" + (" | latest" if args.action == "use" else ""))
    if args.action == "use" and args.id == "latest":
        return 0 if refresh_launcher_link(ctx, unpin=True) else 1
    match = next((s for s in snaps if s["id"] == args.id or s["short"] == args.id), None)
    if not match:
        raise BuildError(f"No snapshot '{args.id}'.")
    if args.action == "use":
        point_launcher_at(Path(match["path"]))
        ok(f"Godot now launches snapshot {match['id']} ({match['short']})")
        detail("A new editor build switches back to the latest automatically.")
        return 0
    if args.action == "delete":
        if match["pinned"]:
            raise BuildError("That snapshot is in use. Switch to another version first.")
        shutil.rmtree(match["dir"])
        ok(f"deleted snapshot {match['id']}")
        return 0
    return 1


# ==============================================================================
#  Changelog: incoming upstream changes, grouped by source area
# ==============================================================================

AREA_LABELS = {
    "core": "Core", "core/crypto": "Crypto", "core/io": "Core I/O", "doc": "Docs", "editor": "Editor",
    "editor/export": "Export", "editor/import": "Import", "editor/plugins": "Editor plugins",
    "main": "Main loop", "misc": "Misc", "scene": "Scene", "scene/2d": "2D", "scene/3d": "3D",
    "scene/gui": "GUI", "scene/animation": "Animation", "scene/resources": "Resources",
    "servers": "Servers", "servers/rendering": "Rendering", "servers/audio": "Audio",
    "servers/physics_3d": "Physics 3D", "servers/physics_2d": "Physics 2D", "servers/display": "Display",
    "drivers": "Drivers", "drivers/vulkan": "Vulkan", "drivers/gles3": "OpenGL", "drivers/d3d12": "Direct3D 12",
    "drivers/metal": "Metal", "platform/linuxbsd": "Linux", "platform/windows": "Windows",
    "platform/macos": "macOS", "platform/android": "Android", "platform/ios": "iOS", "platform/web": "Web",
    "modules/mono": "C# / .NET", "modules/gdscript": "GDScript", "modules/jolt_physics": "Jolt Physics",
    "modules/gltf": "glTF", "modules/navigation_3d": "Navigation", "modules/openxr": "OpenXR",
    "tests": "Tests", "thirdparty": "Third-party", ".github": "CI", "methods.py": "Build system",
    "SConstruct": "Build system", "platform_methods.py": "Build system",
}
TWO_LEVEL_AREAS = {"core", "drivers", "editor", "modules", "platform", "scene", "servers"}


def source_area(path: str) -> str:
    parts = path.split("/")
    if parts[0] in TWO_LEVEL_AREAS and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if len(parts) > 1 else path


def area_label(area: str) -> str:
    if area in AREA_LABELS:
        return AREA_LABELS[area]
    top, _, sub = area.partition("/")
    if sub and top in ("modules", "platform", "drivers"):
        return sub.replace("_", " ").title()
    return AREA_LABELS.get(top) or area


def github_base(ctx: Context) -> str | None:
    url = git(ctx.source, "config", "--get", "remote.origin.url")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"https://github.com/{match.group(1)}/{match.group(2)}" if match else None


MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")


def incoming_changes(ctx: Context, upstream: str) -> list[dict]:
    """Mainline changes in HEAD..upstream: one entry per merged PR or direct commit."""
    raw = git(ctx.source, "log", "--first-parent", "-m", "--name-only", "--no-decorate",
              "--format=%x1e%H%x1f%an%x1f%cI%x1f%s%x1f%b%x1d", f"HEAD..{upstream}")
    watch = [w for w in str(manager_settings()["watch_paths"]).split(",") if w]
    base = github_base(ctx)
    changes = []
    for record in raw.split("\x1e"):
        if "\x1d" not in record:
            continue
        header, _, files_part = record.partition("\x1d")
        fields = header.split("\x1f")
        if len(fields) < 5:
            continue
        sha, author, date, subject, body = fields[0].strip(), fields[1], fields[2], fields[3], fields[4]
        files = [f for f in files_part.strip().splitlines() if f.strip()]
        pr = None
        title = subject
        if m := MERGE_PR_RE.match(subject):
            pr = int(m.group(1))
            title = next((line.strip() for line in body.splitlines() if line.strip()), subject)
            author = m.group(2).split("/")[0]
        counts: dict[str, int] = {}
        for f in files:
            area = source_area(f)
            counts[area] = counts.get(area, 0) + 1
        areas = sorted(counts, key=lambda a: -counts[a])
        watched = sorted({w for w in watch for f in files if f == w or f.startswith(w.rstrip("/") + "/")})
        url = None
        if base:
            url = f"{base}/pull/{pr}" if pr else f"{base}/commit/{sha}"
        changes.append({
            "hash": sha, "short": sha[:10], "pr": pr, "title": title, "subject": subject,
            "author": author, "date": date, "files": len(files),
            "areas": [{"key": a, "label": area_label(a), "files": counts[a]} for a in areas[:6]],
            "watched": watched, "url": url,
        })
    return changes


def fetch_source(ctx: Context, timeout: int = 180) -> str | None:
    """git fetch; returns an error message or None."""
    try:
        result = subprocess.run(["git", "-C", str(ctx.source), "fetch", "--quiet", "--tags"],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "git fetch timed out"
    if result.returncode != 0:
        return (result.stderr.strip().splitlines() or ["unknown error"])[-1]
    save_state({**load_state(), "last_fetch": dt.datetime.now().isoformat(timespec="seconds")})
    return None


# ==============================================================================
#  Launch hook: open Godot, then check for updates in the background
# ==============================================================================

AUTO_CHECK_DELAY = 8  # let the editor start before competing for disk/network


def cmd_launch(args: argparse.Namespace) -> int:
    target = launcher_link()
    if not target.exists():
        try:
            fallback = find_editor(Context(source=SOURCE_DIR, jobs=None, verbose=False, dry_run=False,
                                           assume_yes=True))
        except (SystemExit, SetupRequired):
            fallback = None
        if not fallback:
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", "-a", "Godot", "-i", "dialog-error", "Godot is not built yet",
                                "Open the Build Manager to build the editor."], check=False)
            return 1
        target = fallback
    godot_args = list(args.godot_args or [])
    if godot_args and godot_args[0] == "--":
        godot_args = godot_args[1:]
    subprocess.Popen([str(target), *godot_args], start_new_session=True, stdin=subprocess.DEVNULL)
    with contextlib.suppress(Exception):  # the launch already happened; checks are best effort
        auto_check_after_launch()
    return 0


def auto_check_after_launch() -> None:
    settings = manager_settings()
    if not settings["auto_check"]:
        return
    state = load_state()
    last = state.get("auto_check", {}).get("last")
    if last:
        with contextlib.suppress(ValueError):
            age = (dt.datetime.now() - dt.datetime.fromisoformat(last)).total_seconds()
            if age < int(settings["auto_check_hours"]) * 3600:
                return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / ".auto_check.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # another launch is already checking
        time.sleep(AUTO_CHECK_DELAY)
        try:
            ctx = Context(source=SOURCE_DIR, jobs=None, verbose=False, dry_run=False, assume_yes=True)
        except SetupRequired:
            return
        upstream = git(ctx.source, "rev-parse", "--abbrev-ref", "@{upstream}")
        if not upstream or fetch_source(ctx):
            return
        head = git(ctx.source, "rev-parse", upstream)
        state = load_state()
        previous = state.get("auto_check", {}).get("notified_head")
        state["auto_check"] = {"last": dt.datetime.now().isoformat(timespec="seconds"),
                               "notified_head": previous}
        changes = incoming_changes(ctx, upstream)
        if not changes or head == previous:
            save_state(state)
            return
        state["auto_check"]["notified_head"] = head
        save_state(state)
        notify_changes(changes)


def notify_changes(changes: list[dict]) -> None:
    if not shutil.which("notify-send"):
        return
    count = len(changes)
    watched = [c for c in changes if c["watched"]]
    summary = f"Godot: {count} new change{'s' if count != 1 else ''} upstream"
    lines = []
    if watched:
        areas = sorted({area_label(source_area(w)) for c in watched for w in c["watched"]})
        lines.append(f"{len(watched)} touch {', '.join(areas[:3])}")
    lines += [f"• {c['title'][:70]}" for c in (watched or changes)[:3]]
    cmd = ["notify-send", "-a", "Godot", "-i", "software-update-available", "--expire-time=15000",
           "--action=open=View changes", "--wait", summary, "\n".join(lines)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return
    if result.stdout.strip() == "open":
        python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
        subprocess.Popen([python, str(GUI_SCRIPT), "--page", "source", "--check-updates"],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ==============================================================================
#  Build history and time estimates
# ==============================================================================

HISTORY_MAX = 3000
RETRIEVED_RE = re.compile(r"^Retrieved `.+' from cache")


def ccache_stats() -> dict | None:
    if not shutil.which("ccache"):
        return None
    try:
        out = subprocess.run(["ccache", "--print-stats"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    stats = {}
    for line in out.splitlines():
        key, _, value = line.partition("\t")
        if value.strip().lstrip("-").isdigit():
            stats[key] = int(value)
    if not stats:
        return None
    return {"hits": stats.get("direct_cache_hit", 0) + stats.get("preprocessed_cache_hit", 0),
            "misses": stats.get("cache_miss", 0),
            "size": stats.get("cache_size_kibibyte", 0) * 1024,
            "max_size": stats.get("max_cache_size_kibibyte", 0) * 1024,
            "files": stats.get("files_in_cache", 0)}


def read_history(limit: int | None = None) -> list[dict]:
    entries = []
    with contextlib.suppress(OSError):
        with HISTORY_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    entries.append(json.loads(line))
    return entries[-limit:] if limit else entries


def record_history(entry: dict) -> None:
    with contextlib.suppress(OSError):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if HISTORY_FILE.stat().st_size > 2_000_000:
            keep = read_history(HISTORY_MAX)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(e) + "\n" for e in keep), encoding="utf-8")
            tmp.replace(HISTORY_FILE)


def expected_seconds(state_key: str | None, history: list[dict] | None = None) -> float | None:
    """Median duration of the last few successful runs of this step."""
    if not state_key:
        return None
    runs = [e["seconds"] for e in (history if history is not None else read_history())
            if e.get("key") == state_key and e.get("ok")][-5:]
    if not runs:
        return None
    runs.sort()
    mid = len(runs) // 2
    return round(runs[mid] if len(runs) % 2 else (runs[mid - 1] + runs[mid]) / 2, 1)


def cmd_history(ctx: Context, args: argparse.Namespace) -> int:
    entries = read_history(args.limit)
    if JSON_MODE:
        emit("history", entries=entries)
        return 0
    heading("Build history")
    if not entries:
        info("No builds recorded yet.")
        return 0
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(e["key"] or e["title"], []).append(e)
    for key, runs in sorted(by_key.items()):
        good = [r["seconds"] for r in runs if r["ok"]]
        avg = fmt_duration(sum(good) / len(good)) if good else "—"
        last = runs[-1]
        mark = S.green(ICON["ok"]) if last["ok"] else S.red(ICON["fail"])
        print(f"  {mark} {key:<44} runs {len(runs):>3}  avg {avg:>8}  last {fmt_duration(last['seconds']):>8}")
    return 0


# ==============================================================================
#  Encryption key backups (gpg symmetric, passphrase protected)
# ==============================================================================

BACKUP_FORMAT = "godot-pck-key-backup"
MIN_PASSPHRASE = 8


def read_passphrase(args: argparse.Namespace, confirm_twice: bool) -> str:
    if getattr(args, "passphrase_stdin", False):
        phrase = sys.stdin.readline().rstrip("\r\n")
    else:
        import getpass
        phrase = getpass.getpass("Backup passphrase: ")
        if confirm_twice and getpass.getpass("Repeat passphrase: ") != phrase:
            raise BuildError("Passphrases do not match.")
    if confirm_twice and len(phrase) < MIN_PASSPHRASE:
        raise BuildError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    if not phrase:
        raise BuildError("No passphrase given.")
    return phrase


def gpg_run(gpg_args: list[str], passphrase: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    """Run gpg in a throwaway home so nothing touches the user's keyring or agent cache."""
    import tempfile
    if not shutil.which("gpg"):
        raise BuildError("gpg is not installed.")
    with tempfile.TemporaryDirectory(prefix="godot-key-") as home:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, passphrase.encode() + b"\n")
        os.close(write_fd)
        try:
            result = subprocess.run(
                ["gpg", "--homedir", home, "--batch", "--yes", "--quiet", "--no-symkey-cache",
                 "--pinentry-mode", "loopback", "--passphrase-fd", str(read_fd), *gpg_args],
                input=data, capture_output=True, pass_fds=(read_fd,), timeout=120)
        finally:
            os.close(read_fd)
            subprocess.run(["gpgconf", "--homedir", home, "--kill", "gpg-agent"], capture_output=True)
    return result


def decrypt_backup(path: Path, passphrase: str) -> dict:
    result = gpg_run(["--decrypt", str(path)], passphrase)
    if result.returncode != 0:
        raise BuildError("Could not decrypt the backup: wrong passphrase or damaged file.")
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise BuildError("That file is not a Godot key backup.") from None
    if payload.get("format") != BACKUP_FORMAT or not HEX64.match(str(payload.get("key", ""))):
        raise BuildError("That file is not a Godot key backup.")
    if key_fingerprint(payload["key"]) != payload.get("fingerprint"):
        raise BuildError("Backup is corrupt: fingerprint does not match its key.")
    return payload


def remember_backup(path: Path, fingerprint: str, verified: bool) -> None:
    state = load_state()
    backups = [b for b in state.get("key_backups", []) if b.get("path") != str(path)]
    backups.append({"path": str(path), "fingerprint": fingerprint, "verified": verified,
                    "created": dt.datetime.now().isoformat(timespec="seconds")})
    state["key_backups"] = backups[-20:]
    save_state(state)


def key_backups() -> list[dict]:
    if not key_exists():
        return []
    with contextlib.suppress(BuildError):
        fp = key_fingerprint()
        return [b for b in load_state().get("key_backups", [])
                if b.get("fingerprint") == fp and Path(b.get("path", "")).is_file()]
    return []


def retire_current_key() -> None:
    backup = KEY_FILE.with_name(f"godot.{dt.datetime.now():%Y%m%d-%H%M%S}.gdkey.bak")
    KEY_FILE.replace(backup)
    backup.chmod(0o600)
    info(f"previous key moved to {backup}")


def write_key_file(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.parent.chmod(0o700)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")


def key_backup(args: argparse.Namespace) -> int:
    if not key_exists():
        raise BuildError("No key to back up.")
    key = read_key()
    fp = key_fingerprint(key)
    out = Path(args.path or Path.home() / f"godot-pck-key-{fp}.asc").expanduser().resolve()
    passphrase = read_passphrase(args, confirm_twice=True)
    payload = json.dumps({"format": BACKUP_FORMAT, "version": 1, "key": key, "fingerprint": fp,
                          "created": dt.datetime.now().isoformat(timespec="seconds"),
                          "comment": "Godot PCK encryption key. Restore with: godot_build.py key restore FILE"})
    tmp = out.with_name(f".{out.name}.tmp")
    result = gpg_run(["--symmetric", "--armor", "--cipher-algo", "AES256", "--s2k-mode", "3",
                      "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712", "--output", str(tmp)],
                     passphrase, payload.encode())
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise BuildError("gpg failed: " + result.stderr.decode(errors="replace").strip()[-200:])
    tmp.chmod(0o600)
    tmp.replace(out)
    decrypt_backup(out, passphrase)  # prove it round-trips before calling it a backup
    remember_backup(out, fp, verified=True)
    emit("key_backup", path=str(out), fingerprint=fp)
    ok(f"Key backed up to {out}")
    detail("Store it somewhere other than this disk (cloud drive, USB stick, password manager).")
    return 0


def key_check_backup(args: argparse.Namespace) -> int:
    if not args.path:
        raise BuildError("usage: key check-backup FILE")
    path = Path(args.path).expanduser().resolve()
    payload = decrypt_backup(path, read_passphrase(args, confirm_twice=False))
    current = key_fingerprint() if key_exists() else None
    matches = payload["fingerprint"] == current
    if matches:
        remember_backup(path, current, verified=True)
        ok(f"Backup is valid and matches the current key ({current}).")
    else:
        warn(f"Backup is valid but holds a different key ({payload['fingerprint']}).")
    emit("key_backup_check", path=str(path), fingerprint=payload["fingerprint"], matches=matches)
    return 0 if matches else 1


def key_restore(args: argparse.Namespace) -> int:
    if not args.path:
        raise BuildError("usage: key restore FILE")
    path = Path(args.path).expanduser().resolve()
    payload = decrypt_backup(path, read_passphrase(args, confirm_twice=False))
    if key_exists():
        current = key_fingerprint() if HEX64.match(KEY_FILE.read_text().strip()) else None
        if current == payload["fingerprint"]:
            ok("The current key already matches this backup.")
            remember_backup(path, current, verified=True)
            return 0
        if not args.force:
            raise BuildError(f"A different key is in place ({current}). Use --force to replace it.")
        retire_current_key()
    write_key_file(payload["key"])
    remember_backup(path, payload["fingerprint"], verified=True)
    emit("key", exists=True, fingerprint=payload["fingerprint"], path=str(KEY_FILE))
    ok(f"Key {payload['fingerprint']} restored to {KEY_FILE}")
    return 0


# ==============================================================================
#  Storage report
# ==============================================================================


def du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        out = subprocess.run(["du", "-s", "-B1", str(path)], capture_output=True, text=True, timeout=300).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def storage_items(ctx: Context) -> list[dict]:
    scons_cache = os.path.expanduser(custom_py_value(ctx.source, "_CACHE_DIR"))
    limit = custom_py_value(ctx.source, "_CACHE_LIMIT_GIB")
    items = [
        {"id": "objects", "label": "Object files", "path": str(ctx.bin_dir / "obj"),
         "description": "Intermediate build output. Clearing forces a full rebuild of every platform.",
         "clearable": True, "warning": True},
        {"id": "binaries", "label": "Built binaries", "path": str(ctx.bin_dir),
         "description": "Editor and template binaries in bin/ (object files excluded).", "clearable": False},
        {"id": "scons_cache", "label": "SCons cache", "path": scons_cache or "",
         "description": f"Reused objects across rebuilds{f' · limit {limit} GiB' if limit else ''}.",
         "clearable": bool(scons_cache), "warning": True},
        {"id": "ccache", "label": "ccache", "path": "",
         "description": "Compiler cache shared by every build on this machine.",
         "clearable": shutil.which("ccache") is not None, "warning": True},
        {"id": "snapshots", "label": "Editor snapshots", "path": str(SNAPSHOT_DIR),
         "description": "Previous editor builds kept for rollback. Clearing keeps the one in use.",
         "clearable": True},
        {"id": "dist", "label": "Staged templates", "path": str(DIST_DIR),
         "description": "Templates and .tpz packages waiting to be installed.", "clearable": True},
        {"id": "logs", "label": "Build logs", "path": str(LOG_DIR),
         "description": "Logs of every build. History and timing data are kept.", "clearable": True},
        {"id": "nuget", "label": "Local NuGet feed", "path": str(NUGET_LOCAL_DIR),
         "description": "Godot .NET packages your C# projects restore from. Rebuild the editor to recreate them.",
         "clearable": True},
    ]
    if USER_TEMPLATES_DIR.is_dir():
        for d in sorted(USER_TEMPLATES_DIR.iterdir()):
            if d.is_dir():
                items.append({"id": f"templates:{d.name}", "label": d.name, "path": str(d), "group": "templates",
                              "description": "Installed export templates.", "clearable": True,
                              "current": d.name == ctx.version.template_dir_name(ctx.mono_default)})

    from concurrent.futures import ThreadPoolExecutor
    cc = ccache_stats()

    def size_of(item: dict) -> int:
        if item["id"] == "ccache":
            return cc["size"] if cc else 0
        if item["id"] == "binaries":
            return max(0, du_bytes(ctx.bin_dir) - du_bytes(ctx.bin_dir / "obj"))
        return du_bytes(Path(item["path"])) if item["path"] else 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        for item, size in zip(items, pool.map(size_of, items)):
            item["size"] = size
    return items


def clear_storage(ctx: Context, item_id: str) -> None:
    def empty_dir(path: Path) -> None:
        if path.is_dir():
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()

    if item_id == "objects":
        shutil.rmtree(ctx.bin_dir / "obj", ignore_errors=True)
    elif item_id == "scons_cache":
        cache = custom_py_value(ctx.source, "_CACHE_DIR")
        if not cache:
            raise BuildError("No SCons cache configured.")
        empty_dir(Path(cache).expanduser())
    elif item_id == "ccache":
        subprocess.run(["ccache", "--clear"], check=True, capture_output=True)
    elif item_id == "snapshots":
        for meta in list_snapshots():
            if not meta["pinned"]:
                shutil.rmtree(meta["dir"], ignore_errors=True)
    elif item_id == "dist":
        empty_dir(DIST_DIR)
    elif item_id == "logs":
        for log in LOG_DIR.glob("*.log"):
            log.unlink()
    elif item_id == "nuget":
        empty_dir(NUGET_LOCAL_DIR)
    elif item_id.startswith("templates:"):
        name = item_id.split(":", 1)[1]
        target = (USER_TEMPLATES_DIR / name).resolve()
        if target.parent != USER_TEMPLATES_DIR.resolve() or not target.is_dir():
            raise BuildError(f"No installed templates named '{name}'.")
        shutil.rmtree(target)
    else:
        raise BuildError(f"Unknown storage item '{item_id}'.")


def cmd_storage(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "clear":
        if not args.item:
            raise BuildError("usage: storage clear ITEM")
        if ctx.dry_run or not confirm(f"Clear {args.item}?", ctx):
            return 0
        clear_storage(ctx, args.item)
        ok(f"cleared {args.item}")
        return 0
    items = storage_items(ctx)
    disk = shutil.disk_usage(GODOT_ROOT)
    cc = ccache_stats()
    if JSON_MODE:
        emit("storage", items=items, disk={"path": str(GODOT_ROOT), "total": disk.total, "used": disk.used,
                                           "free": disk.free}, ccache=cc)
        return 0
    heading("Storage")
    for item in items:
        print(f"  {item['label']:<28} {fmt_size(item['size']):>10}  {S.dim(item['path'])}")
    total = sum(i["size"] for i in items)
    info(f"total {fmt_size(total)} · disk free {fmt_size(disk.free)} of {fmt_size(disk.total)}")
    if cc and cc["hits"] + cc["misses"]:
        info(f"ccache hit rate {100 * cc['hits'] / (cc['hits'] + cc['misses']):.0f}%")
    return 0


# ==============================================================================
#  Cross-compile toolchains
# ==============================================================================

OSXCROSS_REPO = "https://github.com/tpoechtrager/osxcross.git"
XCODE_DOWNLOADS = "https://developer.apple.com/download/all/?q=xcode"
SDK_ARCHIVE_RE = re.compile(r"^MacOSX([\d.]*)\.sdk\.tar\.(xz|bz2|gz|zst)$")
MVK_LIB = Path("macos-arm64_x86_64") / "libMoltenVK.a"
OSXCROSS_TOOLS = ("git", "clang", "cmake", "make", "patch", "xz")
OSXCROSS_PACKAGES = {
    "pacman": "sudo pacman -S --needed clang cmake git patch python libxml2 openssl xz bzip2 zlib cpio make",
    "apt-get": "sudo apt install clang cmake git patch python3 libssl-dev liblzma-dev libxml2-dev zlib1g-dev "
               "libbz2-dev cpio make",
    "dnf": "sudo dnf install clang cmake git patch python3 openssl-devel xz-devel libxml2-devel zlib-devel "
           "bzip2-devel cpio make",
    "zypper": "sudo zypper install clang cmake git patch python3 libopenssl-devel xz-devel libxml2-devel "
              "zlib-devel libbz2-devel cpio make",
}


def osxcross_packages() -> str:
    return next((cmd for tool, cmd in OSXCROSS_PACKAGES.items() if shutil.which(tool)), "")


def moltenvk_dir() -> Path | None:
    for candidate in (manager_settings().get("moltenvk_sdk"), TOOLCHAIN_DIR / "moltenvk"):
        if candidate and (Path(str(candidate)).expanduser() / MVK_LIB).is_file():
            return Path(str(candidate)).expanduser()
    return None


def macos_vulkan_args(source: Path) -> list[str]:
    """SCons args that link MoltenVK into macOS builds (unless custom.py already chose)."""
    if custom_py_value(source, "_MOLTENVK_SDK"):
        return []
    if not manager_settings().get("macos_vulkan", True):
        return ["vulkan=no"]
    mvk = moltenvk_dir()
    return ["vulkan=yes", f"vulkan_sdk_path={mvk}"] if mvk else []


def remember_toolchain(key: str, path: Path | None) -> None:
    if key in {n for n, *_ in MANAGER_SPEC}:
        write_manager(key, str(path) if path else "")


def _version_file(path: Path) -> str:
    with contextlib.suppress(OSError):
        return (path / ".gbm-version").read_text().strip()
    return ""


def toolchain_status() -> dict:
    osx_root, osx_sdk = find_osxcross()
    mingw = (LLVM_MINGW_DIR / "bin" / "x86_64-w64-mingw32-clang").exists()
    mvk = moltenvk_dir()
    tarballs = TOOLCHAIN_DIR / "osxcross" / "tarballs"
    return {
        "dir": str(TOOLCHAIN_DIR),
        "mingw": {"installed": mingw, "path": str(LLVM_MINGW_DIR) if mingw else None,
                  "version": _version_file(LLVM_MINGW_DIR) if mingw else ""},
        "osxcross": {"installed": bool(osx_root), "path": str(osx_root) if osx_root else None, "sdk": osx_sdk,
                     "sdk_archives": sorted(p.name for p in tarballs.glob("MacOSX*.sdk.tar.*"))
                     if tarballs.is_dir() else [],
                     "missing_tools": [t for t in OSXCROSS_TOOLS if not shutil.which(t)],
                     "packages": osxcross_packages()},
        "moltenvk": {"installed": bool(mvk), "path": str(mvk) if mvk else None,
                     "version": _version_file(mvk) if mvk else "",
                     "enabled": bool(manager_settings().get("macos_vulkan", True))},
    }


def github_latest(repo: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "godot-build"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (OSError, ValueError) as exc:
        raise BuildError(f"Could not reach GitHub for {repo}: {exc}") from exc


def download(url: str, dest: Path, title: str, size: int = 0) -> None:
    """Download with progress events (the Activity page shows a percentage bar)."""
    if dest.is_file() and size and dest.stat().st_size == size:
        ok(f"{dest.name} already downloaded")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    task_id = f"download:{dest.name}"
    emit("task_start", id=task_id, title=title, log=None, expected=100)
    info(f"{title} {S.dim('… ' + url)}")
    last = -1
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "godot-build"})
        with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or size or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else 0
                if pct != last or not total:
                    last = pct
                    text = f"{fmt_size(done)} of {fmt_size(total)}  ({pct}%)" if total else fmt_size(done)
                    emit("line", id=task_id, text=text, steps=pct, expected=100, error=False)
                    if not JSON_MODE and sys.stdout.isatty():
                        print(f"\r    {text}   ", end="", flush=True)
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        emit("task_end", id=task_id, ok=False, code=1)
        raise BuildError(f"Download failed: {exc}") from exc
    if not JSON_MODE and sys.stdout.isatty():
        print()
    part.replace(dest)
    emit("task_end", id=task_id, ok=True, code=0)


def _plain_ctx() -> argparse.Namespace:
    return argparse.Namespace(verbose=False, git_commit="")


def extract(archive: Path, name: str) -> Path:
    """Unpack into a scratch folder and return the archive's single top-level folder."""
    staging = TOOLCHAIN_DIR / ".extract"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    if not run_logged(["tar", "-xf", str(archive), "-C", str(staging)], cwd=TOOLCHAIN_DIR, env=os.environ.copy(),
                      log_path=new_log_path(f"extract_{name}"), title=f"Extracting {archive.name}",
                      state_key=None, ctx=_plain_ctx(), task_id=f"extract:{name}"):
        raise BuildError(f"Could not extract {archive.name}")
    entries = list(staging.iterdir())
    return entries[0] if len(entries) == 1 else staging


def replace_dir(src: Path, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    shutil.rmtree(TOOLCHAIN_DIR / ".extract", ignore_errors=True)


def install_mingw() -> Path:
    heading("llvm-mingw (Windows)")
    release = github_latest("mstorsjo/llvm-mingw")
    arch = {"x86_64": "x86_64", "arm64": "aarch64"}.get(HOST_ARCH, HOST_ARCH)
    asset = next((a for a in release.get("assets", [])
                  if re.fullmatch(rf"llvm-mingw-.+-ucrt-ubuntu-[\d.]+-{arch}\.tar\.xz", a["name"])), None)
    if not asset:
        raise BuildError(f"llvm-mingw {release.get('tag_name', '')} has no Linux {arch} build.")
    archive = TOOLCHAIN_DIR / "downloads" / asset["name"]
    download(asset["browser_download_url"], archive, f"Downloading llvm-mingw {release['tag_name']}", asset["size"])
    dest = TOOLCHAIN_DIR / "llvm-mingw"
    replace_dir(extract(archive, "llvm-mingw"), dest)
    archive.unlink(missing_ok=True)
    if not (dest / "bin" / "x86_64-w64-mingw32-clang").exists():
        raise BuildError(f"{dest} does not contain x86_64-w64-mingw32-clang")
    (dest / ".gbm-version").write_text(release["tag_name"] + "\n")
    remember_toolchain("llvm_mingw", dest)
    ok(f"llvm-mingw {release['tag_name']} installed in {dest}")
    return dest


def install_moltenvk() -> Path:
    heading("MoltenVK (Vulkan on macOS)")
    release = github_latest("KhronosGroup/MoltenVK")
    asset = next((a for a in release.get("assets", []) if a["name"] == "MoltenVK-macos.tar"), None)
    if not asset:
        raise BuildError(f"MoltenVK {release.get('tag_name', '')} has no MoltenVK-macos.tar download.")
    archive = TOOLCHAIN_DIR / "downloads" / asset["name"]
    download(asset["browser_download_url"], archive, f"Downloading MoltenVK {release['tag_name']}", asset["size"])
    top = extract(archive, "moltenvk")
    framework = top / "MoltenVK" / "static" / "MoltenVK.xcframework"
    if not (framework / MVK_LIB).is_file():
        raise BuildError("Unexpected MoltenVK archive layout (no static MoltenVK.xcframework).")
    dest = TOOLCHAIN_DIR / "moltenvk"
    replace_dir(framework, dest)
    archive.unlink(missing_ok=True)
    (dest / ".gbm-version").write_text(release["tag_name"] + "\n")
    ok(f"MoltenVK {release['tag_name']} installed in {dest}")
    if not manager_settings().get("macos_vulkan", True):
        warn("Vulkan on macOS is turned off in settings (macos_vulkan), so MoltenVK is not used yet.")
    return dest


def _sdk_version(name: str) -> tuple[int, ...]:
    match = SDK_ARCHIVE_RE.match(name)
    return tuple(int(p) for p in match.group(1).split(".") if p) if match else ()


def install_osxcross(sdk: str | None) -> Path:
    heading("osxcross (macOS)")
    root = TOOLCHAIN_DIR / "osxcross"
    tarballs = root / "tarballs"
    sdk_path = Path(sdk).expanduser().resolve() if sdk else None
    if sdk_path and not sdk_path.is_file():
        raise BuildError(f"{sdk_path} does not exist.")
    if sdk_path and sdk_path.suffix != ".xip" and not SDK_ARCHIVE_RE.match(sdk_path.name):
        raise BuildError("Expected an Xcode .xip or a MacOSX<version>.sdk.tar.xz archive.")
    if not sdk_path and not list(tarballs.glob("MacOSX*.sdk.tar.*")):
        raise BuildError("osxcross needs Apple's macOS SDK, which only ships inside Xcode. Download Xcode (.xip, "
                         f"free Apple account) from {XCODE_DOWNLOADS} and pass it with --sdk, or pass a "
                         "MacOSX<version>.sdk.tar.xz packaged on a Mac with osxcross's gen_sdk_package.sh.")
    missing = [t for t in OSXCROSS_TOOLS if not shutil.which(t)]
    if sdk_path and sdk_path.suffix == ".xip" and not shutil.which("cpio"):
        missing.append("cpio")
    if missing:
        hint = osxcross_packages()
        raise BuildError(f"Building osxcross needs: {', '.join(missing)}." + (f" Install with: {hint}" if hint else ""))

    env = os.environ.copy()
    if not (root / "build.sh").is_file():
        shutil.rmtree(root, ignore_errors=True)
        root.parent.mkdir(parents=True, exist_ok=True)
        if not run_logged(["git", "clone", "--depth", "1", "--progress", OSXCROSS_REPO, str(root)],
                          cwd=root.parent, env=env, log_path=new_log_path("osxcross_clone"),
                          title="Cloning osxcross", state_key=None, ctx=_plain_ctx(), task_id="osxcross:clone"):
            raise BuildError("Could not clone osxcross.")
    tarballs.mkdir(parents=True, exist_ok=True)
    if sdk_path and sdk_path.suffix == ".xip":
        if not run_logged(["./tools/gen_sdk_package_pbzx.sh", str(sdk_path)], cwd=root, env=env,
                          log_path=new_log_path("osxcross_sdk"), title="Extracting the macOS SDK from Xcode",
                          state_key=None, ctx=_plain_ctx(), task_id="osxcross:sdk"):
            raise BuildError("Could not extract the SDK from Xcode (see the log).")
        packaged = list(root.glob("MacOSX*.sdk.tar.*"))
        if not packaged:
            raise BuildError("Xcode was unpacked but no MacOSX SDK archive was produced.")
        for archive in packaged:
            shutil.move(str(archive), str(tarballs / archive.name))
    elif sdk_path:
        shutil.copy2(sdk_path, tarballs / sdk_path.name)

    newest = max((p.name for p in tarballs.glob("MacOSX*.sdk.tar.*")), key=_sdk_version)
    version = ".".join(map(str, _sdk_version(newest)))
    if version:
        env["SDK_VERSION"] = version
    env["UNATTENDED"] = "1"
    info(f"building osxcross with {newest} (takes 5–20 minutes)")
    if not run_logged(["./build.sh"], cwd=root, env=env, log_path=new_log_path("osxcross_build"),
                      title=f"Building osxcross (macOS SDK {version or '?'})", state_key="osxcross_build",
                      ctx=_plain_ctx(), task_id="osxcross:build"):
        raise BuildError("osxcross failed to build. Missing development headers are the usual cause"
                         + (f": {osxcross_packages()}" if osxcross_packages() else "."))
    if not list((root / "target" / "bin").glob("x86_64-apple-darwin*-clang")):
        raise BuildError("osxcross built but no x86_64-apple-darwin clang wrapper was found in target/bin.")
    (root / ".gbm-version").write_text(f"SDK {version}\n")
    remember_toolchain("osxcross_root", root)
    ok(f"osxcross installed in {root}")
    return root


def cmd_toolchain(_ctx: None, args: argparse.Namespace) -> int:
    if args.action == "status":
        status = toolchain_status()
        if JSON_MODE:
            emit("toolchains", **status)
            return 0
        heading("Toolchains")
        for key, label in (("mingw", "llvm-mingw (Windows)"), ("osxcross", "osxcross (macOS)"),
                           ("moltenvk", "MoltenVK (macOS Vulkan)")):
            item = status[key]
            if item["installed"]:
                ok(f"{label:<26} {item['path']}  {S.dim(item.get('version') or item.get('sdk') or '')}")
            else:
                warn(f"{label:<26} not installed  {S.dim('godot-build toolchain install ' + key)}")
        return 0
    names = ["mingw", "moltenvk", "osxcross"] if args.name == "all" else [args.name]
    if args.action == "remove":
        for name in names:
            folder = TOOLCHAIN_DIR / {"mingw": "llvm-mingw"}.get(name, name)
            if folder.is_dir():
                shutil.rmtree(folder)
                ok(f"removed {folder}")
            if (name == "mingw" and LLVM_MINGW_DIR == folder) or name == "osxcross":
                remember_toolchain({"mingw": "llvm_mingw", "osxcross": "osxcross_root"}[name], None)
        emit("toolchains", **toolchain_status())
        return 0
    failed = 0
    for name in names:
        try:
            if name == "mingw":
                install_mingw()
            elif name == "moltenvk":
                install_moltenvk()
            elif args.name == "all" and not args.sdk and not list(
                    (TOOLCHAIN_DIR / "osxcross" / "tarballs").glob("MacOSX*.sdk.tar.*")):
                skip("osxcross: pass --sdk with an Xcode .xip or MacOSX SDK archive to build it")
            else:
                install_osxcross(args.sdk)
        except BuildError as exc:
            fail(str(exc))
            failed += 1
    emit("toolchains", **toolchain_status())
    if not failed and "mingw" in names:
        info("Windows builds also need ANGLE / D3D12 / Mesa: run  godot-build deps")
    return 1 if failed else 0


# ==============================================================================
#  First-run setup
# ==============================================================================


def validate_source(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not (path / "SConstruct").is_file() or not (path / "version.py").is_file():
        raise BuildError(f"{path} is not a Godot source checkout (no SConstruct / version.py).")
    return path


def cmd_setup(_ctx: None, args: argparse.Namespace) -> int:
    heading("Setup")
    changed = False
    for flag, key in (("source_dir", "source_dir"), ("workspace", "workspace_dir"),
                      ("llvm_mingw", "llvm_mingw"), ("osxcross", "osxcross_root")):
        value = getattr(args, flag, None)
        if value is None:
            continue
        write_manager(key, value)
        ok(f"{key} = {value or '(default)'}")
        changed = True
    settings = read_manager()
    emit("config", settings=[], manager=settings)
    if not changed:
        for s in settings:
            if s["type"] == "path":
                info(f"{s['name']:<14} {s['value'] or S.dim('(default)')}")
    return 0


def cmd_clone(_ctx: None, args: argparse.Namespace) -> int:
    heading("Clone Godot")
    dest = Path(args.dest).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        if (dest / "SConstruct").is_file():
            info("That folder already holds a Godot checkout; using it.")
            write_manager("source_dir", str(dest))
            return 0
        raise BuildError(f"{dest} exists and is not empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--progress", "--branch", args.branch]
    if args.shallow:
        cmd += ["--depth", "1"]
    cmd += [args.url, str(dest)]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plain = argparse.Namespace(verbose=False, git_commit="")
    if not run_logged(cmd, cwd=dest.parent, env=os.environ.copy(), log_path=new_log_path("clone"),
                      title=f"Cloning Godot ({args.branch})", state_key=None, ctx=plain, task_id="clone"):
        return 1
    write_manager("source_dir", str(dest))
    ok(f"Godot cloned to {dest} and set as the source tree")
    if CUSTOM_PY_TEMPLATE.is_file() and not (dest / "custom.py").exists():
        shutil.copy2(CUSTOM_PY_TEMPLATE, dest / "custom.py")
        ok("installed the recommended custom.py")
    return 0


def install_custom_py(ctx: Context, force: bool) -> None:
    target = ctx.source / "custom.py"
    if not CUSTOM_PY_TEMPLATE.is_file():
        raise BuildError(f"Template missing: {CUSTOM_PY_TEMPLATE}")
    if target.exists():
        if not force:
            raise BuildError("custom.py already exists. Use --force to replace it (a backup is kept).")
        backup = target.with_name(f"custom.py.{dt.datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(target, backup)
        info(f"previous custom.py saved as {backup.name}")
    shutil.copy2(CUSTOM_PY_TEMPLATE, target)
    ok(f"installed {target}")


# ==============================================================================
#  Status snapshot (used by the GUI)
# ==============================================================================


def log_summaries(limit: int = 60) -> list[dict]:
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    result = []
    for path in logs:
        title, code = path.stem, None
        with contextlib.suppress(OSError):
            with path.open("rb") as fh:
                head = fh.read(512).decode(errors="replace")
                fh.seek(max(0, path.stat().st_size - 256))
                tail = fh.read().decode(errors="replace")
            if m := re.search(r"^# title: (.+)$", head, re.M):
                title = m.group(1)
            if m := re.search(r"# exit code (-?\d+)", tail):
                code = int(m.group(1))
            elif "# interrupted" in tail:
                code = 130
        result.append({"path": str(path), "name": path.name, "title": title, "exit_code": code,
                       "size": path.stat().st_size, "modified": path.stat().st_mtime})
    return result


def status_snapshot(ctx: Context) -> dict:
    editor = find_editor(ctx)
    link = launcher_link()
    upstream = git(ctx.source, "rev-parse", "--abbrev-ref", "@{upstream}")
    behind = git(ctx.source, "rev-list", "--count", f"HEAD..{upstream}") if upstream else ""
    key: dict = {"exists": key_exists(), "path": str(KEY_FILE)}
    if key["exists"]:
        try:
            key.update(fingerprint=key_fingerprint(), valid=True, mode=oct(KEY_FILE.stat().st_mode & 0o777))
        except BuildError:
            key.update(valid=False)
        backups = key_backups()
        key.update(backups=backups, backed_up=bool(backups))

    staged = {}
    for mono in (False, True):
        name = ctx.version.template_dir_name(mono)
        manifest = load_manifest(ctx, mono)
        if manifest.get("files"):
            staged[name] = {"dir": str(staging_dir(ctx, mono)), "mono": mono, "files": manifest["files"]}

    installed = []
    if USER_TEMPLATES_DIR.is_dir():
        for d in sorted(USER_TEMPLATES_DIR.iterdir()):
            if d.is_dir():
                files = [p.name for p in d.iterdir() if p.is_file()]
                installed.append({"name": d.name, "path": str(d), "files": sorted(files)})

    platforms = {}
    for p in PLATFORMS.values():
        available, reason = ctx.platform_available(p)
        platforms[p.key] = {"label": p.label, "available": available, "reason": "" if available else reason,
                            "default_arches": list(p.default_arches), "supported_arches": list(p.supported_arches)}

    state = load_state()
    return {
        "version": {"display": ctx.version.display, "number": ctx.version.number, "status": ctx.version.status,
                    "template_dir": ctx.version.template_dir_name(ctx.mono_default)},
        "source": str(ctx.source),
        "git": {"commit": ctx.git_commit, "short": ctx.git_commit[:10],
                "branch": git(ctx.source, "rev-parse", "--abbrev-ref", "HEAD"),
                "upstream": upstream, "behind": int(behind) if behind.isdigit() else None,
                "dirty": bool(git(ctx.source, "status", "--porcelain", "-uno")),
                "subject": git(ctx.source, "log", "-1", "--format=%s"),
                "date": git(ctx.source, "log", "-1", "--format=%cI"),
                "last_fetch": state.get("last_fetch")},
        "mono_default": ctx.mono_default,
        "editor": {"path": str(editor) if editor else None, "name": editor.name if editor else None,
                   "size": editor.stat().st_size if editor else None,
                   "modified": editor.stat().st_mtime if editor else None,
                   "mono": bool(editor and ".mono" in editor.name),
                   "godotsharp": (ctx.bin_dir / "GodotSharp").is_dir()},
        "launcher": {"path": str(link), "is_symlink": link.is_symlink(),
                     "target": os.readlink(link) if link.is_symlink() else None,
                     "current": bool(editor and link.exists() and link.resolve() == editor.resolve()),
                     "snapshot": pinned_snapshot_id(),
                     "desktop_entry": DESKTOP_FILE.is_file()},
        "key": key,
        "platforms": platforms,
        "osxcross": {"root": str(ctx.osxcross_root) if ctx.osxcross_root else None, "sdk": ctx.osxcross_sdk},
        "toolchains": toolchain_status(),
        "staged": staged,
        "installed_templates": installed,
        "paths": {"dist": str(DIST_DIR), "logs": str(LOG_DIR), "templates": str(USER_TEMPLATES_DIR),
                  "custom_py": str(ctx.source / "custom.py"), "config": str(CONFIG_DIR),
                  "workspace": str(WORKSPACE),
                  "nuget": str(NUGET_LOCAL_DIR), "snapshots": str(SNAPSHOT_DIR)},
        "logs": log_summaries(),
        "settings": read_config(ctx),
        "manager": read_manager(),
        "snapshots": list_snapshots(),
        "cpu_count": os.cpu_count(),
        "host_arch": HOST_ARCH,
        "workspace": str(WORKSPACE),
    }


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    snap = status_snapshot(ctx)
    if JSON_MODE:
        emit("status", **snap)
        return 0
    heading("Status")
    ed = snap["editor"]
    info(f"editor     {ed['name'] or S.yellow('not built')}")
    info(f"launcher   {snap['launcher']['path']} → {snap['launcher']['target'] or S.yellow('missing')}")
    behind, upstream = snap["git"]["behind"], snap["git"]["upstream"]
    lag = S.yellow(f"{behind} behind {upstream}") if behind else ""
    info(f"source     {snap['git']['short']} on {snap['git']['branch']}  {lag}")
    info(f"key        {key_status_line()}")
    for name, stage in snap["staged"].items():
        info(f"staged     {name}: {len(stage['files'])} file(s)")
    return 0


# ==============================================================================
#  Interactive menu
# ==============================================================================

MENU = [
    ("Doctor", "check toolchains, SDKs and key", ["doctor"]),
    ("Update source", "git fetch + fast-forward", ["update"]),
    ("Build editor", "Linux editor (+ .NET glue)", ["editor"]),
    ("Build templates", "every available platform, then install", ["templates", "--install"]),
    ("Build Linux templates", "release + debug", ["templates", "-p", "linux", "--install"]),
    ("Build Windows templates", "release + debug", ["templates", "-p", "windows", "--install"]),
    ("Build macOS templates", "Universal 2, release + debug", ["templates", "-p", "macos", "--install"]),
    ("Full pipeline", "doctor → editor → templates → install", ["all"]),
    ("Generate encryption key", "AES-256, stored 0600", ["key", "generate"]),
    ("Verify encryption", "check key is embedded in staged templates", ["key", "verify"]),
    ("Install staged templates", "copy into the editor + .tpz", ["install", "--tpz"]),
    ("Desktop integration", "launcher, .desktop entry, icons, MIME", ["integrate"]),
]


def interactive(parser: argparse.ArgumentParser, base: list[str]) -> int:
    while True:
        heading("Menu")
        for i, (name, desc, _) in enumerate(MENU, 1):
            print(f"  {S.cyan(f'{i:>2}')}  {name:<26} {S.dim(desc)}")
        print(f"  {S.cyan(' q')}  {'Quit'}")
        try:
            choice = input(f"\n  {S.bold(ICON['arrow'])} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit", ""):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            warn("Pick a number from the list.")
            continue
        argv = base + MENU[int(choice) - 1][2]
        code = dispatch(parser, argv, show_banner=False)
        print()
        (ok if code == 0 else fail)(f"finished with exit code {code}")


# ==============================================================================
#  CLI
# ==============================================================================


def common_options(subcommand: bool) -> argparse.ArgumentParser:
    """Global flags, accepted before or after the command.

    Subcommands use SUPPRESS defaults so `-v templates` isn't reset to False
    by the subparser (a long-standing argparse quirk).
    """
    d = (lambda value: argparse.SUPPRESS) if subcommand else (lambda value: value)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-j", "--jobs", type=int, default=d(None),
                        help="parallel compile jobs (default: Godot picks cores - 1)")
    common.add_argument("-v", "--verbose", action="store_true", default=d(False), help="stream full SCons output")
    common.add_argument("-n", "--dry-run", action="store_true", default=d(False),
                        help="show what would run, change nothing")
    common.add_argument("-y", "--yes", action="store_true", default=d(False), help="answer yes to confirmations")
    common.add_argument("--source", type=Path, default=d(SOURCE_DIR), help=f"Godot source tree (default {SOURCE_DIR})")
    common.add_argument("--no-color", action="store_true", default=d(False), help="disable colours")
    common.add_argument("--json", action="store_true", default=d(False),
                        help="machine-readable JSON-lines output (used by the GUI); implies --yes")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = common_options(subcommand=True)

    mono = argparse.ArgumentParser(add_help=False)
    group = mono.add_mutually_exclusive_group()
    group.add_argument("--mono", dest="mono", action="store_true", default=None, help="build with .NET support")
    group.add_argument("--no-mono", dest="mono", action="store_false", help="build without .NET support")

    templates = argparse.ArgumentParser(add_help=False)
    templates.add_argument("-p", "--platforms", default="all",
                           help="comma list: linux,windows,macos or 'all' (default: all available)")
    templates.add_argument("-t", "--targets", default="release,debug", help="comma list: release,debug")
    templates.add_argument("--arch", help="comma list of arches (default per platform; macOS builds arm64+x86_64)")
    templates.add_argument("--no-encrypt", action="store_true", help="build without the PCK encryption key")
    templates.add_argument("--require-encryption", action="store_true",
                           help="fail instead of warning when no key exists")
    templates.add_argument("--keep-going", action="store_true", help="continue after a failed build")
    templates.add_argument("--install", action="store_true", help="install into the editor's template folder")
    templates.add_argument("--tpz", action="store_true", help="also package a .tpz archive")
    templates.add_argument("scons_args", nargs="*", help="extra SCons arguments after --, e.g. -- verbose=yes")

    editor = argparse.ArgumentParser(add_help=False)
    editor.add_argument("--no-glue", action="store_true", help="skip .NET glue + assemblies")

    parser = argparse.ArgumentParser(
        prog="godot_build.py",
        description="Godot Engine production build pipeline: editor, export templates for "
                    "Linux / Windows / macOS, PCK encryption, packaging.",
        parents=[common_options(subcommand=False)],
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("doctor", parents=[common], help="check toolchains and dependencies")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("update", parents=[common], help="fetch and fast-forward the source tree")
    p.add_argument("--check", action="store_true", help="only fetch and list incoming commits")
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("status", parents=[common], help="snapshot of editor, source, key and templates")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("integrate", parents=[common],
                       help="install launcher symlink, .desktop entries (with Build Manager action), icons, MIME")
    p.add_argument("--remove", action="store_true", help="remove everything integrate installed")
    p.set_defaults(func=cmd_integrate)
    p = sub.add_parser("config", parents=[common], help="read or change custom.py toggles")
    p.add_argument("action", nargs="?", choices=["list", "set", "install-template"], default="list")
    p.add_argument("name", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--force", action="store_true", help="install-template: replace an existing custom.py")
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("setup", parents=[common], help="configure the source tree, workspace and toolchains")
    p.add_argument("--source-dir", dest="source_dir", help="Godot source checkout")
    p.add_argument("--workspace", help="folder for logs, templates, snapshots and the key")
    p.add_argument("--llvm-mingw", dest="llvm_mingw", help="llvm-mingw toolchain folder")
    p.add_argument("--osxcross", help="osxcross folder")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("clone", parents=[common], help="clone Godot from GitHub and use it as the source tree")
    p.add_argument("dest", help="folder to clone into")
    p.add_argument("--branch", default="master", help="branch or tag (default: master)")
    p.add_argument("--url", default="https://github.com/godotengine/godot.git")
    p.add_argument("--shallow", action="store_true", help="history depth 1 (smaller, but no changelog)")
    p.set_defaults(func=cmd_clone)
    p = sub.add_parser("editor", parents=[common, mono, editor], help="build the Linux editor")
    p.set_defaults(func=cmd_editor)
    p = sub.add_parser("templates", parents=[common, mono, templates], help="build export templates")
    p.set_defaults(func=cmd_templates)
    p = sub.add_parser("all", parents=[common, mono, editor, templates], help="doctor, editor, templates, install")
    p.set_defaults(func=cmd_all)
    p = sub.add_parser("install", parents=[common, mono], help="install staged templates (+ .tpz)")
    p.add_argument("--tpz", action="store_true", help="also package a .tpz archive")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("deps", parents=[common], help="download AccessKit / ANGLE / D3D12 SDKs")
    p.set_defaults(func=cmd_deps)
    p = sub.add_parser("toolchain", parents=[common],
                       help="install cross-compilers: llvm-mingw (Windows), osxcross + MoltenVK (macOS)")
    p.add_argument("action", nargs="?", choices=["status", "install", "remove"], default="status")
    p.add_argument("name", nargs="?", choices=["mingw", "osxcross", "moltenvk", "all"], default="all")
    p.add_argument("--sdk", help="osxcross: Xcode .xip or MacOSX<version>.sdk.tar.xz to build against")
    p.set_defaults(func=cmd_toolchain)

    p = sub.add_parser("launch", parents=[common], help="open Godot, then check for updates in the background")
    p.add_argument("godot_args", nargs=argparse.REMAINDER, help="arguments for Godot (after --)")
    p = sub.add_parser("snapshot", parents=[common], help="list, create, use or delete editor snapshots")
    p.add_argument("action", nargs="?", choices=["list", "create", "use", "delete"], default="list")
    p.add_argument("id", nargs="?", help="snapshot id or commit (use: also 'latest')")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("history", parents=[common], help="build durations and cache statistics")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_history)
    p = sub.add_parser("storage", parents=[common], help="disk usage of caches, objects, snapshots, templates")
    p.add_argument("action", nargs="?", choices=["list", "clear"], default="list")
    p.add_argument("item", nargs="?")
    p.set_defaults(func=cmd_storage)

    p = sub.add_parser("key", parents=[common], help="manage the PCK encryption key")
    p.add_argument("action", choices=["generate", "show", "path", "env", "verify", "backup", "restore",
                                      "check-backup"])
    p.add_argument("path", nargs="?", help="backup file (backup / restore / check-backup)")
    p.add_argument("--passphrase-stdin", action="store_true", help="read the backup passphrase from stdin")
    p.add_argument("--force", action="store_true", help="replace an existing key (generate)")
    p.add_argument("--reveal", action="store_true", help="print the full key (show)")
    p.add_argument("--shell", choices=["bash", "zsh", "fish"], help="syntax for `key env`")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("clean", parents=[common], help="remove staged output, logs or objects")
    p.add_argument("--dist", action="store_true", help="staged templates and packages")
    p.add_argument("--logs", action="store_true", help="build logs")
    p.add_argument("--objects", action="store_true", help="bin/obj (forces full rebuilds)")
    p.add_argument("--everything", action="store_true", help="all of the above")
    p.set_defaults(func=cmd_clean)
    return parser


def dispatch(parser: argparse.ArgumentParser, argv: list[str], show_banner: bool = True) -> int:
    global JSON_MODE
    args = parser.parse_args(argv)
    if args.json:
        JSON_MODE = True
        Style.enabled = False
    if args.no_color:
        Style.enabled = False
    if args.command == "launch":
        return cmd_launch(args)
    if args.command in ("setup", "clone", "toolchain"):
        try:
            code = args.func(None, args)
        except BuildError as exc:
            fail(str(exc))
            code = 1
        emit("exit", code=code)
        return code
    try:
        ctx = Context(
            source=args.source.expanduser().resolve() if str(args.source) not in ("", ".") else args.source,
            jobs=args.jobs,
            verbose=args.verbose,
            dry_run=args.dry_run,
            assume_yes=args.yes or args.json,
            extra_scons_args=tuple(getattr(args, "scons_args", ()) or ()),
        )
    except SetupRequired as exc:
        emit("setup_required", message=str(exc), source=str(exc.source) if exc.source else None,
             config=str(MANAGER_FILE), settings=read_manager())
        fail(str(exc))
        emit("exit", code=3)
        return 3
    if show_banner and args.command not in ("status", "config", "history", "storage", "snapshot") and not (
            args.command == "key" and args.action in ("show", "path", "env")):
        banner(ctx)
    if not args.command:
        if sys.stdin.isatty() and not JSON_MODE:
            return interactive(parser, argv)  # only global flags here: no command was given
        parser.print_help()
        return 2
    try:
        code = args.func(ctx, args)
    except BuildError as exc:
        fail(str(exc))
        code = 1
    emit("exit", code=code)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        return dispatch(parser, list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        print()
        warn("Cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
