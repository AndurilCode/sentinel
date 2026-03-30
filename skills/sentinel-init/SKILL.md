---
name: sentinel-init
description: Scaffold Sentinel config and rules directory in the current repository, installing prerequisites if needed
user-invocable: true
---

# Sentinel Init

Set up Sentinel in the current repository. Checks prerequisites, installs what's missing, and scaffolds the config directory.

## What to do

Run these steps in order. Stop and report if anything fails.

### Step 1: Check if already initialized

Check if `.claude/sentinel/` already exists in the current working directory. If it does, tell the user it's already initialized and stop.

### Step 2: Check and install Ollama

Run `which ollama` to check if Ollama is installed.

**If not installed**, detect the platform and install:
- **macOS**: `brew install ollama` (if brew is available), otherwise tell the user to download from https://ollama.com
- **Linux**: `curl -fsSL https://ollama.com/install.sh | sh`

### Step 3: Check if Ollama is running

Run `curl -s http://localhost:11434/api/tags` to check if Ollama is serving.

**If not responding**, start it:
- Run `ollama serve` in the background
- Wait a few seconds, then verify it's responding

If it still doesn't respond, tell the user to start Ollama manually and re-run `/sentinel-init`.

### Step 4: Check and pull the default model

Check if `gemma3:4b` is available by inspecting the response from `/api/tags`.

**If not available**, pull it:
- Run `ollama pull gemma3:4b`
- This downloads ~3 GB. Tell the user it's pulling and may take a few minutes.

### Step 5: Scaffold the config directory

Create `.claude/sentinel/config.yaml` with this content:

```yaml
# ─────────────────────────────────────────────────────────────
# Sentinel — configuration
# ─────────────────────────────────────────────────────────────

# Ollama model for rule evaluation.
# Small models work because each rule is evaluated independently
# as a binary classification task with constrained JSON output.
#
# Recommended (by hardware):
#   8 GB RAM   → gemma3:4b        (default, ~3 GB at Q4)
#   16 GB RAM  → gemma3:12b       (best accuracy for block rules)
#
# Any Ollama-compatible model works. Per-rule overrides via `model:` in rule files.
model: "gemma3:4b"

# Ollama endpoint
ollama_url: "http://localhost:11434"

# Per-rule evaluation timeout (ms). Rules that exceed this are skipped.
timeout_ms: 5000

# Minimum confidence to treat an LLM evaluation as a real violation.
# Below this threshold the violation is discarded (avoids false positives).
# Range: 0.0–1.0. Start conservative (0.7), tune down as you gain data.
confidence_threshold: 0.7

# Maximum concurrent Ollama calls.
# Each rule is one lightweight call (~100-150 output tokens).
# Set to match your available inference bandwidth.
max_parallel: 4

# Use thinking mode (slower, more accurate) or disable (fast gate).
# For hook evaluation, disabling thinking is almost always sufficient.
think: false

# Behavior when Ollama is unreachable or returns an error.
#   true  → skip the rule, allow the action (fail open)
#   false → block the action (fail closed, strict mode)
fail_open: true

# Truncation limit for file content included in prompts.
# Keeps token usage predictable. Rule prompts should rarely need
# more than a snippet — the evaluation is about the action, not the code.
content_max_chars: 800

# JSONL log file for evaluation telemetry.
# Each evaluation writes one line: rule_id, trigger, violation, confidence,
# elapsed_ms, model. Feed into Vigil or any observability pipeline.
# Set to null to disable logging.
log_file: ".claude/sentinel/sentinel.log"

# Rules directory (relative to this config's directory or absolute).
rules_dir: "rules"
```

### Step 6: Create the rules directory

Create `.claude/sentinel/rules/.gitkeep` (empty file) so the rules directory is tracked by git.

### Step 7: Verify end-to-end

Run a quick smoke test to confirm everything works:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}' | SENTINEL_CONFIG_DIR=.claude/sentinel python3 ${CLAUDE_PLUGIN_ROOT}/sentinel.py
```

Expected: exit 0, no output (no rules to match yet, so it passes through).

### Step 8: Done

Tell the user:

> Sentinel initialized at `.claude/sentinel/`. Ollama is running with `gemma3:4b`. Use `/sentinel-rule` to create your first rule.

Do NOT copy example rules into the repo. The rules directory starts empty.
