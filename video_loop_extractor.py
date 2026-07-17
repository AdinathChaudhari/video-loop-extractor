#!/usr/bin/env python3
"""video-loop-extractor — detect and extract a seamless video loop from any
yt-dlp-supported URL.

Given a video URL, this tool:
  1. Probes metadata (title/duration/fps/formats) with yt-dlp.
  2. Downloads a full low-resolution copy for whole-timeline analysis.
  3. Detects the repeating loop period via 1 fps perceptual-hash autocorrelation.
  4. Refines the period to an exact frame count at the source's HQ fps.
  5. Downloads only the HQ segment covering that loop (stream copy, no re-encode).
  6. Aligns the segment to the source timeline via fingerprint matching.
  7. Performs exactly ONE encode into a Mac-friendly seamless-loop file.
  8. Verifies the seam and reports a confidence verdict.

See CLAUDE.md for architecture notes and hard invariants. Testing hooks:
--local-file, --detect-only, --json, --keep-temp, --work-dir.

Reference ground truth: https://youtu.be/LpC7_HQ4Jmg -> 720 frames = 12.000s @ 60fps, 4K.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import dataclasses
import enum
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

VERSION = "1.1.0"
PROG_NAME = "video-loop-extractor"

# --------------------------------------------------------------------------
# Deferred, guarded imports (numpy required, rich optional) — §6.3
# --------------------------------------------------------------------------

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:  # pragma: no cover - exercised only without numpy installed
    HAVE_NUMPY = False
    np = None  # type: ignore

HAVE_RICH = False
RichConsole = None
RichLive = None
RichProgress = None
RichTable = None
RichPanel = None
RichGroup = None
RichText = None
RichSpinner = None
SpinnerColumn = None
TextColumn = None
BarColumn = None
DownloadColumn = None
TransferSpeedColumn = None
TimeRemainingColumn = None
TaskProgressColumn = None


def _import_rich():
    """Populate the Rich globals. Raises ImportError if rich is unavailable."""
    global RichConsole, RichLive, RichProgress, RichTable, RichPanel, RichGroup
    global RichText, RichSpinner
    global SpinnerColumn, TextColumn, BarColumn, DownloadColumn
    global TransferSpeedColumn, TimeRemainingColumn, TaskProgressColumn

    from rich.console import Console as _Console
    from rich.console import Group as _Group
    from rich.live import Live as _Live
    from rich.panel import Panel as _Panel
    from rich.progress import BarColumn as _BarColumn
    from rich.progress import DownloadColumn as _DownloadColumn
    from rich.progress import Progress as _Progress
    from rich.progress import SpinnerColumn as _SpinnerColumn
    from rich.progress import TaskProgressColumn as _TaskProgressColumn
    from rich.progress import TextColumn as _TextColumn
    from rich.progress import TimeRemainingColumn as _TimeRemainingColumn
    from rich.progress import TransferSpeedColumn as _TransferSpeedColumn
    from rich.spinner import Spinner as _Spinner
    from rich.table import Table as _Table
    from rich.text import Text as _Text

    RichConsole = _Console
    RichLive = _Live
    RichProgress = _Progress
    RichTable = _Table
    RichPanel = _Panel
    RichGroup = _Group
    RichText = _Text
    RichSpinner = _Spinner
    SpinnerColumn = _SpinnerColumn
    TextColumn = _TextColumn
    BarColumn = _BarColumn
    DownloadColumn = _DownloadColumn
    TransferSpeedColumn = _TransferSpeedColumn
    TimeRemainingColumn = _TimeRemainingColumn
    TaskProgressColumn = _TaskProgressColumn


try:
    _import_rich()
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


def _consent_to_install(missing):
    """Ask before mutating the user's environment. Non-interactive -> no."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    try:
        resp = input(
            f"{', '.join(missing)} not installed. Install now with pip? [y/N]: "
        ).strip().lower()
    except EOFError:
        resp = ""
    return resp in ("y", "yes")


def ensure_deps(assume_yes=False):
    """PEP-668-safe dependency auto-install. Called before deferred numpy/rich use. §6.3

    Auto-install mutates the active Python environment, so it is gated on consent:
    a TTY prompt when interactive, or --yes. Non-interactive without --yes refuses
    and prints the venv instructions rather than silently installing.
    """
    global np, HAVE_NUMPY, HAVE_RICH
    venv_hint = (
        "If your Python is externally managed (brew/PEP 668), run from a venv, e.g.:\n"
        "  python3 -m venv ~/.venvs/main && "
        "~/.venvs/main/bin/pip install -r requirements.txt"
    )
    missing = [m for m in ("numpy", "rich") if importlib.util.find_spec(m) is None]
    if missing:
        consent = assume_yes or _consent_to_install(missing)
        if not consent:
            if "numpy" in missing:
                sys.exit("numpy is required. " + venv_hint)
            missing = []  # only rich left and user declined -> plain-UI fallback
    if missing:
        print(f"Installing {', '.join(missing)}...", file=sys.stderr)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
            importlib.invalidate_caches()
        except subprocess.CalledProcessError:
            if "numpy" in missing:
                sys.exit(
                    "Could not install numpy. If your Python is externally managed "
                    "(brew/PEP 668), run from a venv, e.g.:\n"
                    "  python3 -m venv ~/.venvs/main && "
                    "~/.venvs/main/bin/pip install -r requirements.txt"
                )
            # rich failing -> plain UI fallback, non-fatal
    if not HAVE_NUMPY:
        try:
            import numpy as _np
            np = _np
            HAVE_NUMPY = True
        except ImportError:
            sys.exit(
                "Could not install numpy. If your Python is externally managed "
                "(brew/PEP 668), run from a venv, e.g.:\n"
                "  python3 -m venv ~/.venvs/main && "
                "~/.venvs/main/bin/pip install -r requirements.txt"
            )
    if not HAVE_RICH:
        try:
            _import_rich()
            HAVE_RICH = True
        except ImportError:
            pass


# --------------------------------------------------------------------------
# §0 hard invariants are enforced structurally throughout this file:
#   - exactly one ffmpeg re-encode (encode_loop) — HQ download and --loops
#     concat are both stream copy.
#   - coarse detection always analyzes the full lowres timeline.
#   - no personal absolute paths — all paths derive from Path.home()/tempfile/
#     os.environ/CLI flags.
#   - loop length is defined in frames (N); period_s = N / fps is derived.
#   - absolute time lives only in the lowres file; HQ segments are aligned by
#     fingerprint match (align_segment / §3.7b), never trusted from
#     --download-sections metadata.
# --------------------------------------------------------------------------


class StageState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    WARN = "warn"


STAGES = [
    ("probe", "Probe video metadata"),
    ("lowres", "Download low-res analysis copy"),
    ("detect", "Detect loop period (autocorrelation)"),
    ("refine", "Frame-exact refinement"),
    ("hq", "Download max-quality segment"),
    ("align", "Align segment to source timeline"),
    ("encode", "Encode seamless loop"),
    ("verify", "Verify loop seam"),
    ("done", "Finalize"),
]


class StageError(Exception):
    """Raised by any pipeline stage to abort with a specific exit code."""

    def __init__(self, stage, message, hint=None, log_path=None, code=4):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.hint = hint
        self.log_path = log_path
        self.code = code


@dataclasses.dataclass
class EnvInfo:
    ffmpeg: str
    ffprobe: str
    ytdlp: list
    remote_components_supported: bool


@dataclasses.dataclass
class CoarseResult:
    period_lag: object
    period_s: float
    confidence: float
    verdict: str  # HIGH|MEDIUM|LOW|NONE|STATIC|USER
    candidates: list = dataclasses.field(default_factory=list)
    loop_start_s: float = 0.0
    curve: object = None
    few_periods: bool = False
    provisional_only: bool = False
    borderline: object = None


@dataclasses.dataclass
class LoopResult:
    frames: int
    fps: float
    period_s: float
    start_s: float
    confidence: float = 0.0
    verdict: str = "USER"
    plateau_width: int = 0
    rescaled: bool = False


@dataclasses.dataclass
class SeamResult:
    wrap_similarity: float
    adjacent_p5: float
    z: float
    seamless: bool


@dataclasses.dataclass
class Ctx:
    args: object
    workdir: Path
    ytdlp: list
    ffmpeg: str = ""
    ffprobe: str = ""
    ui: object = None
    info: dict = None
    chosen_fmt: dict = None
    coarse: CoarseResult = None
    loop: LoopResult = None
    hq_offset: float = 0.0
    paths: dict = dataclasses.field(default_factory=dict)
    raw_argv: list = dataclasses.field(default_factory=list)
    remote_components_supported: bool = False
    keep_workdir_due_to_failure: bool = False
    _encode_retry_done: bool = False


# --------------------------------------------------------------------------
# Child process registry (for SIGINT/SIGTERM cleanup) and run_cmd choke point
# --------------------------------------------------------------------------

_CHILD_PROCS = []
_CHILD_LOCK = threading.Lock()
_CTX_FOR_SIGNAL = None
_CLEANED_UP = False


def register_child(proc):
    with _CHILD_LOCK:
        _CHILD_PROCS.append(proc)


def unregister_child(proc):
    with _CHILD_LOCK:
        if proc in _CHILD_PROCS:
            _CHILD_PROCS.remove(proc)


def _kill_all_children():
    with _CHILD_LOCK:
        procs = list(_CHILD_PROCS)
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + 3.0
    for p in procs:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                p.wait(timeout=max(0.0, remaining))
            except Exception:
                pass
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def _num(s):
    """Parse a numeric string that may literally be 'NA' or 'None'. §2.3.3"""
    if s is None:
        return None
    s = s.strip()
    if s in ("NA", "None", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _strip_flag_with_value(argv, flag):
    out = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == flag:
            skip = True
            continue
        out.append(a)
    return out


def _read_exact(stream, n):
    """Read exactly n bytes from a binary stream, or return None at EOF/partial-frame."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _parse_fraction(s):
    if not s:
        return 30.0
    s = str(s)
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            num_f = float(num)
            den_f = float(den)
            return num_f / den_f if den_f else num_f
        except ValueError:
            return 30.0
    try:
        return float(s)
    except ValueError:
        return 30.0


def parse_time_to_seconds(s):
    """Parse SS, MM:SS, or HH:MM:SS (fractional seconds allowed) into seconds."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time value: {s!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid time value: {s!r}")
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60.0 + n
    return seconds


def run_cmd(ctx, argv, *, stage, progress_parser=None, timeout=None, input_text=None):
    """The single choke point for all text-protocol child processes (yt-dlp, ffprobe,
    ffmpeg -progress). Logs argv on --verbose, tees stderr to workdir/logs/{stage}.log,
    drains stdout via progress_parser, registers the Popen for the SIGINT handler, and
    raises StageError on nonzero exit. Raw-video pipe reads use _run_rawvideo_pipe instead."""
    if ctx.args.verbose:
        sys.stderr.write("+ " + " ".join(shlex.quote(a) for a in argv) + "\n")
    log_dir = ctx.workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{stage}.log"

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    register_child(proc)
    stdout_lines = []
    stderr_lines = []

    def _drain_stdout():
        for line in iter(proc.stdout.readline, ""):
            stdout_lines.append(line)
            if progress_parser is not None:
                try:
                    progress_parser(line)
                except Exception:
                    pass

    def _drain_stderr():
        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
            for line in iter(proc.stderr.readline, ""):
                stderr_lines.append(line)
                lf.write(line)
                lf.flush()

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except Exception:
            pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        unregister_child(proc)
        raise StageError(stage, f"Command timed out after {timeout}s: {' '.join(argv[:2])}", log_path=str(log_path))

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    unregister_child(proc)

    completed = subprocess.CompletedProcess(argv, proc.returncode, "".join(stdout_lines), "".join(stderr_lines))
    if proc.returncode != 0:
        tail = "".join(stderr_lines[-25:]) or "".join(stdout_lines[-25:])
        raise StageError(
            stage,
            f"Command failed (exit {proc.returncode}): {' '.join(argv[:2])}",
            hint=tail.strip()[-1500:],
            log_path=str(log_path),
        )
    return completed


