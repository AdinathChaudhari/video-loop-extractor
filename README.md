# Video Loop Extractor

Given a video URL (any yt-dlp-supported site), this tool detects the length of the video's
repeating visual loop, downloads exactly one loop at the source's maximum available quality,
and encodes a single seamless-loop file — with a confidence verdict on how trustworthy the
detected loop is. The original use case: turn a long ambient/looping video (rain on a window,
a slow cityscape pan, a fireplace) into a small, clean, silent 4K/60fps clip suitable for a
looping wallpaper or a QuickTime background loop — no manual scrubbing for the cut point, no
guessing the period, no re-encoding more than once.

The pipeline: probe → full low-res analysis download → 1 fps perceptual-hash autocorrelation →
frame-exact refinement at HQ fps → stream-copy HQ segment download → alignment against the
low-res reference → single clean encode → seam verification.

---

## Requirements

```bash
brew install ffmpeg yt-dlp
```

Python 3.8+ in a virtualenv (recommended — see the PEP 668 note in Troubleshooting):

```bash
python3 -m venv ~/.venvs/main
~/.venvs/main/bin/pip install -r requirements.txt
```

`numpy` (required) and `rich` (optional, nicer progress UI) also auto-install on first run if
missing — the explicit `pip install -r requirements.txt` above just avoids the one-time delay
and any PEP 668 surprises on a bare Homebrew Python.

---

## Quick start

### Interactive

```bash
python video_loop_extractor.py
```

You'll be prompted for the URL, then (after the video is probed) resolution cap, codec, audio,
loop count, and save location — in that order. Defaults are sensible; hit Enter to accept.

### Fully non-interactive

```bash
python video_loop_extractor.py "https://youtu.be/LpC7_HQ4Jmg" -y --json -o ~/Movies
```

`-y` accepts every default; `--json` prints a single machine-readable result object on stdout
(everything else goes to stderr). This is the form to script or test against.

---

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `url` (positional) | — | Video URL. Omitted + TTY → interactive prompt; omitted + non-TTY → exit 2. |
| `-o, --output PATH` | `~/Movies` (else `~/Videos`, else `~`) | Output file or directory. |
| `--codec {hevc,h264}` | `hevc` | `hevc` → libx265 + `hvc1` tag, `.mov`. `h264` → libx264, `.mp4`. |
| `--crf INT` | `18` | Encode quality (0–51, lower = better). |
| `--preset STR` | `slow` | x264/x265 preset. |
| `--max-height INT` | `0` (source max) | Resolution cap. |
| `--loops INT` | `1` | Repetitions in the output file (copy-concat, no re-encode). |
| `--audio` | off | Include audio, trimmed to the loop with short fades. Default is silent (`-an`) — the wallpaper use case. |
| `--start TIME` | auto-detected | Force the extraction window start (`SS`, `MM:SS`, `HH:MM:SS`, fractional seconds ok). |
| `--period FLOAT` | auto-detected | Skip coarse detection; use this candidate period in seconds. |
| `--frames INT` | auto-detected | Skip *all* detection; extract exactly this many frames. Implies `--no-refine`. |
| `--no-refine` | off | Skip frame-exact refinement; round the coarse period to the nearest frame. |
| `--detect-only` | off | Stop after detection; print the verdict/period/frames, no download/encode. |
| `--analysis-fps FLOAT` | `1.0` | Sampling rate for coarse detection. |
| `--min-period FLOAT` | `2.0` | Lower bound of the lag search, seconds. |
| `--max-period FLOAT` | `0` (duration/2) | Upper bound of the lag search, seconds. |
| `--format STR` | auto | Raw yt-dlp format selector override for the HQ download (expert escape hatch). |
| `--cookies-from-browser STR` | none | Passed through to every yt-dlp call — for age-gated/members-only videos. |
| `--work-dir PATH` | fresh temp dir | Use this workspace instead of an auto-created one. |
| `--keep-temp` | off | Don't delete the workspace on exit. |
| `--local-file PATH` | none | Testing hook: treat PATH as the already-downloaded source, skipping all yt-dlp stages. |
| `-y, --yes` | off | Fully non-interactive: accept defaults for anything that would prompt. |
| `--json` | off | Machine-readable result object on stdout; all human output to stderr. |
| `-v, --verbose` | off | Log every subprocess argv and detection internals to stderr. |
| `-q, --quiet` | off | Plain single summary line instead of the fancy UI. Mutually exclusive with `-v`. |
| `--version` | | Print version, exit. |

