# Sentinel Reference

## Architecture

```
Agent (Claude Code,                  Sentinel                     LLM Backend
 Copilot, Cursor,                        │                    (Ollama / Claude / Copilot)
 Windsurf, Cline,                        │                                │
 Amazon Q, ...)                          │                                │
    │                                    │                                │
    │  PreToolUse event (stdin JSON)     │                                │
    ├───────────────────────────────────>│                                │
    │                                    │  1. Parse event (via tool_map) │
    │                                    │  2. Determine trigger type     │
    │                                    │     (file_write|bash|mcp)      │
    │                                    │  3. Glob-filter matching rules │
    │                                    │  4. Skip if zero rules match   │
    │                                    │                                │
    │                                    │  5. Parallel evaluation ──────>│
    │                                    │     (one call per rule)        │
    │                                    │     ┌─ rule A ───> ollama     │
    │                                    │     ├─ rule B ───> ollama     │
    │                                    │     └─ rule C ───> claude     │ (per-rule override)
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

**Single-rule evaluation loop.** Small models (3-4B) can't reliably follow 20 rules simultaneously from a system prompt. But they can reliably do binary classification on one rule with constrained JSON output. Sentinel decomposes multi-rule evaluation into N independent, parallel, single-rule calls.

**Scope-first filtering.** Rules declare glob patterns for when they apply. A rule scoped to `src/core/billing/**` is never evaluated when the agent writes to `README.md`. Zero LLM calls for irrelevant rules.

**Three trigger dimensions.** Agent actions map to three evaluation patterns:
- `file_write` — scope globs match file paths
- `bash` — scope patterns match command strings
- `mcp` — scope patterns match `server:tool` composites

**Silent on pass.** Sentinel only produces output on violations. The agent doesn't know Sentinel exists unless it violates a rule.

**Pluggable LLM backends.** Sentinel supports Ollama (local HTTP), Claude Code CLI, and Copilot CLI as evaluation backends. Selectable globally via `backend:` or per-rule. Ollama uses GPU semaphore gating; CLI backends use subprocess calls.

**Fail open by default.** If the LLM backend is unreachable or a rule evaluation errors, Sentinel allows the action. Set `fail_open: false` for strict mode.

**Parallel execution.** All matching rules evaluate concurrently via ThreadPoolExecutor.

**JSONL telemetry.** Every evaluation writes a structured log line for observability.

## Rule format

```yaml
id: rule-name                    # unique identifier (defaults to filename stem)
trigger: file_write              # file_write | bash | mcp | any
severity: block                  # block (exit 2) | warn (exit 0 + message) | info (context only)
post: true                       # optional — info rules only; opt into PostToolUse synthesis
scope:                           # glob patterns — rule fires if any match
  - "src/core/billing/**"
  - "**/payments/*.ts"
exclude:                         # glob patterns — exempt even if scope matches
  - "**/*.test.ts"
backend: "claude"               # optional per-rule backend override (ollama|claude|copilot)
model: "gemma3:12b"             # optional per-rule model override
prompt: |                        # evaluation prompt with {{template_vars}}
  CONTEXT: {{action_summary}}
  FILE: {{file_path}}
  RULE: ...
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
```

### Info severity

`severity: info` rules provide contextual advice to the agent without blocking or warning. Two modes:

**PreToolUse static** — No LLM call. The `prompt` field is rendered with template variables and returned as `additionalContext`. Zero latency. Use for ownership notices, team contacts, or policy reminders tied to a path.

```yaml
id: payments-ownership
trigger: file_write
severity: info
scope:
  - "src/payments/**"
prompt: |
  This directory is owned by the Payments team.
  Changes require review from @payments-team. Slack: #payments-eng
```

**PostToolUse synthesized** — Add `post: true` to the rule. After the tool executes, `sentinel.py --post` reads the session context summary and the tool's output, calls the configured LLM backend with the rule's domain-knowledge prompt, and returns `additionalContext`. The expected prompt response format is `{"context": "your message (max 80 words)"}`.

```yaml
id: migration-awareness
trigger: file_write
severity: info
post: true
scope:
  - "**/migrations/**"