def _run_rawvideo_pipe(ctx, argv, frame_bytes, stage, ui_task_key=None):
    """Launch argv (an ffmpeg rawvideo-pipe command) and read stdout incrementally in
    exact frame-sized chunks so the UI can advance per decoded frame even on multi-hour
    sources. §3.4"""
    if ctx.args.verbose:
        sys.stderr.write("+ " + " ".join(shlex.quote(a) for a in argv) + "\n")
    log_dir = ctx.workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{stage}.log"

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    register_child(proc)
    stderr_buf = bytearray()

    def _drain_stderr():
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            stderr_buf.extend(chunk)

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    frames = []
    try:
        while True:
            chunk = _read_exact(proc.stdout, frame_bytes)
            if chunk is None:
                break
            frames.append(chunk)
            if ui_task_key is not None:
                ctx.ui.advance_task(ui_task_key, advance=1)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass

    proc.wait()
    t.join(timeout=5)
    unregister_child(proc)
    with open(log_path, "ab") as lf:
        lf.write(bytes(stderr_buf))

    if not frames:
        raise StageError(
            stage,
            "ffmpeg produced no decodable frames.",
            hint=bytes(stderr_buf).decode(errors="replace")[-1500:],
            log_path=str(log_path),
        )
    return frames


# --------------------------------------------------------------------------
# §2 Progress UI — rich single-Live composition with a deterministic plain
# fallback. Rich is used only when HAVE_RICH and stderr is a real TTY and
# --quiet was not given (never trust rich's own TTY autodetection). §2.1
# --------------------------------------------------------------------------


class PlainTask:
    """A single \\r-rewritten (TTY) or 10%-stepped (non-TTY) progress line."""

    def __init__(self, description, total=None):
        self.description = description
        self.total = total
        self.completed = 0.0
        self._last_pct_printed = -1
        self._last_time = 0.0
        self._is_tty = sys.stderr.isatty()

    def update(self, advance=0, completed=None, total=None):
        if total is not None:
            self.total = total
        if completed is not None:
            self.completed = completed
        else:
            self.completed += advance
        self._maybe_print()

    def _pct(self):
        if not self.total:
            return None
        try:
            return max(0, min(100, int(100 * self.completed / self.total)))
        except (TypeError, ZeroDivisionError):
            return None

    def _maybe_print(self):
        now = time.monotonic()
        pct = self._pct()
        if self._is_tty:
            if now - self._last_time < 0.5 and pct != 100:
                return
            self._last_time = now
            if pct is not None:
                sys.stderr.write(f"\r{self.description}: {pct}%   ")
            else:
                sys.stderr.write(f"\r{self.description}: {int(self.completed)}   ")
            sys.stderr.flush()
        else:
            if pct is not None and pct != self._last_pct_printed and pct % 10 == 0:
                self._last_pct_printed = pct
                sys.stderr.write(f"{self.description}: {pct}%\n")
                sys.stderr.flush()

    def finish(self):
        if self._is_tty:
            sys.stderr.write(f"\r{self.description}: done   \n")
            sys.stderr.flush()


class UI:
    """Owns the single rich Live (or the plain-text fallback) for the whole run."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.stage_order = [s[0] for s in STAGES]
        self.stage_labels = dict(STAGES)
        self.state = {s[0]: StageState.PENDING for s in STAGES}
        self.detail = {s[0]: "" for s in STAGES}
        self.header_info = {}
        self.quiet = args.quiet
        # Never trust rich's own TTY autodetection for the mode decision (§2.1).
        self.use_rich = HAVE_RICH and sys.stderr.isatty() and not args.quiet
        self.live = None
        self.console = None
        self.dl_progress = None
        self.analysis_progress = None
        self.tasks = {}
        if self.use_rich:
            self.console = RichConsole(stderr=True)
            self.dl_progress = RichProgress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )
            self.analysis_progress = RichProgress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )

    # -- lifecycle -----------------------------------------------------
    def start(self):
        if self.use_rich:
            self.live = RichLive(get_renderable=self.render, console=self.console, refresh_per_second=8)
            self.live.start()

    def stop(self):
        if self.use_rich and self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass
            self.live = None

    @contextlib.contextmanager
    def _paused(self):
        was_running = self.use_rich and self.live is not None
        if was_running:
            try:
                self.live.stop()
            except Exception:
                pass
        try:
            yield
        finally:
            if was_running:
                try:
                    self.live.start()
                except Exception:
                    pass

    # -- header ----------------------------------------------------------
    def set_header(self, title, duration, width, height, fps):
        with self.lock:
            self.header_info = {"title": title, "duration": duration, "width": width, "height": height, "fps": fps}

    def _header_text(self):
        info = self.header_info
        if not info:
            return "Probing..."
        dur = _format_duration(info.get("duration") or 0)
        fps = info.get("fps") or 0
        return f"{info.get('title', '')}\n{info.get('width')}x{info.get('height')}@{fps:g}  ({dur})"

    # -- stage board -------------------------------------------------------
    def set_stage(self, key, state, detail=None):
        with self.lock:
            self.state[key] = state
            if detail is not None:
                self.detail[key] = detail
        if not self.use_rich and not self.quiet:
            self._plain_stage_line(key, state)

    def note_detail(self, key, text):
        with self.lock:
            cur = self.detail.get(key, "")
            self.detail[key] = (cur + "; " + text) if cur else text

    def _plain_stage_line(self, key, state):
        idx = self.stage_order.index(key) + 1
        total = len(self.stage_order)
        label = self.stage_labels[key]
        detail = self.detail.get(key, "")
        if state == StageState.RUNNING:
            sys.stderr.write(f"[{idx}/{total}] {label} ...")
            sys.stderr.flush()
        elif state == StageState.DONE:
            suffix = f" ({detail})" if detail else ""
            sys.stderr.write(f" done{suffix}\n")
            sys.stderr.flush()
        elif state == StageState.WARN:
            suffix = f" ({detail})" if detail else ""
            sys.stderr.write(f" warning{suffix}\n")
            sys.stderr.flush()
        elif state == StageState.FAILED:
            suffix = f" ({detail})" if detail else ""
            sys.stderr.write(f" FAILED{suffix}\n")
            sys.stderr.flush()
        elif state == StageState.SKIPPED:
            sys.stderr.write(f"[{idx}/{total}] {label} ... skipped\n")
            sys.stderr.flush()

    def _glyph_for(self, state):
        if state == StageState.PENDING:
            return RichText("○", style="dim")
        if state == StageState.RUNNING:
            return RichSpinner("dots")
        if state == StageState.DONE:
            return RichText("✔", style="green")
        if state == StageState.FAILED:
            return RichText("✖", style="red")
        if state == StageState.SKIPPED:
            return RichText("◌", style="dim")
        if state == StageState.WARN:
            return RichText("⚠", style="yellow")
        return RichText(" ")

    def render(self):
        header = RichPanel(self._header_text(), title=PROG_NAME)
        table = RichTable(show_header=False, box=None, padding=(0, 1))
        table.add_column()
        table.add_column()
        with self.lock:
            for key in self.stage_order:
                st = self.state[key]
                label = self.stage_labels[key]
                detail = self.detail.get(key, "")
                text = f"[dim]{detail}[/dim]" if detail else ""
                table.add_row(self._glyph_for(st), f"{label}  {text}" if text else label)
        items = [header, table]
        if self.dl_progress is not None and self.dl_progress.task_ids:
            items.append(self.dl_progress)
        if self.analysis_progress is not None and self.analysis_progress.task_ids:
            items.append(self.analysis_progress)
        return RichGroup(*items)

    # -- tasks ---------------------------------------------------------
    def start_task(self, key, description, total=None, kind="download"):
        if self.use_rich:
            prog = self.dl_progress if kind == "download" else self.analysis_progress
            task_id = prog.add_task(description, total=total)
            self.tasks[key] = ("rich", prog, task_id)
        else:
            self.tasks[key] = ("plain", PlainTask(description, total))
            if not self.quiet:
                sys.stderr.write(f"  {description} ...\n")
        return key

    def advance_task(self, key, advance=1, completed=None, total=None):
        entry = self.tasks.get(key)
        if entry is None:
            return
        if entry[0] == "rich":
            _, prog, task_id = entry
            kwargs = {}
            if completed is not None:
                kwargs["completed"] = completed
            if total is not None:
                kwargs["total"] = total
            if kwargs:
                prog.update(task_id, **kwargs)
            else:
                prog.update(task_id, advance=advance)
        else:
            _, task = entry
            task.update(advance=(advance if completed is None else 0), completed=completed, total=total)

    def finish_task(self, key):
        entry = self.tasks.pop(key, None)
        if entry is None:
            return
        if entry[0] == "rich":
            _, prog, task_id = entry
            try:
                prog.remove_task(task_id)
            except Exception:
                pass
        else:
            entry[1].finish()

    # -- prompts (must pause Live, §2.3.5) ------------------------------
    def prompt(self, text, default=None):
        # Write the prompt to stderr (never stdout) so a mid-run prompt under
        # --json cannot interleave with the single stdout JSON object.
        with self._paused():
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
                resp = input("")
            except EOFError:
                resp = ""
        resp = resp.strip()
        return resp if resp else default

    def confirm(self, text, default=False):
        suffix = " [Y/n]: " if default else " [y/N]: "
        with self._paused():
            try:
                sys.stderr.write(text + suffix)
                sys.stderr.flush()
                resp = input("").strip().lower()
            except EOFError:
                resp = ""
        if not resp:
            return default
        return resp in ("y", "yes")

    def pick_candidate(self, candidates):
        """candidates: list of dict{L, period_s, Z, supp}. Returns chosen dict."""
        with self._paused():
            sys.stderr.write("Multiple plausible loop periods detected:\n")
            for i, d in enumerate(candidates, 1):
                sys.stderr.write(f"  [{i}] {d['period_s']:.2f}s (Z={d['Z']:.1f}, support={d['supp']:.2f})\n")
            sys.stderr.write(f"Pick [1-{len(candidates)}] (default 1): ")
            sys.stderr.flush()
            try:
                resp = input("").strip()
            except EOFError:
                resp = ""
        try:
            idx = int(resp) - 1
        except ValueError:
            idx = 0
        idx = max(0, min(len(candidates) - 1, idx))
        return candidates[idx]

    def print_summary(self, lines):
        if self.use_rich:
            for line in lines:
                self.console.print(line)
        else:
            for line in lines:
                sys.stderr.write(line + "\n")
            sys.stderr.flush()


def _format_duration(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# --------------------------------------------------------------------------
# §3.1 check_environment
# --------------------------------------------------------------------------


def _confirm_install(args, name):
    if args.yes:
        return True
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    try:
        resp = input(f"{name} not found. Install it now with pip? [y/N]: ").strip().lower()
    except EOFError:
        resp = ""
    return resp in ("y", "yes")


def check_environment(args) -> EnvInfo:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        sys.stderr.write("ffmpeg not found. Install with: brew install ffmpeg\n")
        sys.exit(3)

    ytdlp_bin = shutil.which("yt-dlp")
    if ytdlp_bin:
        ytdlp = [ytdlp_bin]
    elif importlib.util.find_spec("yt_dlp") is not None:
        ytdlp = [sys.executable, "-m", "yt_dlp"]
    else:
        if _confirm_install(args, "yt-dlp"):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])
                importlib.invalidate_caches()
                ytdlp = [sys.executable, "-m", "yt_dlp"]
            except subprocess.CalledProcessError:
                sys.stderr.write("Could not install yt-dlp. Install manually: pip install yt-dlp or brew install yt-dlp\n")
                sys.exit(3)
        else:
            sys.stderr.write("yt-dlp not found. Install with: brew install yt-dlp\n")
            sys.exit(3)

    ensure_deps(assume_yes=args.yes)

    remote_ok = False
    try:
        help_out = subprocess.run(ytdlp + ["--help"], capture_output=True, text=True, timeout=30)
        remote_ok = "--remote-components" in (help_out.stdout or "")
    except Exception:
        remote_ok = False

    return EnvInfo(ffmpeg=ffmpeg, ffprobe=ffprobe, ytdlp=ytdlp, remote_components_supported=remote_ok)


# --------------------------------------------------------------------------
# §1.4 output naming / §1.2.7 output path resolution
# --------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r'[\\/:*?"<>|]')
# Titles are fully attacker-controlled remote input: strip path separators,
# Windows-reserved chars, and C0/C1 control chars (ANSI escapes can spoof the
# terminal when echoed and produce garbled filenames).
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_title(title):
    cleaned = _CTRL_RE.sub("", title or "")
    cleaned = _SANITIZE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.lstrip(".-").strip()  # no hidden dotfiles / dash-led args
    cleaned = cleaned[:120]
    return cleaned if cleaned else "video"


def _default_output_dir():
    for cand in (Path.home() / "Movies", Path.home() / "Videos"):
        if cand.exists():
            return cand
    return Path.home()


def _extension_for_codec(codec):
    return ".mov" if codec == "hevc" else ".mp4"


def _build_output_filename(ctx, period_s=None, height=None):
    title = _sanitize_title(ctx.info["title"] if ctx.info else "video")
    ext = _extension_for_codec(ctx.args.codec)
    if period_s is None:
        period_s = 0.0
    if height is None:
        height = (ctx.info or {}).get("height") or 0
    return f"{title} [loop {period_s:.3f}s {height}p]{ext}"


def _unique_path(path):
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    n = 2
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def _display_home(p):
    try:
        rel = Path(p).expanduser().resolve().relative_to(Path.home().resolve())
        return "~" if str(rel) == "." else f"~/{rel}"
    except ValueError:
        return str(p)


def _is_interactive(ctx):
    return (not ctx.args.yes) and sys.stdin.isatty() and sys.stderr.isatty()


def _flag_explicit(ctx, dest):
    """Best-effort check of whether `dest` was supplied explicitly on the CLI
    (argparse doesn't track this itself)."""
    flag_map = {
        "codec": ["--codec"],
        "audio": ["--audio"],
        "loops": ["--loops"],
        "max_height": ["--max-height"],
        "output": ["-o", "--output"],
        "start": ["--start"],
    }
    flags = flag_map.get(dest, [f"--{dest.replace('_', '-')}"])
    for a in ctx.raw_argv:
        key = a.split("=", 1)[0]
        if key in flags:
            return True
    return False


def _resolve_output_path(ctx):
    args = ctx.args
    interactive = _is_interactive(ctx)
    out_arg = args.output

    if out_arg:
        out_path = Path(out_arg).expanduser()
        looks_like_dir = out_path.is_dir() or (out_path.suffix == "" and not out_path.exists())
        if looks_like_dir:
            out_path.mkdir(parents=True, exist_ok=True)
            ctx.paths["out_dir"] = out_path
            ctx.paths["out_explicit_file"] = None
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.paths["out_dir"] = out_path.parent
            ctx.paths["out_explicit_file"] = out_path
    else:
        if interactive:
            default_dir = _default_output_dir()
            resp = ctx.ui.prompt(f"Save to [{_display_home(default_dir)}]: ", default=str(default_dir))
            out_dir = Path(resp).expanduser() if resp else default_dir
        else:
            out_dir = _default_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx.paths["out_dir"] = out_dir
        ctx.paths["out_explicit_file"] = None

    placeholder_name = _build_output_filename(ctx)
    placeholder_path = ctx.paths["out_explicit_file"] or (ctx.paths["out_dir"] / placeholder_name)
    if placeholder_path.exists():
        if interactive:
            resp = ctx.ui.prompt(
                f"{placeholder_path.name} already exists. Overwrite? [y/N] (anything else auto-renames): ",
                default="n",
            )
            if not (resp and resp.lower().startswith("y")):
                placeholder_path = _unique_path(placeholder_path)
        else:
            placeholder_path = _unique_path(placeholder_path)
    ctx.paths["out_placeholder"] = placeholder_path


# --------------------------------------------------------------------------
# §5 Format selection logic
# --------------------------------------------------------------------------


def select_hq_format(formats, max_height=0, prefer_codec_order=("av01", "vp9", "vp09", "hevc", "h265", "avc1", "h264")):
    def codec_rank(fmt):
        vcodec = (fmt.get("vcodec") or "").lower()
        for i, prefix in enumerate(prefer_codec_order):
            if vcodec.startswith(prefix):
                return len(prefer_codec_order) - i
        return 0

    candidates = [
        f for f in (formats or [])
        if f.get("vcodec") not in (None, "none") and f.get("ext") != "mhtml" and f.get("height")
    ]
    if not candidates:
        return None
    if max_height:
        capped = [f for f in candidates if f["height"] <= max_height]
        if capped:
            candidates = capped

    def sort_key(f):
        return (f["height"], f.get("fps") or 0, codec_rank(f), f.get("tbr") or 0)

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def _fallback_selector_string(max_height=0):
    if max_height:
        return f"bv*[height<={max_height}]/bv*/b[height<={max_height}]/b"
    return "bv*/b"


def _estimate_lowres_size(formats, duration):
    candidates = [f for f in (formats or []) if f.get("vcodec") not in (None, "none") and f.get("ext") != "mhtml"]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f.get("height") or 0))
    worst = candidates[0]
    size = worst.get("filesize") or worst.get("filesize_approx")
    if size:
        return size
    tbr = worst.get("tbr")
    if tbr and duration:
        return tbr * 1000 / 8 * duration
    return None


