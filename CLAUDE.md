# Video Loop Extractor

Given a video URL, detects the length of its repeating visual loop, downloads exactly one loop
at the source's maximum quality, and encodes a single seamless-loop file with a confidence
verdict (HIGH/MEDIUM/LOW/NONE/STATIC).

## Stack

- Python 3.8+, single-file script (`from __future__ import annotations` at the top — the source
  uses PEP 585 annotations like `list[str]` but must still import on 3.8)
- `numpy` required (auto-installs on first run; hard-fails with a venv hint if uninstallable —
  PEP 668 on bare Homebrew Python); `rich` optional (missing → plain-text fallback UI)
- External: `yt-dlp`, `ffmpeg`, `ffprobe` (shelled out to; PATH binary preferred, falls back to
  `python -m yt_dlp`)

## Layout & entry point

- `video_loop_extractor.py` — the entire tool: CLI, probe, format selection, fingerprinting/
  detection, refinement, HQ download, alignment, encode, seam verify, progress UI

```bash
python video_loop_extractor.py "https://youtu.be/LpC7_HQ4Jmg" -y --json -o ~/Movies
```

## Testing hooks

- `--local-file PATH` — skip all yt-dlp stages entirely; treat PATH as the already-downloaded
  source (probe via ffprobe, alignment offset exact by construction). Use this for the synthetic
  fixture tests (seamless loop, non-periodic, static, harmonic/intro cases).
- `--detect-only` — run probe → detect → refine, print the verdict/period/frames, exit 0 without
  touching the network for HQ or doing any encode. Cheap way to sanity-check detection.
- `--json` — single result object on stdout, everything else on stderr; use this for asserting
  `loop.frames`, `loop.period_s`, `loop.confidence`/`verdict`, `seam.seamless`, etc. in tests.

## Playlists

- A playlist/channel URL is expanded (via `enumerate_playlist`, `yt-dlp -J --flat-playlist`) and
  every video is processed sequentially, each in its own `video-NNN/` workspace subdir. Pure
  playlist URLs expand by default; `watch?v=…&list=…` stays single unless `--yes-playlist`;
  `--no-playlist` forces single; `--max-videos N` caps the count. A cheap URL heuristic
  (`_url_maybe_playlist`) short-circuits the extra yt-dlp metadata call for plain video URLs.
- **Per-video pipeline is `_process_video(ctx, timings)` → `(code, payload)`** — it never raises
  for a stage failure (returns an error payload so a playlist keeps going). `_run_single` and
  `_run_playlist` own the Live lifecycle, emission, summaries, and cleanup.
- **One (non-nested) Live *session per video*** in playlist mode: `ui.reset()` → `ui.start()` →
  worker → `ui.stop()`. This respects the single-Live rule (never two active at once), it just
  runs sequential sessions. Shared prefs (codec/audio/loops/output dir) are gathered once via
  `_resolve_playlist_prefs`; per-video prompts are suppressed by `args.is_playlist_item`.
- Under `--json`, playlists emit one aggregate object (`{status, playlist, results:[…]}`), not
  one object per video.

## Conventions & gotchas

- **One-encode rule.** The single `ffmpeg` encode in the ENCODE stage is the *only* generation
  loss in the whole pipeline. The HQ segment is downloaded stream-copy only — never
  `--force-keyframes-at-cuts`, never `--recode-video`. `--loops` concat is `-c copy`. Any code
  path that introduces a second encode is a bug.
- **Whole-timeline rule.** Coarse detection always analyzes the *entire* video at low resolution
  — never a sample window — so loops of any length up to duration/2 are catchable.
- **Frame-count-is-truth rule.** The loop is an exact integer frame count `N` at the HQ source
  fps. The period in seconds (`N/fps`) is derived from `N`, never the other way around.
- **Absolute-time-only-in-lowres rule.** The full low-res download starts at source t=0, so its
  timeline *is* the source timeline. Every `--download-sections` clip has unknowable absolute
  alignment (cuts land on the keyframe at-or-before the requested start; PTS resets near 0) —
  never trust those timestamps for absolute position. Sections are only used for (a) *relative*
  frame-distance measurements (refine) or (b) content whose absolute position is recovered by
  fingerprint-matching against the lowres file (the align stage, before the HQ segment is
  encoded).
- **`--remote-components ejs:github` YouTube gotcha.** Current YouTube extraction needs this
  flag on every yt-dlp invocation (probed for support once at startup via `yt-dlp --help`) —
  same gotcha as the sibling `youtube-downloader` repo.
- **Sections are cut on keyframes by design.** Never "fix" this with
  `--force-keyframes-at-cuts` — that re-encodes and violates the one-encode rule. Refinement
  only needs relative frame distances, which keyframe-aligned cuts don't corrupt; absolute
  position is recovered separately by the align stage.
- **Refine-at-HQ-fps rule + rescale trap.** Refinement must sample at the HQ frame rate, not
  whatever low-res format falls out of a naive selector — YouTube's ≤480p formats cap at ≤30 fps
  even when the source is 60 fps, and refining below the true fps makes odd frame counts (e.g.
  719 @ 60) unrecoverable (±1 frame error → a visible seam). Rescaling a low-fps refine sample up
  to the HQ fps is a documented fallback only, not the primary path.
- **Rich single-Live rule.** Exactly one `rich.Live` for the whole run. `Progress` is used purely
  as a renderable inside it — never call `progress.start()`/`__enter__`/wrap it in its own Live
  (nested Lives crash). Any prompt while Live is active must `live.stop()` first and
  `live.start()` after, or the terminal garbles.
- **Temp-dir lifecycle.** Workspace is `tempfile.mkdtemp(prefix="video-loop-extractor-")` unless
  `--work-dir` is given. Cleaned up on exit unless `--keep-temp` or a stage failed (kept for
  inspection either way, with a printed path). SIGINT/SIGTERM kill registered child processes
  before cleanup.
- **Reference ground truth:** `https://youtu.be/LpC7_HQ4Jmg` — the proven end-to-end case, a
  3h47m 4K/60fps video whose visual loop is *exactly* 720 frames = 12.000 s @ 60 fps. Use it for
  the full network E2E test; assert `loop.frames == 720`, `loop.period_s == 12.0`, and
  `seam.seamless == true`. Verdict is **MEDIUM** here, not HIGH: the scene is near-static so its
  frame-to-frame contrast is low (confidence ~0.75), which is correct behavior — a low-contrast
  ambient loop legitimately scores MEDIUM even when the extracted loop is perfect. Don't "fix"
  detection to force HIGH on ambient scenes.
