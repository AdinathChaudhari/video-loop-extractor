#!/usr/bin/env bash
# Regression harness for video_loop_extractor.py's loop detection.
#
# Usage: run_fixtures.sh <path-to-extractor-script> [output_json_path]
#
# Runs `--local-file <fixture> --detect-only --json -y` against each synthetic
# fixture in fixtures/ and records verdict/period_s/confidence/exit_code per
# fixture as one JSON line each on stdout (and, if given, appended/written to
# output_json_path). Never touches the network -- --local-file + --detect-only
# skip yt-dlp/HQ/encode entirely (see CLAUDE.md "Testing hooks").
set -u -o pipefail

SCRIPT_PATH="${1:?usage: run_fixtures.sh <extractor-script-path> [output_json_path]}"
OUT_JSON="${2:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="$HERE/fixtures"

# name -> fixture file -> expectation label (informational only, not enforced here)
declare -a FIXTURES=(
  "fine_motion_loop:${FIXTURES_DIR}/fine_motion_loop.mp4:expect period_s~8.0 (currently the known bug -- may report STATIC/NONE)"
  "normal_loop:${FIXTURES_DIR}/normal_loop.mp4:expect period_s~12.0"
  "static:${FIXTURES_DIR}/static.mp4:expect verdict STATIC"
  "non_periodic:${FIXTURES_DIR}/non_periodic.mp4:expect verdict NONE"
)

if [[ -n "$OUT_JSON" ]]; then
  : > "$OUT_JSON"
fi

overall_status=0

for entry in "${FIXTURES[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  fixture_path="${rest%%:*}"
  note="${rest#*:}"

  if [[ ! -f "$fixture_path" ]]; then
    echo "{\"fixture\": \"$name\", \"error\": \"fixture file not found: $fixture_path\"}" | tee -a ${OUT_JSON:-/dev/null}
    overall_status=1
    continue
  fi

  raw_out="$(python3 "$SCRIPT_PATH" --local-file "$fixture_path" --detect-only --json -y 2>/tmp/run_fixtures_stderr_${name}.log)"
  exit_code=$?

  # --json contract: single result object on stdout, everything else on stderr.
  # Take the last non-empty stdout line as the JSON result line.
  json_line="$(printf '%s\n' "$raw_out" | grep -v '^[[:space:]]*$' | tail -n1)"

  result="$(python3 - "$name" "$fixture_path" "$exit_code" "$note" "$json_line" <<'PYEOF'
import json, sys

name, fixture_path, exit_code, note, json_line = sys.argv[1:6]

record = {
    "fixture": name,
    "fixture_path": fixture_path,
    "exit_code": int(exit_code),
    "note": note,
}

try:
    payload = json.loads(json_line) if json_line.strip() else {}
except json.JSONDecodeError as e:
    record["parse_error"] = str(e)
    record["raw_stdout_tail"] = json_line[-2000:]
    print(json.dumps(record))
    sys.exit(0)

loop = payload.get("loop") or {}
record["status"] = payload.get("status")
record["verdict"] = loop.get("verdict")
record["period_s"] = loop.get("period_s")
record["confidence"] = loop.get("confidence")
record["frames"] = loop.get("frames")
if payload.get("status") == "error":
    record["error_message"] = payload.get("message")
    record["error_stage"] = payload.get("stage")

print(json.dumps(record))
PYEOF
)"

  echo "$result"
  if [[ -n "$OUT_JSON" ]]; then
    echo "$result" >> "$OUT_JSON"
  fi
done

exit "$overall_status"