# --------------------------------------------------------------------------
# §3.2 probe
# --------------------------------------------------------------------------


def _ytdlp_base_argv(ctx):
    argv = list(ctx.ytdlp)
    if ctx.remote_components_supported:
        argv += ["--remote-components", "ejs:github"]
    if ctx.args.cookies_from_browser:
        argv += ["--cookies-from-browser", ctx.args.cookies_from_browser]
    return argv


def _probe_local_file(ctx) -> dict:
    path = Path(ctx.args.local_file).expanduser()
    if not path.exists():
        raise StageError("probe", f"--local-file not found: {path}", code=2)
    argv = [ctx.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    cp = run_cmd(ctx, argv, stage="probe", timeout=60)
    data = json.loads(cp.stdout)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    fps = _parse_fraction(vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "30/1")
    duration = float(data.get("format", {}).get("duration") or vstream.get("duration") or 0)
    min_period = ctx.args.min_period
    ctx.info = {
        "title": path.stem,
        "duration": duration,
        "fps": fps,
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "formats": [],
        "webpage_url": str(path),
        "duration_recovered": False,
    }
    if duration and duration < 2 * min_period:
        raise StageError("probe", "Video too short to contain >=2 loop periods.", code=5)
    ctx.chosen_fmt = None
    ctx.paths["lowres"] = path
    ctx.ui.set_header(ctx.info["title"], duration, ctx.info["width"], ctx.info["height"], fps)
    _resolve_output_path(ctx)
    return ctx.info


def probe(ctx) -> dict:
    args = ctx.args
    if args.local_file:
        return _probe_local_file(ctx)

    url = args.url
    argv = _ytdlp_base_argv(ctx) + ["-j", "--no-playlist", "--", url]
    cp = run_cmd(ctx, argv, stage="probe", timeout=120)
    try:
        last_line = [l for l in cp.stdout.splitlines() if l.strip()][-1]
        info = json.loads(last_line)
    except Exception as e:
        raise StageError("probe", f"Could not parse yt-dlp metadata: {e}", code=4)

    if info.get("is_live"):
        raise StageError("probe", "Live streams have no fixed timeline to loop.", code=4)

    duration = info.get("duration") or 0
    min_period = args.min_period
    ctx.info = {
        "title": info.get("title") or "video",
        "duration": duration,
        "fps": info.get("fps") or 30.0,
        "width": info.get("width") or 0,
        "height": info.get("height") or 0,
        "formats": info.get("formats") or [],
        "webpage_url": info.get("webpage_url") or url,
        "duration_recovered": not bool(duration),
        "is_vfr": False,
    }
    if duration and duration < 2 * min_period:
        raise StageError("probe", "Video too short to contain >=2 loop periods.", code=5)

    ctx.chosen_fmt = select_hq_format(ctx.info["formats"], max_height=args.max_height)
    if ctx.chosen_fmt:
        ctx.info["fps"] = ctx.chosen_fmt.get("fps") or ctx.info["fps"]
        ctx.info["width"] = ctx.chosen_fmt.get("width") or ctx.info["width"]
        ctx.info["height"] = ctx.chosen_fmt.get("height") or ctx.info["height"]

    ctx.ui.set_header(ctx.info["title"], ctx.info["duration"], ctx.info["width"], ctx.info["height"], ctx.info["fps"])
    _resolve_output_path(ctx)
    return ctx.info


# --------------------------------------------------------------------------
# §3.3 download_lowres
# --------------------------------------------------------------------------


def _delete_partials(workdir, prefix):
    for pattern in (f"{prefix}.*", f"{prefix}.*.part"):
        for p in workdir.glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


def _make_ytdlp_progress_parser(ctx, task_key):
    def parser(line):
        if "VLEPROG|" not in line:
            return
        _, _, rest = line.partition("VLEPROG|")
        parts = rest.strip().split("|")
        if len(parts) < 4:
            return
        downloaded_s, total_s, total_est_s, _speed_s = parts[:4]
        downloaded = _num(downloaded_s)
        total = _num(total_s)
        if total is None:
            total = _num(total_est_s)
        if downloaded is None:
            return
        ctx.ui.advance_task(task_key, completed=downloaded, total=total)

    return parser


_PROGRESS_TEMPLATE = (
    "download:VLEPROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s"
)


def download_lowres(ctx) -> Path:
    args = ctx.args
    formats = ctx.info.get("formats") or []
    est = _estimate_lowres_size(formats, ctx.info["duration"])
    if est and est > 500 * 1024 * 1024:
        msg = f"worst available stream is ~{est / 1024 / 1024:.0f} MB; analysis will download it fully"
        if _is_interactive(ctx):
            if not ctx.ui.confirm(msg + ". Continue?", default=True):
                raise StageError("lowres", "Aborted by user (oversized lowres stream).", code=2)
        else:
            sys.stderr.write(msg + "\n")

    selector = "wv*[height>=100][ext!=mhtml]/wv*[ext!=mhtml]/w"
    out_tmpl = str(ctx.workdir / "lowres.%(ext)s")
    argv = _ytdlp_base_argv(ctx) + [
        "-f", selector,
        "--no-part", "--newline",
        "--progress-template", _PROGRESS_TEMPLATE,
        "-o", out_tmpl,
        "--", ctx.info["webpage_url"],
    ]

    task_key = ctx.ui.start_task("lowres_dl", "Download low-res analysis copy", total=None, kind="download")
    parser = _make_ytdlp_progress_parser(ctx, task_key)
    try:
        try:
            run_cmd(ctx, argv, stage="lowres", progress_parser=parser)
        except StageError:
            _delete_partials(ctx.workdir, "lowres")
            run_cmd(ctx, argv, stage="lowres", progress_parser=parser)
    finally:
        ctx.ui.finish_task(task_key)

    matches = sorted(p for p in ctx.workdir.glob("lowres.*") if p.suffix != ".part")
    if not matches:
        raise StageError("lowres", "yt-dlp did not produce a lowres file.", code=4)
    lowres_path = matches[0]
    ctx.paths["lowres"] = lowres_path

    cp = run_cmd(
        ctx,
        [ctx.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(lowres_path)],
        stage="lowres",
        timeout=60,
    )
    data = json.loads(cp.stdout)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration:
        ctx.info["duration"] = duration
    if ctx.info["duration"] and ctx.info["duration"] < 2 * ctx.args.min_period:
        raise StageError("lowres", "Video too short to contain >=2 loop periods.", code=5)

    ctx.info["lowres_fps"] = _parse_fraction(vstream.get("avg_frame_rate") or "30/1")
    ctx.info["lowres_width"] = int(vstream.get("width") or 0)
    ctx.info["lowres_height"] = int(vstream.get("height") or 0)
    return lowres_path


# --------------------------------------------------------------------------
# §3.4 extract_fingerprints — incremental rawvideo pipe read
# --------------------------------------------------------------------------


def extract_fingerprints(ctx, video_path, fps, size=(32, 18), *, expect_frames=None,
                          stage="detect", task_label="Analyzing frames", t_limit=None):
    W, H = size
    frame_bytes = W * H
    vf = f"scale={W}:{H}:flags=fast_bilinear"
    if fps is not None:
        vf = f"fps={fps}," + vf
    argv = [ctx.ffmpeg, "-v", "error", "-i", str(video_path)]
    if t_limit:
        argv += ["-t", f"{t_limit}"]
    argv += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]

    task_key = ctx.ui.start_task(f"{stage}_fp_{id(video_path)}_{fps}", task_label, total=expect_frames, kind="analysis")
    try:
        frames = _run_rawvideo_pipe(ctx, argv, frame_bytes, stage, ui_task_key=task_key)
    finally:
        ctx.ui.finish_task(task_key)

    arr = np.frombuffer(b"".join(frames), dtype=np.uint8).reshape(len(frames), frame_bytes).astype(np.float32)
    return arr


