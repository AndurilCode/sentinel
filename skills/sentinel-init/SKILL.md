---
name: sentinel-init
description: Scaffold Sentinel config and rules directory in the current repository
user-invocable: true
---

# Sentinel Init

Scaffold the Sentinel configuration directory in the current repository.

## What to do

1. Check if `.claude/sentinel/` already exists in the current working directory. If it does, tell the user it's already initialized and stop.

2. Create `.claude/sentinel/config.yaml` with this content:

```yaml
# ─────────────────────────────────────────────────────────────
# Sentinel — configuration
# ─────────────────────────────────────────────────────────────

# Ollama model for rule evaluation.
# Small models work because each rule is evaluated independently
# as a binary classification task with constrained JSON output.
#
# Recommended (by hardware):
#   8 GB RAM   → qwen3.5:4b       (dense, ~3 GB at Q4)
#   16 GB RAM  → qwen3.5-35b-a3b  (MoE, 3B active, ~12 GB at Q4)
#   32 GB RAM  → qwen3.5:9b       (dense, best sub-10B accuracy)
#
# Any Ollama-compatible model works. Per-rule overrides via `model:` in rule files.
model: "qwen3.5:4b"

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

# Use /think mode (slower, more accurate) or /no_think (fast gate).
# For hook evaluation, /no_think is almost always sufficient.
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

3. Create `.claude/sentinel/rules/.gitkeep` (empty file) so the rules directory is tracked by git.

4. Tell the user:

> Sentinel initialized at `.claude/sentinel/`. Use `/sentinel-rule` to create your first rule.

Do NOT copy example rules into the repo. The rules directory starts empty.
