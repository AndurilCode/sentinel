# Sentinel

Local LLM rule evaluator for Claude Code hooks.  
Runs a small OSS model (via Ollama) as a pre-tool-use gate that evaluates repository rules against agent actions. Only violations surface — everything else is silent.

## Architecture

```
Claude Code                          Sentinel                          Ollama
    │                                    │                                │
    │  PreToolUse event (stdin JSON)     │                                │
    ├───────────────────────────────────>│                                │
    │                                    │  1. Parse event                │
    │                                    │  2. Determine trigger type     │
    │                                    │     (file_write|bash|mcp)      │
    │                                    │  3. Glob-filter matching rules │
    │                                    │  4. Skip if zero rules match   │
    │                                    │                                │
    │                                    │  5. Parallel evaluation ──────>│
    │                                    │     (one call per rule)        │
    │                                    │     ┌─ rule A ───> qwen3.5:4b │
    │                                    │     ├─ rule B ───> qwen3.5:4b │
    │                                    │     └─ rule C ───> qwen3.5:9b │ (per-rule override)
    │                                    │                                │
    │                                    │  6. Collect results            │
    │                                    │     violations only            │
    │                                    │                                │
    │  exit 0  (silent, all clear)      │                                │
    │<──────────────────────────────────│                                │
    │                                    │                                │
    │  exit 2 + stderr  (blocked)       │                                │
    │<──────────────────────────────────│                                │
```

## Design decisions

**Single-rule evaluation loop.** Small models (3-4B) can't reliably follow 20 rules simultaneously from a system prompt. But they can reliably do binary classification on one rule with constrained JSON output. Sentinel decomposes multi-rule evaluation into N independent, parallel, single-rule calls. This converts a hard instruction-following problem into a trivially easy classification problem.

**Scope-first filtering.** Rules declare glob patterns for when they apply. A rule scoped to `src/core/billing/**` is never evaluated when the agent writes to `README.md`. Zero LLM calls for irrelevant rules. This is context precision applied to the agent harness layer.

**Three trigger dimensions.** Agent actions map to three distinct evaluation patterns:
- `file_write` — scope globs match file paths
- `bash` — scope patterns match command strings  
- `mcp` — scope patterns match `server:tool` composites

Each dimension has different risk profiles and different contextual signals.

**Silent on pass.** Sentinel only produces output on violations. No noise, no "all rules passed" spam. The agent doesn't even know Sentinel exists unless it tries something that violates a rule.

**Fail open by default.** If Ollama is down or a rule evaluation errors, Sentinel allows the action. Productivity > paranoia. Set `fail_open: false` for high-stakes environments.

**Parallel execution.** All matching rules evaluate concurrently via ThreadPoolExecutor. 4 rules × ~50ms each (no_think mode) = ~50ms wall clock, not 200ms.

**JSONL telemetry.** Every evaluation writes a structured log line. Feed into Vigil for precision/recall tracking, false positive analysis, and rule tuning.

## Installation

```bash
# 1. Install Ollama and pull a model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:4b

# 2. Install PyYAML
pip install pyyaml

# 3. Install the Sentinel plugin
claude plugin install sentinel
```

### Initialize in your repository

```
# Run inside your project
/sentinel-init
```

This creates `.claude/sentinel/config.yaml` and `.claude/sentinel/rules/` in your repo. Then use `/sentinel-rule` to create rules.

### Skills

| Command | Description |
|---|---|
| `/sentinel-init` | Scaffold config and rules directory in your repo |
| `/sentinel-rule` | Create a new rule through guided conversation |
| `/sentinel-config` | View or update configuration |

## Plugin layout

```
sentinel/                          # Plugin (installed by Claude Code)
├── .claude-plugin/plugin.json     # Plugin manifest
├── hooks/hooks.json               # PreToolUse hook registration
├── sentinel.py                    # Evaluator script
├── examples/                      # Reference rules
│   └── *.yaml
└── skills/                        # Slash commands
    ├── sentinel-init/SKILL.md     # /sentinel-init
    ├── sentinel-rule/SKILL.md     # /sentinel-rule
    └── sentinel-config/SKILL.md   # /sentinel-config
```