prompt: |
  DOMAIN KNOWLEDGE: OpenAPI spec at api/v2/openapi.yaml must reflect DB changes.
  CHANGELOG.md must be updated for any migration.

  Based on the session context and the tool action,
  provide a brief, relevant contextual reminder.
  Respond with JSON: {"context": "your message (max 80 words)"}
```

### Template variables by trigger type

| Variable | `file_write` | `bash` | `mcp` | Notes |
|---|---|---|---|---|
| `{{file_path}}` | target path | — | — | |
| `{{content_snippet}}` | first N chars | — | — | |
| `{{content_length}}` | total chars | — | — | |
| `{{command}}` | — | full command | — | |
| `{{server_name}}` | — | — | MCP server | |
| `{{mcp_tool}}` | — | — | MCP tool name | |
| `{{mcp_arguments}}` | — | — | args JSON (truncated) | |
| `{{action_summary}}` | all | all | all | |
| `{{tool_name}}` | all | all | all | |
| `{{trigger}}` | all | all | all | |
| `{{tool_output}}` | — | — | — | PostToolUse only (`post: true`) |
| `{{session_context}}` | — | — | — | PostToolUse only (`post: true`) |

### Scope matching by trigger type

| Trigger | Match target | Example scope |
|---|---|---|
| `file_write` | file path | `src/core/billing/**` |
| `bash` | command string | `git push --force*` |
| `mcp` | `server:tool`, `tool`, `server` | `postgres-prod:*` |

## Configuration reference

| Key | Default | Description |
|---|---|---|
| `backend` | `ollama` | LLM backend: `ollama`, `claude`, or `copilot` |
| `model` | `gemma3:4b` | Default model for evaluation (backend-specific) |
| `backends.ollama.url` | `http://localhost:11434` | Ollama endpoint |
| `backends.ollama.model` | *(top-level model)* | Default Ollama model |
| `backends.claude.model` | `haiku` | Default Claude model |
| `backends.copilot.model` | `gpt-5-mini` | Default Copilot model |
| `timeout_ms` | `5000` | Per-rule evaluation timeout |
| `confidence_threshold` | `0.7` | Minimum confidence to count as violation |
| `max_parallel` | `4` | Concurrent LLM calls |
| `ollama_concurrency` | `1` | Max concurrent Ollama HTTP calls (GPU-bound) |
| `think` | `false` | Enable thinking mode (slower, more accurate) |
| `fail_open` | `true` | Skip rule on error vs block |
| `content_max_chars` | `800` | File content truncation in prompts |
| `log_file` | `null` | JSONL telemetry path |
| `rules_dir` | `rules` | Rules directory (relative to config dir) |
| `tool_map` | *(see below)* | Tool name → trigger type mapping |
| `mcp_prefix` | `mcp__` | Prefix for detecting MCP tool names |
| `mcp_separator` | `__` | Separator for parsing MCP server/tool from tool name |
| `context.enabled` | `true` | Enable session context accumulator |
| `context.backend` | *(top-level backend)* | LLM backend for accumulator |
| `context.model` | `gemma3:4b` | Model for accumulator (can differ from judge model) |
| `context.min_events` | `3` | Minimum new events before accumulator updates the summary |
| `context.lock_timeout_s` | `30` | Max seconds to wait for GPU lock (Ollama only) |
| `context.summary_max_words` | `150` | Token budget for rolling session summary |

Backward compatibility: if the `backends` key is absent, `model` and `ollama_url` at the top level still work. If `backend` is absent, defaults to `ollama`.

### Multi-agent tool mapping

Sentinel ships with built-in tool name mappings for multiple coding agents. The default `tool_map` recognizes tool names from Claude Code, Copilot, Cursor, Windsurf, Cline, and Amazon Q:

| Agent | File write tools | Terminal tools |
|---|---|---|
| **Claude Code** | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `Bash` |
| **Copilot** (VS Code) | `create_file`, `replace_string_in_file`, `multi_replace_string_in_file` | `run_in_terminal` |
| **Cursor** | `edit_file` | `run_terminal_cmd` |
| **Windsurf** | `write_to_file`, `edit_file` | `run_command` |
| **Cline** | `write_to_file`, `replace_in_file` | `execute_command` |
| **Amazon Q** | `fs_write` | `execute_bash` |

MCP tool detection uses a configurable prefix and separator. Defaults match Claude Code (`mcp__server__tool`). For Cursor, set:

```yaml
mcp_prefix: "mcp_"
mcp_separator: "_"
```

To add custom tool names or override the defaults, provide a `tool_map` in your config:

```yaml
tool_map:
  my_custom_write_tool: file_write
  my_shell_tool: bash
```

Note: a custom `tool_map` **replaces** the defaults entirely. If you only need to add entries, copy the defaults from `sentinel.py` and append your additions.

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
  "model": "gemma3:4b",
  "backend": "ollama"
}
```

## Session context accumulator

`sentinel_context.py` maintains a rolling summary of the agent's session for use by PostToolUse `info` rules. It runs on the `Stop` hook, async and non-blocking, so it never delays the agent.

On each `Stop` event it reads new transcript entries since the last checkpoint, compacts them (stripping meta-tools and raw payloads), and calls the configured LLM backend to produce an updated summary. The summary is written to `.sentinel/sessions/<session_id>/summary.json` and consumed by `sentinel.py --post` when evaluating `post: true` info rules.

If the summary doesn't exist yet (early in a session), the synthesizer runs using the rule's domain knowledge alone.

### GPU coordination (Ollama only)

When using the Ollama backend, three consumers share a single flock-based lockfile (`.sentinel/sessions/<session_id>/ollama.lock`):

| Consumer | Priority | Lock behavior |
|---|---|---|
| Judge (`block`/`warn`) | P0 | Non-blocking try — proceeds regardless if locked |
| Synthesizer (`info post`) | P1 | Blocks up to 5 s, skips on timeout |
| Accumulator | P2 | Blocks up to 30 s, skips on timeout (catches up next Stop) |

The judge is on the critical path and must never wait. The synthesizer is advisory but synchronous — a brief wait is acceptable. The accumulator is async and eventually consistent.

Claude and Copilot backends skip lock acquisition entirely — they don't share a local GPU.

## Writing effective rules

**One rule = one concern.** Don't combine unrelated checks. Decomposition is the design.

**Use exclude patterns.** Test files, mocks, and examples rarely need protection.

**Start with `severity: warn`.** Promote to `block` after verifying precision via telemetry.

**Trust the glob, not the LLM, for deterministic checks.** If a rule is purely about file paths, use Claude Code permissions instead. Sentinel's value is semantic evaluation: "does this contain secrets?", "is this destructive SQL?".

## Plugin layout

```
sentinel/                          # Plugin (installed by Claude Code)
├── .claude-plugin/plugin.json     # Plugin manifest
├── hooks/hooks.json               # PreToolUse, PostToolUse, Stop hooks
├── sentinel.py                    # Rule evaluator (PreToolUse + PostToolUse)
├── sentinel_context.py            # Session context accumulator (Stop hook)
├── sentinel_scribe.py             # Convention extraction + draft rules (Stop hook + /sentinel-learn)
├── sentinel_lock.py               # GPU coordination lock
├── sentinel_log.py                # Shared JSONL logging
├── examples/                      # Reference rules
│   └── *.yaml
└── skills/                        # Slash commands
    ├── sentinel-init/SKILL.md
    ├── sentinel-rule/SKILL.md
    ├── sentinel-config/SKILL.md
    ├── sentinel-learn/SKILL.md
    ├── sentinel-drafts/SKILL.md
    ├── sentinel-promote/SKILL.md
    ├── sentinel-dismiss/SKILL.md
    └── sentinel-stats/SKILL.md
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

## Scribe — convention learning from sessions and documentation

Scribe analyzes agent sessions and documentation to extract conventions, then proposes draft Sentinel rules for human review. It runs at session end (`Stop` hook) and looks for two types of signals:

1. **Human-expressed rules** — the developer states a permanent convention ("never do X", "always do Y")
2. **Agent self-corrections** — the agent makes a mistake and corrects itself (tool error → fix, write → revise, test failure → code fix)

### How it works

At session end, Scribe reads the full compacted transcript and runs a two-phase pipeline:

1. **Extraction** — one LLM call classifies the session transcript for conventions
2. **Validation** — for each extracted convention, checks structural dedup (dismissed list, existing rules), then calls the LLM to judge semantic redundancy against active rules and generate a draft YAML if not redundant

Separately, `/sentinel-learn` scans documentation files (CLAUDE.md, ADRs, READMEs) for conventions using the same extraction → synthesis pipeline.

### Scribe configuration

| Key | Default | Description |
|---|---|---|
| `scribe.enabled` | `true` | Enable the Scribe pipeline |
| `scribe.model` | *(top-level model)* | Default Ollama model for all scribe steps |
| `scribe.extraction_model` | *(scribe.model)* | Override for extraction (reflect + learn) |
| `scribe.synthesis_model` | *(scribe.model)* | Override for validation + synthesis |
| `scribe.guidance` | `null` | Priority guidance text for extraction (e.g., "focus on security") |
| `scribe.think` | `false` | Enable /think mode for validation+synthesis |
| `scribe.extraction_timeout_ms` | `15000` | Timeout for extraction LLM calls |
| `scribe.extraction_num_predict` | `1000` | Max output tokens for extraction |
| `scribe.synthesis_timeout_ms` | `15000` | Timeout for validation+synthesis LLM calls |
| `scribe.synthesis_num_predict` | `1000` | Max output tokens for validation+synthesis |
| `scribe.temperature` | `0.1` | LLM temperature for all scribe calls |
| `scribe.transcript_budget_chars` | `4000` | Max compacted transcript size before truncation |
| `scribe.thresholds.extraction_confidence` | `0.7` | Minimum confidence to store an observation |
| `scribe.thresholds.draft_confidence` | `0.8` | Minimum confidence for learn mode draft generation |
| `scribe.sources.documentation` | `true` | Enable documentation scanning via `/sentinel-learn` |
| `scribe.doc_globs` | `[CLAUDE.md, AGENTS.md, README.md, docs/**/*.md, ADR*.md]` | File patterns for `/sentinel-learn` |
| `scribe.notification.max_age_days` | `7` | Max age for draft notifications |

Model resolution order: `scribe.<step>_model` → `scribe.model` → top-level `model` → `gemma3:4b`

Example:

```yaml
scribe:
  enabled: true
  extraction_model: "gemma3:4b"
  synthesis_model: "gemma3:12b"
  guidance: "focus on security boundaries and data access patterns"
