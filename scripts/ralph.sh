#!/usr/bin/env bash
#
# Ralph Loop - Event-Driven AI Agent Loop
# https://github.com/Endogen/ralph-loop
#
set -euo pipefail

# File paths
PLAN_FILE="IMPLEMENTATION_PLAN.md"
LOG_DIR=".ralph"
LOG_FILE="$LOG_DIR/ralph.log"
NOTIFY_FILE="$LOG_DIR/pending-notification.txt"
ITERATIONS_FILE="$LOG_DIR/iterations.jsonl"
PID_FILE="$LOG_DIR/ralph.pid"
PAUSE_FILE="$LOG_DIR/pause"
INJECT_FILE="$LOG_DIR/inject.md"
CONFIG_FILE="$LOG_DIR/config.json"

# Defaults
DEFAULT_MAX_ITERS=20
DEFAULT_CLI="codex"

# Completion markers
PLANNING_DONE="STATUS: PLANNING_COMPLETE"
BUILDING_DONE="STATUS: COMPLETE"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
  cat << EOF_USAGE
Usage: $(basename "$0") [max_iterations]

Environment variables:
  RALPH_CLI    - CLI to use (codex, claude, opencode, goose) [default: codex]
  RALPH_FLAGS  - CLI flags [default: auto-detected per CLI]
  RALPH_TEST   - Test command to run after each iteration [optional]

Examples:
  ./ralph.sh 20                          # Run 20 iterations with Codex
  RALPH_CLI=claude ./ralph.sh 10         # Use Claude Code
  RALPH_TEST="pytest" ./ralph.sh         # Run pytest after each iteration
EOF_USAGE
  exit 1
}