## Repository layout (your repo)

```
your-repo/
└── .claude/
    └── sentinel/
        ├── config.yaml            # Your configuration
        ├── sentinel.log           # Telemetry (auto-created)
        └── rules/
            └── *.yaml             # Your rules
```

## Rule format

```yaml
id: rule-name                    # unique identifier (defaults to filename stem)
trigger: file_write              # file_write | bash | mcp | any
severity: block                  # block (exit 2) | warn (exit 0 + message)
scope:                           # glob patterns — rule fires if any match
  - "src/core/billing/**"
  - "**/payments/*.ts"
exclude:                         # glob patterns — exempt even if scope matches
  - "**/*.test.ts"
model: "qwen3.5:9b"             # optional per-rule model override
prompt: |                        # evaluation prompt with {{template_vars}}
  CONTEXT: {{action_summary}}
  FILE: {{file_path}}
  RULE: ...
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
```

### Template variables by trigger type

| Variable | `file_write` | `bash` | `mcp` |
|---|---|---|---|
| `{{file_path}}` | target path | — | — |
| `{{content_snippet}}` | first N chars | — | — |
| `{{content_length}}` | total chars | — | — |
| `{{command}}` | — | full command | — |
| `{{server_name}}` | — | — | MCP server |
| `{{mcp_tool}}` | — | — | MCP tool name |
| `{{mcp_arguments}}` | — | — | args JSON (truncated) |
| `{{action_summary}}` | all | all | all |
| `{{tool_name}}` | all | all | all |
| `{{trigger}}` | all | all | all |

### Scope matching by trigger type

| Trigger | Match target | Example scope |
|---|---|---|
| `file_write` | file path | `src/core/billing/**` |
| `bash` | command string | `git push --force*` |
| `mcp` | `server:tool`, `tool`, `server` | `postgres-prod:*` |

## Configuration reference

| Key | Default | Description |
|---|---|---|
| `model` | `qwen3.5:4b` | Ollama model for evaluation |
| `ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `timeout_ms` | `5000` | Per-rule evaluation timeout |
| `confidence_threshold` | `0.7` | Minimum confidence to count as violation |
| `max_parallel` | `4` | Concurrent Ollama calls |
| `think` | `false` | Enable /think mode (slower, more accurate) |
| `fail_open` | `true` | Skip rule on error vs block |
| `content_max_chars` | `800` | File content truncation in prompts |
| `log_file` | `null` | JSONL telemetry path |
| `rules_dir` | `rules` | Rules directory (relative to sentinel dir) |

## Telemetry format

Each evaluation appends one JSONL line:

```json
{
  "ts": "2026-03-30T14:22:01Z",
  "rule_id": "billing-protection",
  "trigger": "file_write",
  "target": "src/core/billing/invoice.ts",
  "violation": true,
  "confidence": 0.92,
  "reason": "File is in the protected billing directory",
  "elapsed_ms": 47,
  "model": "qwen3.5:4b"
}
```

Feed into Vigil, Langfuse, or any JSONL-compatible pipeline for:
- Per-rule precision/recall tracking
- False positive rate monitoring
- Latency percentile analysis
- Model comparison (A/B test different models per rule)

## Writing effective rules

**Keep prompts focused.** One rule = one concern. Don't combine "don't write to billing" and "don't hardcode secrets" in the same rule. Decomposition is the design, not a workaround.

**Use exclude patterns.** Test files, mocks, and examples rarely need protection. Excluding them reduces false positives and unnecessary LLM calls.

**Start with `severity: warn`.** New rules should warn first. Promote to `block` after you've verified precision via the telemetry log.

**Trust the glob, not the LLM, for deterministic checks.** If a rule is purely about file paths (e.g., "never write to /prod/"), you don't need an LLM — add it to your Claude Code permissions instead. Sentinel's value is for rules that need semantic evaluation: "does this content contain secrets?", "is this a destructive SQL operation?", "does this schema change need a migration?"

**Tune confidence_threshold per rule (future).** Currently global. The JSONL log will show you which rules produce low-confidence true positives, signaling where to adjust.