def _extract_fingerprints_window(ctx, path, fps, size, start_s, length_s, stage="align"):
    W, H = size
    frame_bytes = W * H
    vf = f"fps={fps},scale={W}:{H}:flags=fast_bilinear"
    argv = [
        ctx.ffmpeg, "-v", "error",
        "-ss", f"{max(0.0, start_s)}",
        "-i", str(path),
        "-t", f"{max(0.1, length_s)}",
        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    frames = _run_rawvideo_pipe(ctx, argv, frame_bytes, stage)
    arr = np.frombuffer(b"".join(frames), dtype=np.uint8).reshape(len(frames), frame_bytes).astype(np.float32)
    return arr


# --------------------------------------------------------------------------
# §4 Loop-detection math
# --------------------------------------------------------------------------


def normalize_fingerprints(F):
    """§4.1: x_hat = (x - mean) / (||x - mean||_2 + 1e-8). Zero-variance frames -> zero vector."""
    mean = F.mean(axis=1, keepdims=True)
    centered = F - mean
    norm = np.linalg.norm(centered, axis=1)
    zero_variance = norm < 1e-6
    safe = (norm + 1e-8)[:, None]
    Fhat = centered / safe
    Fhat[zero_variance] = 0.0
    return Fhat, zero_variance


def _sim_rows(Fhat, idx_a, idx_b):
    return np.einsum("ij,ij->i", Fhat[idx_a], Fhat[idx_b])


def autocorrelation_curve(ctx, Fhat, L_min, L_max, K=400, task_label="Autocorrelation"):
    """§4.2: R(L) = mean over up to K evenly spaced anchors of sim(f[a], f[a+L])."""
    T = Fhat.shape[0]
    Ls = np.arange(L_min, L_max + 1, dtype=np.int64)
    R = np.full(len(Ls), -1.0, dtype=np.float64)
    task_key = ctx.ui.start_task(f"autocorr_{id(Fhat)}", task_label, total=len(Ls), kind="analysis")
    try:
        for idx in range(len(Ls)):
            L = int(Ls[idx])
            max_a = T - L
            if max_a > 0:
                n_anchors = int(min(K, max_a))
                anchors = np.unique(np.linspace(0, max_a - 1, n_anchors).astype(np.int64))
                sims = _sim_rows(Fhat, anchors, anchors + L)
                R[idx] = float(sims.mean())
            ctx.ui.advance_task(task_key, advance=1)
    finally:
        ctx.ui.finish_task(task_key)
    return Ls, R


def baseline_contrast(R):
    """§4.3."""
    b = float(np.median(R))
    s = float(1.4826 * np.median(np.abs(R - b)))
    Z = (R - b) / max(s, 1e-6)
    C = (R - b) / (1 - b + 1e-6)
    return b, s, Z, C


def _local_maxima_mask(R):
    n = len(R)
    mask = np.zeros(n, dtype=bool)
    for i in range(n):
        ok = True
        if i - 1 >= 0 and R[i] < R[i - 1]:
            ok = False
        if ok and i + 1 < n and R[i] < R[i + 1]:
            ok = False
        if ok and i - 2 >= 0 and R[i] <= R[i - 2]:
            ok = False
        if ok and i + 2 < n and R[i] <= R[i + 2]:
            ok = False
        mask[i] = ok
    return mask


def find_peaks(Ls, R, Z, C):
    """§4.5: strong peaks (Z>=4, C>=0.5) and provisional peaks (Z>=4, 0.35<=C<0.5),
    both local-max filtered and merged within tolerance keeping the highest Z."""
    mask = _local_maxima_mask(R)
    strong, provisional = [], []
    for i in range(len(Ls)):
        if not mask[i]:
            continue
        Li, Zi, Ci = int(Ls[i]), float(Z[i]), float(C[i])
        if Zi >= 4.0 and Ci >= 0.5:
            strong.append((Li, Zi, Ci))
        elif Zi >= 4.0 and 0.35 <= Ci < 0.5:
            provisional.append((Li, Zi, Ci))

    def _merge(cands):
        cands = sorted(cands, key=lambda c: c[0])
        merged = []
        for c in cands:
            placed = False
            for j, m in enumerate(merged):
                tol = max(2, 0.02 * max(c[0], m[0]))
                if abs(c[0] - m[0]) <= tol:
                    if c[1] > m[1]:
                        merged[j] = c
                    placed = True
                    break
            if not placed:
                merged.append(c)
        return sorted(merged, key=lambda c: c[0])

    return _merge(strong), _merge(provisional), mask


def _all_peak_lags(Ls, Z, mask, z_thresh=2.0):
    return [int(Ls[i]) for i in range(len(Ls)) if mask[i] and Z[i] >= z_thresh]


def comb_support(L_c, all_peak_lags, L_max):
    """§4.6.1: fraction of multiples m*L_c <= L_max having a peak within tolerance."""
    m = 1
    total = 0
    hit = 0
    while m * L_c <= L_max:
        total += 1
        target = m * L_c
        tol = max(1, 0.02 * target)
        if any(abs(target - pl) <= tol for pl in all_peak_lags):
            hit += 1
        m += 1
    if total == 0:
        return 0.0, True
    return hit / total, total == 1


def _C_at(Ls, C, L):
    idx = L - int(Ls[0])
    if 0 <= idx < len(C):
        return float(C[idx])
    return None


def _Z_at(Ls, Z, L):
    idx = L - int(Ls[0])
    if 0 <= idx < len(Z):
        return float(Z[idx])
    return 0.0


def _sigmoid(x):
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _near_small_integer_ratio(a, b, tol=0.02):
    if a == 0 or b == 0:
        return True
    r = (a / b) if a > b else (b / a)
    nearest = round(r)
    if nearest < 1:
        return False
    return abs(r - nearest) / nearest <= tol


def _is_pinned(R):
    if len(R) < 3:
        return True
    if int(np.argmax(R)) == 0:
        return True
    diffs = np.diff(R)
    return bool(np.all(diffs >= -1e-9))


_VERDICT_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH"]


def _downgrade_verdict(v):
    if v not in _VERDICT_ORDER:
        return v
    i = _VERDICT_ORDER.index(v)
    return _VERDICT_ORDER[max(0, i - 1)]


def _loop_start(Fhat, L0, b, RL0, analysis_fps):
    """§4.8."""
    T = Fhat.shape[0]
    max_a = T - L0
    if max_a <= 0:
        return 0.0
    idx_a = np.arange(max_a)
    q = _sim_rows(Fhat, idx_a, idx_a + L0)
    theta = b + 0.8 * (RL0 - b)
    above = q >= theta
    run_len = 0
    run_start = 0
    best_start = None
    for i in range(len(above)):
        if above[i]:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len >= L0:
                best_start = run_start
                break
        else:
            run_len = 0
    if best_start is None:
        best_start = 0
    return best_start / analysis_fps


# --------------------------------------------------------------------------
# §3.5 detect_period
# --------------------------------------------------------------------------


def _detect_pass(ctx, F, analysis_fps, min_period, max_period_arg, pass_label="coarse"):
    Fhat, zero_var = normalize_fingerprints(F)
    T = Fhat.shape[0]
    duration = T / analysis_fps
    max_period = max_period_arg if max_period_arg else duration / 2
    L_min = max(2, math.ceil(min_period * analysis_fps))
    L_max = min(T // 2, math.floor(max_period * analysis_fps))

    if L_max <= L_min:
        return CoarseResult(period_lag=None, period_s=0.0, confidence=0.0, verdict="NONE",
                             candidates=[], loop_start_s=0.0, curve=None)

    Ls, R = autocorrelation_curve(ctx, Fhat, L_min, L_max, K=400,
                                   task_label=f"Autocorrelation ({pass_label})")
    b, s, Z, C = baseline_contrast(R)

    zero_frac = float(np.mean(zero_var)) if len(zero_var) else 0.0
    if (1 - b) < 0.005 or zero_frac > 0.30:
        return CoarseResult(period_lag=None, period_s=0.0, confidence=0.0, verdict="STATIC",
                             candidates=[], loop_start_s=0.0, curve=R)

    strong, provisional, mask = find_peaks(Ls, R, Z, C)
    all_peak_lags = _all_peak_lags(Ls, Z, mask)

    provisional_only = False
    peaks = strong
    if not peaks:
        if provisional:
            peaks = provisional
            provisional_only = True
        else:
            if _is_pinned(R):
                pass  # handled by caller via sub-lag rescue
            best_L = int(Ls[int(np.argmax(R))])
            return CoarseResult(period_lag=best_L, period_s=best_L / analysis_fps, confidence=0.0,
                                 verdict="NONE", candidates=[], loop_start_s=0.0, curve=R)

    scored = []
    for (L, Zc, Cc) in peaks:
        supp, few = comb_support(L, all_peak_lags, L_max)
        scored.append({"L": L, "Z": Zc, "C": Cc, "supp": supp, "few_periods": few})
    scored.sort(key=lambda d: d["L"])

    fund_candidates = [d for d in scored if d["supp"] >= 0.6 and d["Z"] >= 4.0]
    if not fund_candidates:
        fund_candidates = scored
    chosen = min(fund_candidates, key=lambda d: d["L"])
    few_periods = chosen["few_periods"]
    borderline_pending = None

    for d in (2, 3, 4):
        Lc = chosen["L"]
        cand_lag = round(Lc / d)
        if cand_lag < L_min:
            continue
        C_cand = _C_at(Ls, C, cand_lag)
        if C_cand is None or chosen["C"] == 0:
            continue
        ratio = C_cand / chosen["C"]
        if ratio >= 0.9:
            supp2, few2 = comb_support(cand_lag, all_peak_lags, L_max)
            Z_cand = _Z_at(Ls, Z, cand_lag)
            demoted = {"L": cand_lag, "Z": Z_cand, "C": C_cand, "supp": supp2, "few_periods": few2}
            if ratio >= 0.99:
                chosen = demoted
                few_periods = few2
            else:
                borderline_pending = (dict(chosen, **{}), demoted)
                chosen = demoted
                few_periods = few2
            break

    ambiguity_list = sorted(fund_candidates, key=lambda d: d["Z"] * d["supp"], reverse=True)[:3]
    ambiguous = False
    if len(fund_candidates) >= 2:
        if not all(_near_small_integer_ratio(a["L"], chosen["L"]) for a in fund_candidates):
            ambiguous = True

    verdict_cap = None
    if ambiguous:
        for d in ambiguity_list:
            d["period_s"] = d["L"] / analysis_fps
        if _is_interactive(ctx):
            chosen = ctx.ui.pick_candidate(ambiguity_list)
        else:
            chosen = ambiguity_list[0]
            verdict_cap = "MEDIUM"

    L0 = chosen["L"]
    Z0 = chosen["Z"]
    C0 = chosen["C"]
    supp0 = chosen["supp"]

    conf = _clamp01(0.45 * _sigmoid((Z0 - 4) / 3) + 0.35 * supp0 + 0.20 * C0)
    if few_periods:
        conf *= 0.75
    if provisional_only:
        conf = min(conf, 0.59)

    if conf >= 0.80:
        verdict = "HIGH"
    elif conf >= 0.60:
        verdict = "MEDIUM"
    elif conf >= 0.40:
        verdict = "LOW"
    else:
        verdict = "NONE"
    if verdict_cap == "MEDIUM" and verdict == "HIGH":
        verdict = "MEDIUM"

    period_s = L0 / analysis_fps
    RL0 = float(R[L0 - L_min]) if 0 <= L0 - L_min < len(R) else float(np.max(R))
    loop_start_s = _loop_start(Fhat, L0, b, RL0, analysis_fps)

    result = CoarseResult(
        period_lag=L0, period_s=period_s, confidence=conf, verdict=verdict,
        candidates=[(d.get("period_s", d["L"] / analysis_fps), d["Z"], d["supp"]) for d in ambiguity_list],
        loop_start_s=loop_start_s, curve=R, few_periods=few_periods, provisional_only=provisional_only,
    )
    if borderline_pending is not None:
        larger, smaller = max(borderline_pending, key=lambda d: d["L"]), min(borderline_pending, key=lambda d: d["L"])
        result.borderline = (larger["L"] / analysis_fps, smaller["L"] / analysis_fps)

    # NOTE: NONE/STATIC verdicts are NOT raised here. All fatal-verdict handling
    # is centralized in detect_period() so that the no-peaks early-return above
    # and the low-confidence path below are guarded identically (and so the
    # sub-second rescue can inspect a NONE result before deciding the message).
    return result


def detect_period(ctx, F) -> CoarseResult:
    """§3.5. Verdict NONE/STATIC raises StageError (exit 5) unless --detect-only."""
    args = ctx.args
    analysis_fps = args.analysis_fps

    result = _detect_pass(ctx, F, analysis_fps, args.min_period, args.max_period, pass_label="coarse")

    if result.verdict == "STATIC":
        if args.detect_only:
            return result
        raise StageError("detect", "Video is (nearly) static -- any cut loops trivially; "
                                    "no meaningful period exists.", code=5)

    if result.verdict == "NONE" and result.period_lag is not None:
        L_min = max(2, math.ceil(args.min_period * analysis_fps))
        if result.period_lag <= L_min + 1 or (result.curve is not None and _is_pinned(result.curve)):
            rescue_fps = analysis_fps * 4
            window_s = min(ctx.info["duration"], 600)
            n_frames = int(window_s * rescue_fps)
            F2 = extract_fingerprints(
                ctx, ctx.paths["lowres"], fps=rescue_fps, size=(32, 18),
                expect_frames=n_frames, stage="detect",
                task_label="Re-analyzing (sub-second loop rescue)", t_limit=window_s,
            )
            result2 = _detect_pass(ctx, F2, rescue_fps, args.min_period, args.max_period, pass_label="rescue")
            if result2.verdict == "STATIC":
                result2.verdict = "NONE"
            if result2.verdict != "NONE":
                return result2
            if args.detect_only:
                return result2
            raise StageError(
                "detect",
                "No reliable repeating loop detected; loop may be < 0.5s -- try --analysis-fps 10.",
                code=5,
            )

    # Final guard: any NONE that reached here (no peaks at a mid-range lag, or a
    # low-confidence winner) is fatal unless --detect-only. This is the single
    # authoritative NONE exit -- without it the pipeline would silently encode a
    # bogus loop and exit 0 on non-periodic footage whose best lag is mid-range.
    if result.verdict == "NONE":
        if args.detect_only:
            return result
        raise StageError(
            "detect",
            f"No reliable repeating loop detected (best candidate {result.period_s:.2f}s, "
            f"confidence {result.confidence:.2f}). If you know the period, re-run with --period SECONDS.",
            code=5,
        )
    return result


# --------------------------------------------------------------------------
# §3.6 refine_period
# --------------------------------------------------------------------------


def _offset_search(Fhat, lo, hi, j0):
    T = Fhat.shape[0]
    best_j = None
    best_S = -2.0
    scores = {}
    for j in range(max(1, lo), hi + 1):
        max_i = T - j
        if max_i <= 0:
            continue
        idx = np.arange(max_i)
        sims = _sim_rows(Fhat, idx, idx + j)
        S = float(sims.mean())
        scores[j] = S
        if S > best_S:
            best_S = S
            best_j = j
    if best_j is None:
        return j0, 0.0, 0
    tied = [j for j, S in scores.items() if abs(S - best_S) < 1e-4]
    if len(tied) > 1:
        best_j = min(tied, key=lambda j: abs(j - j0))
    return best_j, best_S, len(tied)


def _ffprobe_stream_info(ctx, path, stage="refine"):
    argv = [ctx.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", str(path)]
    cp = run_cmd(ctx, argv, stage=stage, timeout=60)
    data = json.loads(cp.stdout)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    avg = vstream.get("avg_frame_rate") or "0/1"
    rfr = vstream.get("r_frame_rate") or avg
    fps_avg = _parse_fraction(avg)
    fps_r = _parse_fraction(rfr)
    is_vfr = abs(fps_avg - fps_r) > 0.01 if (fps_avg and fps_r) else False
    height = int(vstream.get("height") or 0)
    fps_hq = ctx.chosen_fmt.get("fps") if ctx.chosen_fmt else ctx.info["fps"]
    fps_hq = fps_hq or ctx.info["fps"] or 30.0
    got_hq_fps = bool(fps_avg) and fps_avg >= (fps_hq - 0.5)
    return {"fps": fps_avg if fps_avg else ctx.info["fps"], "is_vfr": is_vfr, "got_hq_fps": got_hq_fps, "height": height}


def _download_refine_sample(ctx, start, length, fps_hq):
    args = ctx.args
    seg = f"*{start}-{start + length}"
    selector = f"bv*[height<=720][fps>={fps_hq}]/bv*[fps>={fps_hq}]/bv*[height<=480]/wv*[ext!=mhtml]/b"
    out_tmpl = str(ctx.workdir / "refine.%(ext)s")
    argv = _ytdlp_base_argv(ctx) + [
        "-f", selector,
        "--download-sections", seg,
        "--no-part", "--newline",
        "--progress-template", _PROGRESS_TEMPLATE,
        "-o", out_tmpl,
        "--", ctx.info["webpage_url"],
    ]
    task_key = ctx.ui.start_task("refine_dl", "Download refine sample", total=None, kind="download")
    parser = _make_ytdlp_progress_parser(ctx, task_key)
    try:
        try:
            run_cmd(ctx, argv, stage="refine", progress_parser=parser)
        except StageError:
            if ctx.info["duration"] and ctx.info["duration"] <= 900:
                _delete_partials(ctx.workdir, "refine")
                argv2 = _strip_flag_with_value(argv, "--download-sections")
                argv2 = [a for a in argv2 if a != seg]
                run_cmd(ctx, argv2, stage="refine", progress_parser=parser)
                ctx.ui.note_detail("refine", "whole-file fallback (sectioned download unsupported)")
            else:
                raise
    finally:
        ctx.ui.finish_task(task_key)

    matches = [p for p in ctx.workdir.glob("refine.*") if p.suffix != ".part"]
    if not matches:
        raise StageError("refine", "yt-dlp did not produce a refine sample.", code=4)
    return matches[0]


def refine_period(ctx) -> LoopResult:
    args = ctx.args
    coarse = ctx.coarse
    fps_hq = ctx.chosen_fmt.get("fps") if ctx.chosen_fmt else ctx.info["fps"]
    fps_hq = fps_hq or ctx.info["fps"] or 30.0
    period_s = coarse.period_s
    start = coarse.loop_start_s
    duration = ctx.info["duration"] or (period_s * 4)

    larger_period = period_s
    if coarse.borderline:
        larger_period = max(coarse.borderline)

    length = min(2 * larger_period + 5, duration - start)
    if length < 1.5 * larger_period:
        start = max(0.0, start - (1.5 * larger_period - length))
        length = min(2 * larger_period + 5, duration - start)
    if length < 1.5 * larger_period:
        N = round(period_s * fps_hq)
        return LoopResult(frames=int(N), fps=fps_hq, period_s=N / fps_hq, start_s=coarse.loop_start_s,
                           confidence=coarse.confidence, verdict=coarse.verdict)

    if args.local_file:
        # No separate "refine download" for a local file -- it already contains the
        # exact HQ-fps source; fingerprint a local relative-time window of it directly.
        refine_path = ctx.paths["lowres"]
        info = _ffprobe_stream_info(ctx, refine_path)
        fps_used = info["fps"] or fps_hq
        ctx.info["is_vfr"] = bool(info["is_vfr"] or ctx.info.get("is_vfr", False))
        got_hq_fps = True
        F = _extract_fingerprints_window(ctx, refine_path, fps_used, (48, 27), start, length, stage="refine")
    else:
        refine_path = _download_refine_sample(ctx, start, length, fps_hq)
        info = _ffprobe_stream_info(ctx, refine_path)
        fps_used = info["fps"] or fps_hq
        ctx.info["is_vfr"] = bool(info["is_vfr"] or ctx.info.get("is_vfr", False))
        got_hq_fps = info["got_hq_fps"]
        F = extract_fingerprints(ctx, refine_path, fps=None, size=(48, 27), stage="refine",
                                  task_label="Frame-exact refinement")
    Fhat, _ = normalize_fingerprints(F)

    def _score_window(period_guess_s):
        j0 = round(period_guess_s * fps_used)
        halfwin = math.ceil(1.5 * fps_used)
        lo = max(1, j0 - halfwin)
        hi = j0 + halfwin
        return _offset_search(Fhat, lo, hi, j0)

    j_best, S_best, plateau = _score_window(period_s)
    if coarse.borderline:
        larger_j, larger_S, larger_plateau = _score_window(max(coarse.borderline))
        if larger_S > S_best:
            j_best, S_best, plateau = larger_j, larger_S, larger_plateau

    verdict = coarse.verdict
    if S_best < 0.985:
        verdict = _downgrade_verdict(verdict)

    rescaled = False
    if got_hq_fps:
        N = j_best
        fps_final = fps_used
    else:
        N_native = j_best
        N = round(N_native * fps_hq / fps_used)
        check = abs(N / fps_hq - N_native / fps_used)
        if check > 0.5 / fps_hq:
            ctx.ui.note_detail("refine", "frame count may be +/-1; seam verify will arbitrate")
        fps_final = fps_hq
        rescaled = True

    N = max(2, int(N))
    return LoopResult(
        frames=N, fps=fps_final, period_s=N / fps_final, start_s=coarse.loop_start_s,
        confidence=coarse.confidence, verdict=verdict, plateau_width=plateau, rescaled=rescaled,
    )


# --------------------------------------------------------------------------
# §3.7 download_hq_segment
# --------------------------------------------------------------------------


def _hq_format_selector(ctx):
    if ctx.args.format:
        return ctx.args.format
    if ctx.chosen_fmt:
        return ctx.chosen_fmt["format_id"]
    return _fallback_selector_string(ctx.args.max_height)


def _run_sectioned_with_retry(ctx, argv, seg, stage, parser, glob_prefix):
    try:
        run_cmd(ctx, argv, stage=stage, progress_parser=parser)
        return
    except StageError:
        _delete_partials(ctx.workdir, glob_prefix)
    try:
        run_cmd(ctx, argv, stage=stage, progress_parser=parser)
        return
    except StageError as e:
        if ctx.info["duration"] and ctx.info["duration"] <= 900:
            argv2 = _strip_flag_with_value(argv, "--download-sections")
            argv2 = [a for a in argv2 if a != seg]
            _delete_partials(ctx.workdir, glob_prefix)
            run_cmd(ctx, argv2, stage=stage, progress_parser=parser)
            ctx.ui.note_detail(stage, "whole-file fallback (sectioned download unsupported)")
            return
        raise StageError(stage, "HQ segment download failed after retry.", hint=str(e),
                          log_path=str(ctx.workdir / "logs" / f"{stage}.log"), code=4)


def _do_hq_download(ctx, seg_start, seg_end):
    seg = f"*{seg_start}-{seg_end}"
    selector = _hq_format_selector(ctx)
    out_tmpl = str(ctx.workdir / "raw.%(ext)s")
    argv = _ytdlp_base_argv(ctx) + [
        "-f", selector,
        "--download-sections", seg,
        "--no-part", "--newline",
        "--progress-template", _PROGRESS_TEMPLATE,
        "-o", out_tmpl,
        "--", ctx.info["webpage_url"],
    ]
    task_key = ctx.ui.start_task("hq_dl", "Download max-quality segment", total=None, kind="download")
    parser = _make_ytdlp_progress_parser(ctx, task_key)
    try:
        _run_sectioned_with_retry(ctx, argv, seg, "hq", parser, "raw")
    finally:
        ctx.ui.finish_task(task_key)
    matches = [p for p in ctx.workdir.glob("raw.*") if p.suffix != ".part"]
    if not matches:
        raise StageError("hq", "yt-dlp did not produce an HQ segment.", code=4)
    ctx.paths["raw"] = matches[0]


def _muxed_audio_present(ctx):
    if ctx.args.local_file:
        return True
    return bool(ctx.chosen_fmt and ctx.chosen_fmt.get("acodec") not in (None, "none"))


def _do_audio_download(ctx, seg_start, seg_end):
    if _muxed_audio_present(ctx):
        return
    seg = f"*{seg_start}-{seg_end}"
    out_tmpl = str(ctx.workdir / "rawaudio.%(ext)s")
    argv = _ytdlp_base_argv(ctx) + [
        "-f", "ba/b",
        "--download-sections", seg,
        "--no-part", "--newline",
        "--progress-template", _PROGRESS_TEMPLATE,
        "-o", out_tmpl,
        "--", ctx.info["webpage_url"],
    ]
    task_key = ctx.ui.start_task("audio_dl", "Download audio segment", total=None, kind="download")
    parser = _make_ytdlp_progress_parser(ctx, task_key)
    try:
        _run_sectioned_with_retry(ctx, argv, seg, "hq", parser, "rawaudio")
    finally:
        ctx.ui.finish_task(task_key)
    matches = [p for p in ctx.workdir.glob("rawaudio.*") if p.suffix != ".part"]
    if matches:
        ctx.paths["rawaudio"] = matches[0]


def download_hq_segment(ctx):
    args = ctx.args
    loop_start = ctx.loop.start_s
    period_s = ctx.loop.period_s
    seg_start = 0.0 if loop_start == 0 else max(0.0, loop_start - 5)
    seg_end = loop_start + period_s + 12
    ctx.paths["seg_start"] = seg_start
    ctx.paths["seg_end"] = seg_end
    _do_hq_download(ctx, seg_start, seg_end)
    if args.audio:
        _do_audio_download(ctx, seg_start, seg_end)


# --------------------------------------------------------------------------
# §3.7b align_segment (fixes the absolute-time gap; §0.6)
# --------------------------------------------------------------------------


def _ffprobe_duration(ctx, path, stage="align"):
    argv = [ctx.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    cp = run_cmd(ctx, argv, stage=stage, timeout=60)
    data = json.loads(cp.stdout)
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _cross_correlate_shift(Fhat_low, Fhat_raw):
    T_low = Fhat_low.shape[0]
    T_raw = Fhat_raw.shape[0]
    best_shift = 0
    best_score = -2.0
    max_shift = max(0, T_low - 1)
    for shift in range(0, max_shift + 1):
        overlap = min(T_raw, T_low - shift)
        if overlap <= 0:
            break
        sims = np.einsum("ij,ij->i", Fhat_low[shift:shift + overlap], Fhat_raw[:overlap])
        score = float(sims.mean())
        if score > best_score:
            best_score = score
            best_shift = shift
    return best_shift


def _fingerprint_align(ctx, loop_start, seg_start):
    raw = ctx.paths["raw"]
    raw_duration = _ffprobe_duration(ctx, raw)
    align_fps = 4
    size = (32, 18)
    clip_len = min(20.0, raw_duration) if raw_duration else 20.0

    F_raw = extract_fingerprints(ctx, raw, fps=align_fps, size=size, stage="align",
                                  task_label="Aligning to source timeline", t_limit=clip_len)
    strip_start = max(0.0, seg_start - 15)
    strip_len = min((seg_start + 25) - strip_start, max(0.1, ctx.info["duration"] - strip_start))
    F_low = _extract_fingerprints_window(ctx, ctx.paths["lowres"], align_fps, size, strip_start, strip_len)

    Fhat_raw, _ = normalize_fingerprints(F_raw)
    Fhat_low, _ = normalize_fingerprints(F_low)
    best_shift = _cross_correlate_shift(Fhat_low, Fhat_raw)
    raw_abs_start = strip_start + best_shift / align_fps
    return max(0.0, (loop_start + 0.5) - raw_abs_start)


def align_segment(ctx) -> float:
    args = ctx.args
    if args.local_file:
        ctx.hq_offset = ctx.loop.start_s
        ctx.paths["audio_offset"] = ctx.loop.start_s
        return ctx.hq_offset

    retried = False
    loop_start = ctx.loop.start_s
    seg_start = ctx.paths.get("seg_start", 0.0)
    while True:
        loop_start = ctx.loop.start_s
        seg_start = ctx.paths.get("seg_start", 0.0)
        if loop_start == 0 and seg_start == 0:
            ctx.hq_offset = 0.0
        else:
            ctx.hq_offset = _fingerprint_align(ctx, loop_start, seg_start)

        N = ctx.loop.frames
        fps = ctx.loop.fps
        raw_duration = _ffprobe_duration(ctx, ctx.paths["raw"])
        needed = N / fps + 0.25
        if raw_duration - ctx.hq_offset >= needed:
            break
        if retried:
            raise StageError("align", "HQ segment coverage insufficient even after retry.", code=4)
        retried = True
        seg_end2 = ctx.paths["seg_end"] + 15
        _delete_partials(ctx.workdir, "raw")
        _do_hq_download(ctx, seg_start, seg_end2)

    if args.audio and ctx.paths.get("rawaudio"):
        ctx.paths["audio_offset"] = max(0.0, loop_start - seg_start)
    else:
        ctx.paths["audio_offset"] = 0.0
    return ctx.hq_offset


# --------------------------------------------------------------------------
# §3.8 encode_loop — the ONE encode in the whole pipeline (§0.1)
# --------------------------------------------------------------------------


def _detect_hdr(ctx, path):
    argv = [ctx.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=color_transfer,color_primaries,color_space", "-of", "json", str(path)]
    try:
        cp = run_cmd(ctx, argv, stage="encode", timeout=60)
        data = json.loads(cp.stdout)
        stream = (data.get("streams") or [{}])[0]
    except Exception:
        stream = {}
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    is_hdr = transfer in ("smpte2084", "arib-std-b67") or primaries == "bt2020"
    if not is_hdr:
        return None
    has_mastering = False
    try:
        argv2 = [ctx.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                 "-read_intervals", "%+#1", "-show_entries", "frame_side_data_list",
                 "-of", "json", str(path)]
        cp2 = run_cmd(ctx, argv2, stage="encode", timeout=60)
        has_mastering = "mastering" in cp2.stdout.lower()
    except Exception:
        pass
    return {"transfer": transfer, "primaries": primaries, "has_mastering_display": has_mastering}


def _count_output_frames(ctx, path):
    argv = [ctx.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=nb_frames", "-of", "json", str(path)]
    cp = run_cmd(ctx, argv, stage="encode", timeout=60)
    data = json.loads(cp.stdout)
    stream = (data.get("streams") or [{}])[0]
    nb = stream.get("nb_frames")
    try:
        nb_int = int(nb)
        if nb_int > 0:
            return nb_int
    except (TypeError, ValueError):
        pass
    argv2 = [ctx.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)]
    cp2 = run_cmd(ctx, argv2, stage="encode", timeout=180)
    data2 = json.loads(cp2.stdout)
    stream2 = (data2.get("streams") or [{}])[0]
    try:
        return int(stream2.get("nb_read_frames"))
    except (TypeError, ValueError):
        return -1


def _make_ffmpeg_progress_parser(ctx, task_key):
    def parser(line):
        line = line.strip()
        if line.startswith("frame="):
            val = _num(line.partition("=")[2])
            if val is not None:
                ctx.ui.advance_task(task_key, completed=val)
        elif line == "progress=end":
            pass

    return parser


def encode_loop(ctx) -> Path:
    args = ctx.args
    N = ctx.loop.frames
    fps = ctx.loop.fps
    raw = ctx.paths["raw"]
    ext = _extension_for_codec(args.codec)
    out_path = ctx.workdir / f"loop_single{ext}"

    hdr = _detect_hdr(ctx, raw)
    vfr = bool(ctx.info.get("is_vfr", False))

    vf_chain = []
    if vfr:
        vf_chain.append(f"fps={fps}")
    if hdr and args.codec == "h264":
        vf_chain.append("zscale=t=linear:npl=100,tonemap=hable,zscale=p=bt709:t=bt709:m=bt709,format=yuv420p")
    src_height = ctx.info.get("height") or (1 << 30)
    if args.max_height and args.max_height < src_height:
        vf_chain.append(f"scale=-2:{args.max_height}:flags=lanczos")

    argv = [ctx.ffmpeg, "-y"]
    if ctx.hq_offset > 0:
        argv += ["-ss", f"{ctx.hq_offset}"]
    argv += ["-i", str(raw)]

    have_audio_input = False
    audio_offset = ctx.paths.get("audio_offset", 0.0)
    rawaudio = ctx.paths.get("rawaudio")
    if args.audio and rawaudio:
        if audio_offset > 0:
            argv += ["-ss", f"{audio_offset}"]
        argv += ["-i", str(rawaudio)]
        have_audio_input = True

    argv += ["-frames:v", str(N)]

    if args.codec == "hevc":
        argv += ["-c:v", "libx265", "-crf", str(args.crf), "-preset", args.preset, "-tag:v", "hvc1"]
    else:
        argv += ["-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset]

    if hdr and args.codec == "hevc":
        argv += ["-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                  "-colorspace", "bt2020nc"]
        if hdr.get("has_mastering_display"):
            argv += ["-x265-params", "hdr10=1"]
        ctx.ui.note_detail("encode", "HDR preserved (10-bit)")
    else:
        argv += ["-pix_fmt", "yuv420p"]
        if hdr and args.codec == "h264":
            ctx.ui.note_detail("encode", "tonemapped to SDR")

    if vf_chain:
        argv += ["-vf", ",".join(vf_chain)]

    muxed_audio_ok = _muxed_audio_present(ctx)
    if args.audio and have_audio_input:
        argv += ["-map", "0:v:0", "-map", "1:a:0"]
        fade_out_start = max(0.0, ctx.loop.period_s - 0.05)
        argv += ["-c:a", "aac", "-b:a", "256k",
                 "-af", f"afade=t=in:d=0.05,afade=t=out:st={fade_out_start:.3f}:d=0.05", "-shortest"]
    elif args.audio and muxed_audio_ok:
        argv += ["-map", "0:v:0", "-map", "0:a:0?"]
        fade_out_start = max(0.0, ctx.loop.period_s - 0.05)
        argv += ["-c:a", "aac", "-b:a", "256k",
                 "-af", f"afade=t=in:d=0.05,afade=t=out:st={fade_out_start:.3f}:d=0.05", "-shortest"]
    else:
        argv += ["-an"]

    argv += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(out_path)]

    task_key = ctx.ui.start_task("encode", "Encoding seamless loop", total=N, kind="analysis")
    parser = _make_ffmpeg_progress_parser(ctx, task_key)
    try:
        try:
            run_cmd(ctx, argv, stage="encode", progress_parser=parser)
        except StageError as e:
            if hdr and args.codec == "h264" and "zscale" in (e.hint or "").lower():
                raise StageError("encode", "zscale/tonemap filter unavailable in this ffmpeg build.",
                                  hint="retry with --codec hevc", code=6)
            raise
    finally:
        ctx.ui.finish_task(task_key)

    nb_frames = _count_output_frames(ctx, out_path)
    if nb_frames != N:
        if not ctx._encode_retry_done:
            ctx._encode_retry_done = True
            ctx.ui.note_detail("encode", f"nb_frames mismatch ({nb_frames} != {N}), retrying with larger pad")
            ctx.paths["seg_end"] = ctx.paths["seg_end"] + 15
            _delete_partials(ctx.workdir, "raw")
            _do_hq_download(ctx, ctx.paths["seg_start"], ctx.paths["seg_end"])
            align_segment(ctx)
            return encode_loop(ctx)
        raise StageError("encode", f"Encoded output has {nb_frames} frames, expected {N}.", code=6)

    ctx.paths["loop_single"] = out_path
    return out_path


# --------------------------------------------------------------------------
# §3.9 verify_seam
# --------------------------------------------------------------------------


def verify_seam(ctx, single) -> SeamResult:
    F = extract_fingerprints(ctx, single, fps=None, size=(32, 18), expect_frames=ctx.loop.frames,
                              stage="verify", task_label="Verifying seam")
    Fhat, _ = normalize_fingerprints(F)
    N = Fhat.shape[0]
    if N < 2:
        return SeamResult(wrap_similarity=1.0, adjacent_p5=1.0, z=0.0, seamless=True)

    A = _sim_rows(Fhat, np.arange(N - 1), np.arange(1, N))
    w = float(np.dot(Fhat[N - 1], Fhat[0]))
    p5 = float(np.percentile(A, 5))
    # Judge the seam in DISSIMILARITY space: the wrap (last->first) is seamless
    # if its frame-to-frame jump is not a high outlier versus the normal
    # adjacent-frame jumps. This is robust in both directions:
    #   * near-static ambient scenes have an ultra-tight similarity cluster, so
    #     an absolute floor like `median - 3*MADsigma` rejects any below-median
    #     wrap even when it is visually perfect -- a dissimilarity CEILING does not.
    #   * a loop containing a legit internal hard cut has one large dissimilarity;
    #     the 95th percentile stays at normal-step level, so a wrap as jarring as
    #     that cut still exceeds the ceiling and is correctly flagged.
    d = 1.0 - A
    dw = 1.0 - w
    med_d = float(np.median(d))
    mad_d = 1.4826 * float(np.median(np.abs(d - med_d)))
    allow = max(float(np.percentile(d, 95)), med_d + 3.0 * mad_d)
    seamless = bool(dw <= allow + 1e-9)
    a_std = float(np.std(A))
    z = (w - float(np.mean(A))) / a_std if a_std > 1e-9 else 0.0
    return SeamResult(wrap_similarity=w, adjacent_p5=p5, z=z, seamless=seamless)


# --------------------------------------------------------------------------
# §3.10 finalize
# --------------------------------------------------------------------------


def finalize(ctx, single) -> Path:
    args = ctx.args
    if args.loops > 1:
        loop_ext = single.suffix
        looped_path = ctx.workdir / f"loop_final{loop_ext}"
        argv = [
            ctx.ffmpeg, "-y", "-fflags", "+genpts",
            "-stream_loop", str(args.loops - 1),
            "-i", str(single), "-c", "copy", "-movflags", "+faststart", str(looped_path),
        ]
        run_cmd(ctx, argv, stage="done")
        final_local = looped_path
    else:
        final_local = single

    final_name = _build_output_filename(ctx, period_s=ctx.loop.period_s, height=ctx.info.get("height") or 0)
    out_dir = ctx.paths["out_dir"]
    dest = ctx.paths.get("out_explicit_file") or (out_dir / final_name)
    if dest.exists():
        if _is_interactive(ctx):
            resp = ctx.ui.prompt(f"{dest.name} already exists. Overwrite? [y/N] (anything else auto-renames): ",
                                  default="n")
            if not (resp and resp.lower().startswith("y")):
                dest = _unique_path(dest)
        else:
            dest = _unique_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(final_local), str(dest))
    ctx.paths["out_final"] = dest
    return dest


# --------------------------------------------------------------------------
# §1.1 argparse surface
# --------------------------------------------------------------------------


def build_argparser():
    epilog = textwrap.dedent(
        """\
        Examples:
          %(prog)s "https://youtu.be/XXXXXXXXXXX"
          %(prog)s "https://youtu.be/XXXXXXXXXXX" -y --json -o ~/Movies
        """
    )
    p = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Detect and extract a seamless video loop from any yt-dlp-supported URL.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", default=None, help="Video URL (any yt-dlp-supported site).")
    p.add_argument("-o", "--output", default=None, metavar="PATH", help="Output file or directory.")
    p.add_argument("--codec", choices=["hevc", "h264"], default="hevc",
                    help="hevc -> libx265 .mov (default); h264 -> libx264 .mp4.")
    p.add_argument("--crf", type=int, default=18, help="Encode quality, 0-51 (default 18).")
    p.add_argument("--preset", default="slow", help="x264/x265 preset (default slow).")
    p.add_argument("--max-height", type=int, default=0, metavar="N",
                    help="Resolution cap; 0 = source max (default).")
    p.add_argument("--loops", type=int, default=1, help="Repetitions in the output file (default 1).")
    p.add_argument("--audio", action="store_true", help="Include audio (default silent).")
    p.add_argument("--start", default=None, metavar="TIME",
                    help="Force extraction window start (SS, MM:SS, HH:MM:SS).")
    p.add_argument("--period", type=float, default=None, metavar="SECONDS",
                    help="Skip coarse detection; use this candidate period.")
    p.add_argument("--frames", type=int, default=None, metavar="N",
                    help="Skip ALL detection; extract exactly N frames. Implies --no-refine.")
    p.add_argument("--no-refine", action="store_true", help="Skip frame-exact refinement.")
    p.add_argument("--detect-only", action="store_true",
                    help="Stop after detection+refinement; print verdict; no HQ/encode.")
    p.add_argument("--analysis-fps", type=float, default=1.0, help="Fingerprint sampling rate (default 1.0).")
    p.add_argument("--min-period", type=float, default=2.0, help="Lag search minimum, seconds (default 2.0).")
    p.add_argument("--max-period", type=float, default=0.0,
                    help="Lag search maximum, seconds (default 0 = duration/2).")
    p.add_argument("--format", default=None, metavar="SELECTOR",
                    help="Raw yt-dlp format selector override for the HQ download.")
    p.add_argument("--cookies-from-browser", default=None, metavar="BROWSER",
                    help="Passed verbatim to every yt-dlp call.")
    p.add_argument("--work-dir", default=None, metavar="PATH", help="Workspace dir instead of a fresh tempdir.")
    p.add_argument("--keep-temp", action="store_true", help="Don't delete workspace on exit.")
    p.add_argument("--local-file", default=None, metavar="PATH", help=argparse.SUPPRESS)
    p.add_argument("-y", "--yes", action="store_true", help="Fully non-interactive: accept defaults.")
    p.add_argument("--json", action="store_true", help="Machine-readable result object on stdout.")
    p.add_argument("-v", "--verbose", action="store_true", help="Log every subprocess argv and internals.")
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress fancy UI; single summary line.")
    p.add_argument("--version", action="version", version=f"{PROG_NAME} {VERSION}")
    return p


def validate_args(parser, args):
    if args.loops < 1:
        parser.error("--loops must be >= 1")
    if not (0 <= args.crf <= 51):
        parser.error("--crf must be between 0 and 51")
    if args.frames is not None and args.frames < 2:
        parser.error("--frames must be >= 2")
    if args.period is not None and args.frames is not None:
        parser.error("--period and --frames are mutually exclusive")
    if args.quiet and args.verbose:
        parser.error("--quiet and --verbose are mutually exclusive")
    if args.max_period and args.min_period >= args.max_period:
        parser.error("--min-period must be < --max-period")
    if not (0.1 < args.analysis_fps <= 30):
        parser.error("--analysis-fps must be in (0.1, 30]")
    if args.frames is not None:
        args.no_refine = True


# --------------------------------------------------------------------------
# §1.2 interactive prompt flow
# --------------------------------------------------------------------------


def _resolve_start(ctx):
    if ctx.args.start is not None:
        return parse_time_to_seconds(ctx.args.start)
    return 0.0


def _resolve_interactive_answers(ctx):
    args = ctx.args
    if not _is_interactive(ctx):
        return

    heights = sorted({f.get("height") for f in (ctx.info.get("formats") or []) if f.get("height")}, reverse=True)
    if not args.max_height and heights and not _flag_explicit(ctx, "max_height"):
        max_h = heights[0]
        opts = [h for h in heights if h < max_h][:4]
        opts_str = "/".join(str(h) for h in opts) if opts else "no lower options"
        fps_disp = ctx.info.get("fps") or 0
        resp = ctx.ui.prompt(
            f"Max quality is {ctx.info['width']}x{ctx.info['height']}@{fps_disp:g}. "
            f"Cap resolution? [Enter = max, or {opts_str}]: ",
            default="",
        )
        if resp:
            try:
                args.max_height = int(resp)
            except ValueError:
                pass

    if not _flag_explicit(ctx, "codec"):
        resp = ctx.ui.prompt(
            "Output codec? [1] HEVC .mov (Apple-native, default)  [2] H.264 .mp4 (max compatibility): ",
            default="1",
        )
        if resp and resp.strip() == "2":
            args.codec = "h264"

    if not _flag_explicit(ctx, "audio"):
        args.audio = ctx.ui.confirm("Include audio?", default=False)

    if not _flag_explicit(ctx, "loops"):
        resp = ctx.ui.prompt("Repetitions in output file? [1]: ", default="1")
        try:
            args.loops = max(1, int(resp))
        except (TypeError, ValueError):
            args.loops = 1


# --------------------------------------------------------------------------
# JSON payload / human summary
# --------------------------------------------------------------------------


def _adjust_verdict(verdict, seam):
    if seam is not None and not seam.seamless:
        return _downgrade_verdict(verdict)
    return verdict


def _build_json_payload(ctx, timings, seam=None, output_path=None, status="ok", verdict=None):
    payload = {
        "status": status,
        "url": (ctx.info.get("webpage_url") if ctx.info else ctx.args.url),
        "title": (ctx.info.get("title") if ctx.info else None),
        "source": ({
            "width": ctx.info.get("width"), "height": ctx.info.get("height"),
            "fps": ctx.info.get("fps"), "duration": ctx.info.get("duration"),
            "format_id": (ctx.chosen_fmt.get("format_id") if ctx.chosen_fmt else None),
        } if ctx.info else {}),
        "loop": ({
            "frames": ctx.loop.frames, "period_s": ctx.loop.period_s, "start_s": ctx.loop.start_s,
            "confidence": ctx.loop.confidence, "verdict": verdict or ctx.loop.verdict,
        } if ctx.loop else {}),
        "timings": timings,
    }
    if seam is not None:
        payload["seam"] = {
            "wrap_similarity": seam.wrap_similarity, "adjacent_p5": seam.adjacent_p5,
            "z": seam.z, "seamless": seam.seamless,
        }
    if output_path is not None:
        try:
            size_bytes = output_path.stat().st_size
        except OSError:
            size_bytes = None
        payload["output"] = {
            "path": str(output_path), "codec": ctx.args.codec, "size_bytes": size_bytes, "loops": ctx.args.loops,
        }
    return payload


def _emit_result(ctx, payload, error=False):
    if ctx.args.json:
        print(json.dumps(payload), file=sys.stdout)
        sys.stdout.flush()
    if error and not ctx.args.json:
        msg = payload.get("message", "Unknown error")
        hint = payload.get("hint")
        sys.stderr.write(f"Error ({payload.get('stage')}): {msg}\n")
        if hint:
            sys.stderr.write(f"Hint: {hint}\n")
        log_path = payload.get("log_path")
        if log_path:
            sys.stderr.write(f"Log: {log_path}\n")


def _print_human_summary(ctx, payload):
    if ctx.args.json:
        return
    lines = []
    loop = payload.get("loop", {})
    out = payload.get("output")
    seam = payload.get("seam")
    if loop:
        lines.append(f"Loop: {loop.get('frames')} frames, {loop.get('period_s'):.3f}s, verdict {loop.get('verdict')}")
    if seam:
        lines.append(f"Seam: {'OK' if seam['seamless'] else 'NOT SEAMLESS'} (z={seam['z']:.2f})")
    if out:
        lines.append(f"Output: {out['path']}")
    ctx.ui.print_summary(lines)


# --------------------------------------------------------------------------
# §6.1 workspace lifecycle / cleanup / signal handling
# --------------------------------------------------------------------------


def _register_cleanup(ctx):
    global _CTX_FOR_SIGNAL
    _CTX_FOR_SIGNAL = ctx
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(_atexit_cleanup)


def _cleanup(ctx):
    global _CLEANED_UP
    if _CLEANED_UP:
        return
    _CLEANED_UP = True
    if ctx.keep_workdir_due_to_failure or ctx.args.keep_temp:
        sys.stderr.write(f"Workspace kept for inspection: {ctx.workdir}\n")
        return
    if ctx.args.work_dir:
        for pattern in ("lowres.*", "refine.*", "raw.*", "rawaudio.*", "loop_single.*", "loop_final.*"):
            for f in ctx.workdir.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass
        for sub in ("logs", "frames"):
            d = ctx.workdir / sub
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
    else:
        shutil.rmtree(ctx.workdir, ignore_errors=True)


def _atexit_cleanup():
    ctx = _CTX_FOR_SIGNAL
    if ctx is None:
        return
    _cleanup(ctx)


def _signal_handler(signum, frame):
    ctx = _CTX_FOR_SIGNAL
    if ctx is not None:
        try:
            ctx.ui.stop()
        except Exception:
            pass
    _kill_all_children()
    if ctx is not None:
        try:
            ctx.keep_workdir_due_to_failure = False
            _cleanup(ctx)
        except Exception:
            pass
    sys.stderr.write("\nInterrupted.\n")
    os._exit(130)


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------


def main(argv=None):
    parser = build_argparser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw_argv)
    validate_args(parser, args)

    stdin_tty = sys.stdin.isatty()
    if args.url is None and not args.local_file and not stdin_tty:
        parser.error("the following arguments are required: url (or use --local-file)")

    if args.work_dir:
        workdir = Path(args.work_dir).expanduser()
    else:
        workdir = Path(tempfile.mkdtemp(prefix="video-loop-extractor-"))
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "logs").mkdir(parents=True, exist_ok=True)

    ui = UI(args)
    ctx = Ctx(args=args, workdir=workdir, ytdlp=[], ui=ui, raw_argv=raw_argv)
    _register_cleanup(ctx)

    timings = {}
    try:
        env = check_environment(args)
        ctx.ffmpeg = env.ffmpeg
        ctx.ffprobe = env.ffprobe
        ctx.ytdlp = env.ytdlp
        ctx.remote_components_supported = env.remote_components_supported

        _mark_skipped_stages(ctx)
        ui.start()

        if args.url is None and not args.local_file:
            while True:
                resp = ui.prompt("Video URL: ")
                if resp:
                    args.url = resp.strip()
                    break

        t0 = time.monotonic()
        ui.set_stage("probe", StageState.RUNNING)
        probe(ctx)
        timings["probe"] = time.monotonic() - t0
        ui.set_stage("probe", StageState.DONE,
                      detail=f"{ctx.info['width']}x{ctx.info['height']}@{ctx.info['fps']:g}")

        _resolve_interactive_answers(ctx)

        if args.local_file:
            ui.set_stage("lowres", StageState.SKIPPED)
        else:
            t = time.monotonic()
            ui.set_stage("lowres", StageState.RUNNING)
            download_lowres(ctx)
            timings["lowres"] = time.monotonic() - t
            ui.set_stage("lowres", StageState.DONE, detail=f"{ctx.info['duration']:.0f}s source")

        _run_detection(ctx, timings)

        if ctx.args.start is not None:
            forced = parse_time_to_seconds(ctx.args.start)
            ctx.loop.start_s = forced
            if ctx.coarse is not None:
                ctx.coarse.loop_start_s = forced

        if ctx.coarse is not None and ctx.coarse.verdict == "LOW" and not args.detect_only:
            if _is_interactive(ctx):
                proceed = ui.confirm(
                    f"Low-confidence loop detected ({ctx.coarse.period_s:.2f}s, "
                    f"confidence {ctx.coarse.confidence:.2f}). Proceed?",
                    default=False,
                )
                if not proceed:
                    raise StageError("detect", "Aborted by user after LOW-confidence detection.", code=5)
            else:
                sys.stderr.write(
                    f"LOW-confidence loop detected ({ctx.coarse.period_s:.2f}s, "
                    f"confidence {ctx.coarse.confidence:.2f}); proceeding (-y).\n"
                )

        if args.detect_only:
            for k in ("hq", "align", "encode", "verify", "done"):
                ui.set_stage(k, StageState.SKIPPED)
            ui.stop()
            payload = _build_json_payload(ctx, timings, seam=None, output_path=None, status="ok")
            _emit_result(ctx, payload)
            _print_human_summary(ctx, payload)
            _cleanup(ctx)
            return 0

        if args.local_file:
            ui.set_stage("hq", StageState.SKIPPED)
            ctx.paths["raw"] = ctx.paths["lowres"]
            t = time.monotonic()
            ui.set_stage("align", StageState.RUNNING)
            align_segment(ctx)
            timings["align"] = time.monotonic() - t
            ui.set_stage("align", StageState.DONE, detail="local file (exact)")
        else:
            t = time.monotonic()
            ui.set_stage("hq", StageState.RUNNING)
            download_hq_segment(ctx)
            timings["hq"] = time.monotonic() - t
            ui.set_stage("hq", StageState.DONE)

            t = time.monotonic()
            ui.set_stage("align", StageState.RUNNING)
            align_segment(ctx)
            timings["align"] = time.monotonic() - t
            ui.set_stage("align", StageState.DONE, detail=f"offset {ctx.hq_offset:.2f}s")

        t = time.monotonic()
        ui.set_stage("encode", StageState.RUNNING)
        single = encode_loop(ctx)
        timings["encode"] = time.monotonic() - t
        ui.set_stage("encode", StageState.DONE)

        t = time.monotonic()
        ui.set_stage("verify", StageState.RUNNING)
        seam = verify_seam(ctx, single)
        timings["verify"] = time.monotonic() - t
        if seam.seamless:
            ui.set_stage("verify", StageState.DONE, detail=f"z={seam.z:.2f}")
        else:
            ui.set_stage("verify", StageState.WARN, detail=f"z={seam.z:.2f} NOT SEAMLESS")

        final_verdict = _adjust_verdict(ctx.loop.verdict, seam)

        ui.set_stage("done", StageState.RUNNING)
        out_path = finalize(ctx, single)
        ui.set_stage("done", StageState.DONE)
        ui.stop()

        payload = _build_json_payload(ctx, timings, seam=seam, output_path=out_path, status="ok",
                                       verdict=final_verdict)
        if not seam.seamless:
            payload.setdefault("warnings", []).append(
                "Seam not verified as seamless; consider retrying with --frames N+-1 or --start."
            )
        _emit_result(ctx, payload)
        _print_human_summary(ctx, payload)
        _cleanup(ctx)
        return 0

    except StageError as e:
        try:
            ui.set_stage(e.stage, StageState.FAILED, detail=e.message)
            ui.stop()
        except Exception:
            pass
        payload = {"status": "error", "stage": e.stage, "code": e.code, "message": e.message}
        if e.hint:
            payload["hint"] = e.hint
        if e.log_path:
            payload["log_path"] = e.log_path
        _emit_result(ctx, payload, error=True)
        ctx.keep_workdir_due_to_failure = True
        _cleanup(ctx)
        return e.code
    except KeyboardInterrupt:
        try:
            ui.stop()
        except Exception:
            pass
        sys.stderr.write("\nInterrupted.\n")
        _cleanup(ctx)
        return 130
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover - last-resort guard, never a silent crash
        try:
            ui.stop()
        except Exception:
            pass
        payload = {"status": "error", "stage": "unknown", "code": 6, "message": str(e)}
        _emit_result(ctx, payload, error=True)
        ctx.keep_workdir_due_to_failure = True
        _cleanup(ctx)
        if ctx.args.verbose:
            raise
        return 6


def _mark_skipped_stages(ctx):
    args = ctx.args
    if args.local_file:
        ctx.ui.set_stage("lowres", StageState.SKIPPED)
    if args.frames is not None:
        ctx.ui.set_stage("detect", StageState.SKIPPED)
        ctx.ui.set_stage("refine", StageState.SKIPPED)
    elif args.no_refine:
        ctx.ui.set_stage("refine", StageState.SKIPPED)
    if args.detect_only:
        for k in ("hq", "align", "encode", "verify"):
            ctx.ui.set_stage(k, StageState.SKIPPED)


def _run_detection(ctx, timings):
    """Runs the detect/refine stages according to --frames / --period / auto-detect."""
    args = ctx.args
    ui = ctx.ui

    fps_hq = ctx.chosen_fmt.get("fps") if ctx.chosen_fmt else ctx.info["fps"]
    fps_hq = fps_hq or ctx.info["fps"] or 30.0

    if args.frames is not None:
        start_s = _resolve_start(ctx)
        ctx.loop = LoopResult(frames=args.frames, fps=fps_hq, period_s=args.frames / fps_hq,
                               start_s=start_s, confidence=1.0, verdict="USER")
        ctx.coarse = CoarseResult(period_lag=args.frames, period_s=ctx.loop.period_s, confidence=1.0,
                                   verdict="USER", candidates=[], loop_start_s=start_s, curve=None)
        return

    if args.period is not None:
        start_s = _resolve_start(ctx)
        ctx.coarse = CoarseResult(period_lag=round(args.period * args.analysis_fps), period_s=args.period,
                                   confidence=1.0, verdict="USER", candidates=[], loop_start_s=start_s, curve=None)
        ui.set_stage("detect", StageState.DONE, detail=f"{args.period:.2f}s period (user-specified)")
        timings["detect"] = 0.0
        if args.no_refine:
            N = round(args.period * fps_hq)
            ctx.loop = LoopResult(frames=int(N), fps=fps_hq, period_s=N / fps_hq, start_s=start_s,
                                   confidence=1.0, verdict="USER")
            ui.set_stage("refine", StageState.SKIPPED)
        else:
            t = time.monotonic()
            ui.set_stage("refine", StageState.RUNNING)
            ctx.loop = refine_period(ctx)
            timings["refine"] = time.monotonic() - t
            ui.set_stage("refine", StageState.DONE, detail=f"{ctx.loop.frames} frames")
        return

    t = time.monotonic()
    ui.set_stage("detect", StageState.RUNNING)
    expect_frames = int((ctx.info["duration"] or 0) * args.analysis_fps) or None
    F = extract_fingerprints(ctx, ctx.paths["lowres"], fps=args.analysis_fps, size=(32, 18),
                              expect_frames=expect_frames, stage="detect", task_label="Analyzing frames")
    ctx.coarse = detect_period(ctx, F)
    timings["detect"] = time.monotonic() - t
    ui.set_stage("detect", StageState.DONE,
                  detail=f"{ctx.coarse.period_s:.2f}s period, {ctx.coarse.verdict}")

    if args.no_refine:
        ui.set_stage("refine", StageState.SKIPPED)
        N = round(ctx.coarse.period_s * fps_hq)
        ctx.loop = LoopResult(frames=int(N), fps=fps_hq, period_s=N / fps_hq, start_s=ctx.coarse.loop_start_s,
                               confidence=ctx.coarse.confidence, verdict=ctx.coarse.verdict)
    else:
        t = time.monotonic()
        ui.set_stage("refine", StageState.RUNNING)
        ctx.loop = refine_period(ctx)
        timings["refine"] = time.monotonic() - t
        ui.set_stage("refine", StageState.DONE, detail=f"{ctx.loop.frames} frames")


if __name__ == "__main__":
    sys.exit(main())
