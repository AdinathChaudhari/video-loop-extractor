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
  the full network E2E test; assert `loop.frames == 720` and `loop.period_s == 12.0` (frame count
  is the load-bearing invariant — see frame-count-is-truth above) and `seam.seamless == true`.
  **Verdict/analysis_height caveat (post size-budgeted analysis picker):** historically this
  scored MEDIUM at 144p (confidence ~0.75) because the near-static scene's low frame-to-frame
  contrast was further smoothed by 144p quantization. Since the size-budgeted analysis picker
  (`--analysis-height`/`--analysis-budget-mb`, see fine-motion detection below) can select a
  taller format when one fits the byte budget, `detection.analysis_height` and the verdict for
  this specific video are a function of *this video's current yt-dlp format list* (filesizes,
  not just nominal resolution) at the time of the test run, not a fixed constant — re-verify both
  numbers against a fresh `yt-dlp -j` before treating a deviation as a regression, rather than
  assuming they must still read 144p/MEDIUM.
- **Fine-motion loop detection (size-budgeted analysis download + escalation ladder).** Small,
  low-amplitude motion used to be invisible to detection: the old fixed worst-format (~144p)
  selector plus the 32×18/8-bit fingerprint grid quantized subtle motion away before
  autocorrelation ever saw it. Two independent, layered fixes:
  - `select_analysis_format`/`_analysis_selector_string` (§5.1) replace the fixed worst-format
    selector with a picker that takes the *tallest* format whose estimated whole-file size
    (`filesize`/`filesize_approx`/`tbr*duration`) fits `--analysis-budget-mb` (default 300 MB),
    soft-capped by `--analysis-height` (default 480; `0` = tallest under budget). This still
    obeys the whole-timeline rule — the budget picks **which resolution**, never a time window —
    and an unknown-size format is always skipped (never selected), so a metadata-sparse source
    falls back to the exact historical worst-format string.
  - `detect_period` runs a byte-identical pass-0 coarse detection first (today's exact 32×18/8-bit
    grid, fixed thresholds) and returns immediately on any positive verdict (HIGH/MEDIUM/LOW) —
    zero added cost on the common path. Only a STATIC/NONE pass-0 triggers the escalation ladder:
    the pre-existing sub-second rescue, then a 64×36/`gray16le`/`area`-scaled re-fingerprint of
    the *same* downloaded file (finer cells + 16-bit precision recover sub-8-bit-LSB motion that
    8-bit quantization erased), then a signed frame-to-frame motion-energy signal
    (`temporal_difference_fingerprints`) on that same array (pure numpy, no extra ffmpeg/network),
    then — network only, last resort — a re-download at `--analysis-max-height` (default 1080)
    strictly taller than what pass-0 got. Each escalation pass uses relaxed low-contrast
    thresholds (`_LOW_CONTRAST_PRESET`: `z_strong=2.5`/`c_strong=0.20`/`z_prov=2.2`/`c_prov=0.10`)
    guarded by `strict_fundamental=True` (refuses to invent a fundamental without harmonic-comb
    support at ≥60% of its multiples, so non-periodic footage still verdicts NONE) and is capped
    at MEDIUM (fine-motion recoveries are legitimately lower-confidence than a clean pass-0 HIGH).
    The thresholds are this low deliberately: at low `--analysis-fps` the escalation signal has
    very few effective samples (a 72s clip at `--analysis-fps 1` is only 72 rows, and for the
    signed motion-energy signal roughly half of those rows can be exact-zero when the source's
    discrete transitions don't land on the sampling grid), which pins genuine periodic peaks'
    Z-scores in the 2.5–3.0 band even when their contrast is already well past the pass-0 bar —
    the fixture regression sweep (`static.mp4`/`non_periodic.mp4` stay STATIC/NONE across
    `--analysis-fps` 1–15) is the evidence this doesn't manufacture false loops. **Win rule
    (load-bearing):** an escalation result replaces the pass-0 result only if its own verdict is
    positive; if every escalation pass stays non-positive, the *original* pass-0 result is
    returned unchanged — escalation can only add detections, never turn a STATIC into NONE or a
    NONE into a spurious loop. `--no-escalate` disables the entire ladder (including the
    pre-existing sub-second rescue) for an exact single-coarse-pass comparison.
  - **`refine_period` must match the escalation's fingerprint precision, or it silently undoes
    the win rule.** `refine_period` always runs after `detect_period` (even under
    `--detect-only`) and can downgrade `coarse.verdict` by one notch whenever its own
    offset-search alignment score `S_best < 0.985`. Its historical fingerprint grid
    (48×27/8-bit/`fast_bilinear`) has exactly the same sub-8-bit-LSB quantization loss that
    made pass-0 miss fine motion in the first place, so re-deriving refine fingerprints at that
    grid for an escalation-sourced coarse result drives `S_best` down (measured ~0.77 on the
    fine-motion fixture) and downgrades a correctly-recovered LOW/MEDIUM verdict — the *exact*
    mechanism behind the "escalation ran but the payload came back NONE/inconsistent" failure
    mode. `_refine_precision(coarse)` fixes this at the root: when
    `coarse.escalation` is set, `refine_period` fingerprints at the same 64×36/`gray16le`/`area`
    precision the ladder used to find the period (measured ~0.99999 `S_best` on the same
    fixture), instead of relaxing the `0.985` gate itself. Ordinary (`escalation=None`) coarse
    results keep the exact historical 48×27/8-bit/`gray`/`fast_bilinear` refine fingerprint —
    byte-identical, no regression risk for loops that already detect at pass-0.
  - **Pass E writes to a distinct filename (`lowres_hi.*`), never reusing pass-0's
    `lowres.*`.** Reusing the same output template is unreliable: yt-dlp skips re-downloading a
    complete file of that name (no `--force-overwrites` is set anywhere), so a same-container
    Pass E re-download silently no-ops, and when the container differs the pass-0 and Pass E
    files coexist under one `lowres.*` glob where `sorted(...)[0]` can select the stale,
    alphabetically-first pass-0 file — misreporting `detection.analysis_height` and feeding F3
    from the wrong file. A distinct prefix sidesteps both failure modes with no new yt-dlp flags.