log() {
  echo -e "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Send notification via OpenClaw cron + write details to file.
# The orchestrating agent (OpenClaw) will triage and decide whether to
# forward to human or attempt to help.
notify() {
  local prefix="$1"
  local message="$2"
  local details="${3:-}"
  local timestamp
  timestamp="$(date -Iseconds)"
  local project_dir
  project_dir="$(pwd)"
  local project_name
  project_name="$(basename "$project_dir")"

  cat > "$NOTIFY_FILE" << EOF_NOTIFY
{
  "timestamp": "$timestamp",
  "project": "$project_dir",
  "project_name": "$project_name",
  "prefix": "$prefix",
  "message": "$message",
  "details": "$details",
  "iteration": ${CURRENT_ITER:-0},
  "max_iterations": $MAX_ITERS,
  "cli": "$CLI",
  "log_tail": "$(tail -50 "$LOG_FILE" 2>/dev/null | base64 -w0)",
  "status": "pending"
}
EOF_NOTIFY

  log "📝 Notification written to $NOTIFY_FILE"

  if command -v openclaw &>/dev/null; then
    local event_text="[Ralph:${project_name}] ${prefix}: ${message}"
    if openclaw cron add \
      --name "ralph-${project_name}-notify" \
      --at "5s" \
      --session main \
      --system-event "$event_text" \
      --wake now \
      --delete-after-run >/dev/null 2>&1; then
      sed -i 's/"status": "pending"/"status": "delivered"/' "$NOTIFY_FILE" 2>/dev/null || true
      log "✅ OpenClaw notification scheduled"
    else
      log "⚠️ OpenClaw cron failed - notification saved to file for heartbeat pickup"
    fi
  else
    log "📋 openclaw not found - notification saved to $NOTIFY_FILE"
  fi
}

load_config_file() {
  CONFIG_CLI=""
  CONFIG_FLAGS=""
  CONFIG_MAX_ITERS=""
  CONFIG_TEST_CMD=""
  CONFIG_MODEL=""

  if [[ ! -f "$CONFIG_FILE" ]]; then
    return
  fi

  local config_values=()
  if ! mapfile -t config_values < <(python3 - "$CONFIG_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict):
    raise SystemExit(1)


def emit(value):
    if value is None:
        print("")
        return
    print(str(value))


emit(payload.get("cli", ""))
emit(payload.get("flags", ""))
emit(payload.get("max_iterations", ""))
emit(payload.get("test_command", ""))
emit(payload.get("model", ""))
PY
  ); then
    log "⚠️ Could not parse $CONFIG_FILE; continuing with defaults/environment"
    return
  fi

  CONFIG_CLI="${config_values[0]:-}"
  CONFIG_FLAGS="${config_values[1]:-}"
  CONFIG_MAX_ITERS="${config_values[2]:-}"
  CONFIG_TEST_CMD="${config_values[3]:-}"
  CONFIG_MODEL="${config_values[4]:-}"
}

snapshot_completed_tasks() {
  local source_file="$1"

  if [[ ! -f "$source_file" ]]; then
    return 0
  fi

  python3 - "$source_file" <<'PY'
import re
import sys

path = sys.argv[1]
pattern = re.compile(r"^\s*-\s*\[[xX]\]\s*([0-9]+(?:\.[0-9]+)*)\s*[\:\—\–\-]")

with open(path, encoding="utf-8") as handle:
    for line in handle:
        match = pattern.match(line)
        if match:
            print(match.group(1))
PY
}

extract_token_count() {
  local output="$1"
  local token_line

  token_line="$(printf '%s\n' "$output" | awk 'tolower($0) ~ /tokens used/ {getline; print; exit}')"
  token_line="$(printf '%s' "$token_line" | tr -d '[:space:],')"

  if [[ "$token_line" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    printf '%s' "$token_line"
  fi
}

json_array_from_values() {
  python3 - "$@" <<'PY'
import json
import sys

values = [value for value in sys.argv[1:] if value.strip()]
print(json.dumps(values))
PY
}

append_iteration_record() {
  local iteration="$1"
  local max_iterations="$2"
  local iter_start="$3"
  local iter_end="$4"
  local duration_seconds="$5"
  local tokens="$6"
  local status="$7"
  local tasks_json="$8"
  local commit_hash="$9"
  local commit_message="${10}"
  local test_passed="${11}"
  local test_output="${12}"
  local errors_json="${13}"

  ITERATION_NUMBER="$iteration" \
  ITERATION_MAX="$max_iterations" \
  ITERATION_START="$iter_start" \
  ITERATION_END="$iter_end" \
  ITERATION_DURATION="$duration_seconds" \
  ITERATION_TOKENS="$tokens" \
  ITERATION_STATUS="$status" \
  ITERATION_TASKS_JSON="$tasks_json" \
  ITERATION_COMMIT="$commit_hash" \
  ITERATION_COMMIT_MESSAGE="$commit_message" \
  ITERATION_TEST_PASSED="$test_passed" \
  ITERATION_TEST_OUTPUT="$test_output" \
  ITERATION_ERRORS_JSON="$errors_json" \
  ITERATION_FILE_PATH="$ITERATIONS_FILE" \
    python3 - <<'PY'
import json
import os


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


payload = {
    "iteration": int(os.environ["ITERATION_NUMBER"]),
    "max": int(os.environ["ITERATION_MAX"]),
    "start": os.environ["ITERATION_START"],
    "end": os.environ["ITERATION_END"],
    "duration_seconds": int(os.environ["ITERATION_DURATION"]),
    "tokens": parse_optional_float(os.environ.get("ITERATION_TOKENS")),
    "status": os.environ.get("ITERATION_STATUS") or None,
    "tasks_completed": json.loads(os.environ.get("ITERATION_TASKS_JSON", "[]")),
    "commit": os.environ.get("ITERATION_COMMIT") or None,
    "commit_message": os.environ.get("ITERATION_COMMIT_MESSAGE") or None,
    "test_passed": parse_optional_bool(os.environ.get("ITERATION_TEST_PASSED")),
    "test_output": os.environ.get("ITERATION_TEST_OUTPUT") or None,
    "errors": json.loads(os.environ.get("ITERATION_ERRORS_JSON", "[]")),
}

with open(os.environ["ITERATION_FILE_PATH"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
PY
}

process_injection_file() {
  if [[ ! -f "$INJECT_FILE" ]]; then
    return
  fi

  log "💉 Injecting instructions into AGENTS.md"
  {
    echo ""
    echo "## Injected Instructions ($(date '+%Y-%m-%d %H:%M'))"
    cat "$INJECT_FILE"
    echo ""
  } >> AGENTS.md
  rm -f "$INJECT_FILE"
}

wait_if_paused() {
  if [[ ! -f "$PAUSE_FILE" ]]; then
    return
  fi

  log "⏸️  Paused - waiting for resume..."
  notify "PROGRESS" "Loop paused at iteration ${CURRENT_ITER:-0}/$MAX_ITERS" "Waiting for .ralph/pause to be removed"

  while [[ -f "$PAUSE_FILE" ]]; do
    sleep 2
  done

  log "▶️  Resumed"
}

cleanup_pid() {
  rm -f "$PID_FILE"
}

handle_signal() {
  local signal_name="$1"
  log "🛑 Ralph loop stopped (signal: $signal_name)"
  notify "PROGRESS" "Loop stopped at iteration ${CURRENT_ITER:-0}/$MAX_ITERS" "Stopped by $signal_name"
  exit 0
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

# Setup
mkdir -p "$LOG_DIR"

CLI="$DEFAULT_CLI"
CLI_FLAGS=""
TEST_CMD=""
MAX_ITERS="$DEFAULT_MAX_ITERS"

load_config_file

if [[ -n "$CONFIG_CLI" ]]; then
  CLI="$CONFIG_CLI"
fi
if [[ -n "$CONFIG_FLAGS" ]]; then
  CLI_FLAGS="$CONFIG_FLAGS"
fi
if [[ -n "$CONFIG_MAX_ITERS" ]]; then
  MAX_ITERS="$CONFIG_MAX_ITERS"
fi
if [[ -n "$CONFIG_TEST_CMD" ]]; then
  TEST_CMD="$CONFIG_TEST_CMD"
fi
MODEL=""
if [[ -n "$CONFIG_MODEL" ]]; then
  MODEL="$CONFIG_MODEL"
fi

# Environment variables override config/defaults
if [[ -n "${RALPH_CLI:-}" ]]; then
  CLI="$RALPH_CLI"
fi
if [[ "${RALPH_FLAGS+x}" == "x" ]]; then
  CLI_FLAGS="$RALPH_FLAGS"
fi
if [[ "${RALPH_TEST+x}" == "x" ]]; then
  TEST_CMD="$RALPH_TEST"
fi
if [[ -n "${RALPH_MODEL:-}" ]]; then
  MODEL="$RALPH_MODEL"
fi

# Positional max_iterations has highest priority
if [[ -n "${1:-}" ]]; then
  MAX_ITERS="$1"
fi

if [[ -z "$CLI_FLAGS" ]]; then
  case "$CLI" in
    codex)
      CLI_FLAGS="-s workspace-write"
      ;;
    claude)
      CLI_FLAGS="--dangerously-skip-permissions"
      ;;
    *)
      CLI_FLAGS=""
      ;;
  esac
fi

if ! [[ "$MAX_ITERS" =~ ^[0-9]+$ ]] || ((MAX_ITERS < 1)) || ((MAX_ITERS > 999)); then
  echo -e "${RED}❌ Invalid max iterations: $MAX_ITERS (expected 1-999)${NC}"
  exit 1
fi

# Preflight checks
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo -e "${RED}❌ Must run inside a git repository${NC}"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo -e "${RED}❌ python3 is required${NC}"
  exit 1
fi

if ! command -v "$CLI" &>/dev/null; then
  echo -e "${RED}❌ CLI not found: $CLI${NC}"
  exit 1
fi

if [[ ! -f "PROMPT.md" ]]; then
  echo -e "${YELLOW}⚠️ PROMPT.md not found. Creating template...${NC}"
  cat > PROMPT.md << 'EOF_PROMPT'
# Ralph Loop

## Goal
[Describe what you want to build]

## Context
- Read: specs/*.md, IMPLEMENTATION_PLAN.md, AGENTS.md

## Notifications
When you need input or hit a blocker, write to .ralph/pending-notification.txt:
```bash
mkdir -p .ralph
cat > .ralph/pending-notification.txt << 'NOTIFY'
{"prefix":"ERROR","message":"Brief description","details":"Full context..."}
NOTIFY
```

Prefixes:
- DECISION: Need human input on a choice
- ERROR: Tests failing after retries
- BLOCKED: Missing dependency or unclear spec
- PROGRESS: Major milestone complete
- DONE: All tasks finished

## Completion
When finished, add to IMPLEMENTATION_PLAN.md: STATUS: COMPLETE
EOF_PROMPT
  echo -e "${BLUE}📝 Created PROMPT.md template. Edit it and run again.${NC}"
  exit 0
fi

touch AGENTS.md "$PLAN_FILE" "$ITERATIONS_FILE" 2>/dev/null || true

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo -e "${RED}❌ Ralph loop appears to already be running (PID $EXISTING_PID)${NC}"
    exit 1
  fi
  rm -f "$PID_FILE"
fi

echo $$ > "$PID_FILE"
trap cleanup_pid EXIT
trap 'handle_signal SIGTERM' TERM
trap 'handle_signal SIGINT' INT

# Clear any stale pending notification from previous run
[[ -f "$NOTIFY_FILE" ]] && rm -f "$NOTIFY_FILE"

# Resume iteration numbering from last JSONL entry
ITER_OFFSET=0
if [[ -f "$ITERATIONS_FILE" ]]; then
  ITER_OFFSET=$(python3 -c "
import json, sys
last = 0
for line in open('$ITERATIONS_FILE'):
    line = line.strip()
    if not line: continue
    try:
        last = max(last, json.loads(line).get('iteration', 0))
    except: pass
print(last)
" 2>/dev/null || echo 0)
fi

echo -e "${BLUE}🐺 Ralph Loop starting${NC}"
echo -e "   CLI: $CLI $CLI_FLAGS"
echo -e "   Max iterations: $MAX_ITERS"
echo -e "   Project: $(pwd)"
[[ -n "$TEST_CMD" ]] && echo -e "   Test command: $TEST_CMD"
[[ "$ITER_OFFSET" -gt 0 ]] && echo -e "   Resuming from iteration: $((ITER_OFFSET + 1))"
echo ""

# Main loop
for i in $(seq 1 "$MAX_ITERS"); do
  CURRENT_ITER=$((ITER_OFFSET + i))
  export CURRENT_ITER

  process_injection_file
  wait_if_paused

  ITER_START="$(date -Iseconds)"
  ITER_START_EPOCH="$(date +%s)"

  PLAN_BEFORE_FILE="$(mktemp)"
  PLAN_AFTER_FILE="$(mktemp)"
  snapshot_completed_tasks "$PLAN_FILE" > "$PLAN_BEFORE_FILE"

  HEAD_BEFORE="$(git rev-parse --short=7 HEAD 2>/dev/null || true)"

  log "${BLUE}=== Iteration $CURRENT_ITER (loop $i/$MAX_ITERS) ===${NC}"

  CLAUDE_JSON_MODE=false
  MODEL_FLAG=""
  if [[ -n "$MODEL" ]]; then
    MODEL_FLAG="--model $MODEL"
  fi

  case "$CLI" in
    codex)
      CMD="codex exec $CLI_FLAGS $MODEL_FLAG"
      ;;
    claude)
      CMD="claude --print --output-format json $CLI_FLAGS $MODEL_FLAG"
      CLAUDE_JSON_MODE=true
      ;;
    opencode)
      CMD="opencode run"
      ;;
    goose)
      CMD="goose run"
      ;;
    *)
      CMD="$CLI $CLI_FLAGS"
      ;;
  esac

  log "Running: $CMD \"...\""

  AGENT_OUTPUT=""
  AGENT_EXIT_CODE=0
  if AGENT_OUTPUT=$($CMD "$(cat PROMPT.md)" 2>&1); then
    AGENT_EXIT_CODE=0
  else
    AGENT_EXIT_CODE=$?
  fi

  # For Claude JSON mode: extract result text and token usage from JSON response
  CLAUDE_TOKENS=""
  if [[ "$CLAUDE_JSON_MODE" == "true" && -n "$AGENT_OUTPUT" ]]; then
    CLAUDE_RESULT="$(printf '%s' "$AGENT_OUTPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('result', ''))
except: print('')
" 2>/dev/null || true)"
    CLAUDE_TOKENS="$(printf '%s' "$AGENT_OUTPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    usage = data.get('usage', {})
    total = (usage.get('input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
             + usage.get('cache_read_input_tokens', 0) + usage.get('output_tokens', 0))
    print(f'{total / 1000:.3f}')
except: pass
" 2>/dev/null || true)"
    # Replace AGENT_OUTPUT with just the result text for logging
    if [[ -n "$CLAUDE_RESULT" ]]; then
      AGENT_OUTPUT="$CLAUDE_RESULT"
    fi
  fi

  if [[ -n "$AGENT_OUTPUT" ]]; then
    printf '%s\n' "$AGENT_OUTPUT" | tee -a "$LOG_FILE"
  fi

  # Append token info to log so the dashboard log parser can find it
  if [[ -n "$CLAUDE_TOKENS" ]]; then
    printf 'tokens used\n%s\n' "$CLAUDE_TOKENS" | tee -a "$LOG_FILE"
  fi

  TOKENS="$(extract_token_count "$AGENT_OUTPUT" || true)"
  # Use Claude JSON tokens if text extraction found nothing
  if [[ -z "$TOKENS" && -n "$CLAUDE_TOKENS" ]]; then
    TOKENS="$CLAUDE_TOKENS"
  fi

  TEST_PASSED="null"
  TEST_OUTPUT=""
  ITER_ERRORS=()

  if ((AGENT_EXIT_CODE != 0)); then
    ITER_ERRORS+=("Agent exited with code $AGENT_EXIT_CODE")
    log "${YELLOW}⚠️ Agent exited with code $AGENT_EXIT_CODE${NC}"
    notify "ERROR" "Agent crashed on iteration $CURRENT_ITER" "Exit code: $AGENT_EXIT_CODE. Check log for details."
  fi

  if [[ -n "$TEST_CMD" ]]; then
    log "Running tests: $TEST_CMD"

    TEST_RAW_OUTPUT=""
    if TEST_RAW_OUTPUT=$(bash -lc "$TEST_CMD" 2>&1); then
      TEST_PASSED="true"
      log "${GREEN}✅ Tests passed${NC}"
    else
      TEST_PASSED="false"
      ITER_ERRORS+=("Tests failed")
      log "${YELLOW}⚠️ Tests failed${NC}"
    fi

    if [[ -n "$TEST_RAW_OUTPUT" ]]; then
      printf '%s\n' "$TEST_RAW_OUTPUT" | tee -a "$LOG_FILE"
    fi

    TEST_OUTPUT="$(printf '%s\n' "$TEST_RAW_OUTPUT" | awk 'NF { line = $0 } END { print line }')"
  fi

  ITER_END="$(date -Iseconds)"
  ITER_END_EPOCH="$(date +%s)"
  ITER_DURATION=$((ITER_END_EPOCH - ITER_START_EPOCH))

  snapshot_completed_tasks "$PLAN_FILE" > "$PLAN_AFTER_FILE"
  mapfile -t NEW_TASKS < <(comm -13 <(sort -u "$PLAN_BEFORE_FILE") <(sort -u "$PLAN_AFTER_FILE") || true)
  rm -f "$PLAN_BEFORE_FILE" "$PLAN_AFTER_FILE"

  HEAD_AFTER="$(git rev-parse --short=7 HEAD 2>/dev/null || true)"
  COMMIT_HASH=""
  COMMIT_MESSAGE=""
  if [[ -n "$HEAD_AFTER" && "$HEAD_AFTER" != "$HEAD_BEFORE" ]]; then
    COMMIT_HASH="$HEAD_AFTER"
    COMMIT_MESSAGE="$(git log -1 --pretty=%s "$HEAD_AFTER" 2>/dev/null || true)"
  fi

  ITER_STATUS="success"
  if ((AGENT_EXIT_CODE != 0)) || [[ "$TEST_PASSED" == "false" ]]; then
    ITER_STATUS="error"
  fi

  TASKS_JSON="$(json_array_from_values "${NEW_TASKS[@]:-}")"
  ERRORS_JSON="$(json_array_from_values "${ITER_ERRORS[@]:-}")"

  append_iteration_record \
    "$CURRENT_ITER" \
    "$MAX_ITERS" \
    "$ITER_START" \
    "$ITER_END" \
    "$ITER_DURATION" \
    "$TOKENS" \
    "$ITER_STATUS" \
    "$TASKS_JSON" \
    "$COMMIT_HASH" \
    "$COMMIT_MESSAGE" \
    "$TEST_PASSED" \
    "$TEST_OUTPUT" \
    "$ERRORS_JSON"

  if grep -Fq "$BUILDING_DONE" "$PLAN_FILE" 2>/dev/null; then
    log "${GREEN}✅ All tasks complete!${NC}"
    notify "DONE" "All tasks complete" "Ralph loop finished successfully."
    exit 0
  fi

  if grep -Fq "$PLANNING_DONE" "$PLAN_FILE" 2>/dev/null; then
    log "${GREEN}📋 Planning phase complete${NC}"
    notify "PLANNING_COMPLETE" "Ready for BUILDING mode" "Switch PROMPT.md to PROMPT-BUILDING.md and restart."
    exit 0
  fi

  if ((AGENT_EXIT_CODE != 0)); then
    sleep 5
    continue
  fi

  sleep 2
  wait_if_paused
  process_injection_file
done

log "${RED}❌ Max iterations ($MAX_ITERS) reached${NC}"
notify "BLOCKED" "Max iterations reached" "Completed $MAX_ITERS iterations without finishing. Manual review needed."
exit 1