---

## How it works

The progress UI walks through these stages, in order:

1. **Probe video metadata** — title, duration, resolution/fps, available formats.
2. **Download low-res analysis copy** — the *entire* video at the worst available quality, so the whole timeline (not just a sample window) can be analyzed.
3. **Detect loop period (autocorrelation)** — 1 fps perceptual-hash fingerprinting + autocorrelation finds the coarse repeating period.
4. **Frame-exact refinement** — re-samples around the candidate period at full HQ frame rate to pin the period down to an exact integer frame count.
5. **Download max-quality segment** — stream-copies (no re-encode) just the one loop's worth of source video at maximum quality.
6. **Align segment to source timeline** — the high-quality segment's absolute position is unknown (keyframe-cut downloads reset timestamps), so it's fingerprint-matched back against the low-res reference to recover it precisely.
7. **Encode seamless loop** — the single encode in the whole pipeline: exactly `N` frames, HEVC or H.264.
8. **Verify loop seam** — checks that the last frame flows into the first frame at least as well as adjacent frames flow into each other.
9. **Finalize** — applies `--loops` (copy-concat, no extra encode) and writes the output file.

## Confidence verdicts

| Verdict | Meaning | What to do |
|---|---|---|
| **HIGH** | Strong, unambiguous periodic signal | Proceed — no action needed. |
| **MEDIUM** | Clear period found, weaker margins | Proceeds automatically; spot-check the output. |
| **LOW** | Marginal signal (e.g. a slow ambient loop like rain or fog) | Interactive runs ask to confirm before spending time on the HQ download; `-y` proceeds with a warning. Watch the output for a seam. |
| **NONE** | No reliable repeating loop detected | The tool refuses and exits (code 5). If you know the true period, re-run with `--period SECONDS` or `--frames N`. |
| **STATIC** | Video is (near-)static — any cut "loops" trivially | Exits (code 5); override with `--period`/`--frames` if you actually want a clip from it. |

---

## Output formats

- **HEVC / `hvc1` `.mov`** (default) — hardware-decoded on Apple Silicon, smaller files at the same visual quality, HDR passthrough when the source is HDR. Best choice if you're staying on a Mac.
- **H.264 `.mp4`** (`--codec h264`) — maximum compatibility with non-Apple players and editors; HDR sources are tonemapped to SDR for this path.
- macOS has **no built-in custom-video-wallpaper feature** — the output file is meant for QuickTime Player's loop playback (⌘L) or a third-party wallpaper app (e.g. Wallpaper Engine-style tools, Plash, Lively). This tool only produces the clip; setting it as an actual desktop wallpaper is up to whatever wallpaper app you use.

### `--audio` limitations

Audio is cut to the *video's* detected period, not independently analyzed — if the soundtrack
loops on a different period than the visuals (common for ambient/music beds), the audio will
not be musically seamless even though the video is. Short fades at both ends mask the cut click
but do not fix the phrasing. With `--loops > 1`, the audio join may carry faint AAC
priming-sample clicks at each repeat boundary.

---

## Troubleshooting

**"Sign in to confirm your age" / members-only / private video**
Pass `--cookies-from-browser safari` (or `chrome`, `firefox`, etc.) so yt-dlp reuses your
browser's session cookies.

**"No reliable repeating loop detected"**
The video may not actually loop, may loop over more than half its own duration (undetectable —
fewer than 2 repetitions can't be measured), or may be too short/ambient for the default
thresholds. If you know the period, re-run with `--period SECONDS` (or `--frames N` to skip
detection entirely).

**`pip install` fails with an "externally-managed-environment" error (PEP 668)**
Your system Python refuses global installs. Use a venv:
```bash
python3 -m venv ~/.venvs/main
~/.venvs/main/bin/pip install -r requirements.txt
~/.venvs/main/bin/python video_loop_extractor.py ...
```

**A note on `--remote-components`**
When your installed yt-dlp supports it, this tool passes `--remote-components ejs:github` to
every yt-dlp call. Current YouTube extraction needs it — yt-dlp fetches and runs a small
JS-challenge component from GitHub at runtime to solve YouTube's player challenge. The flag is
only added when `yt-dlp --help` reports support for it; on older yt-dlp it is silently omitted.
If you'd rather not fetch remote components, use a yt-dlp build that doesn't require them (and
expect YouTube extraction to fail until yt-dlp is updated).

---

## License

MIT — see [LICENSE](LICENSE).