```

### GPU coordination

Scribe uses priority P3 (lowest) for GPU lock acquisition:

| Consumer | Priority | Lock behavior |
|---|---|---|
| Judge (`block`/`warn`) | P0 | Non-blocking try — proceeds regardless |
| Synthesizer (`info post`) | P1 | Blocks up to 5 s |
| Accumulator | P2 | Blocks up to 30 s |
| Scribe | P3 | Blocks up to 10 s, skips on timeout |

### Slash commands

#### `/sentinel-learn`

Scan repository documentation files for conventions and generate draft rules. Scans files matching `scribe.doc_globs` in config.

#### `/sentinel-drafts`

List all pending draft rules. Each draft shows ID, trigger, scope, source (`user_feedback`, `agent_self_correction`, or `documentation`), age, and evidence.

#### `/sentinel-promote <id>`

Promote a draft rule to active. Moves the draft from `.claude/sentinel/drafts/` to `rules/` — it becomes part of the evaluation pipeline immediately.

#### `/sentinel-dismiss <id>`

Dismiss a draft rule. Removes the draft and adds it to the dismissed blocklist so it won't be re-proposed.

### File locations

| Path | Purpose |
|---|---|
| `.claude/sentinel/drafts/` | Draft rules proposed by Scribe, pending human review |
| `.sentinel/scribe/observations.jsonl` | Convention observations with provenance |
| `.sentinel/scribe/dismissed.jsonl` | Dismissed convention blocklist |
