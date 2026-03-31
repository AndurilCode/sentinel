# Sentinel Reference

## Architecture

```
Agent (Claude Code,                  Sentinel                          Ollama
 Copilot, Cursor,                        │                                │
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
    │                                    │     ┌─ rule A ───> gemma3:4b │
    │                                    │     ├─ rule B ───> gemma3:4b │
    │                                    │     └─ rule C ───> gemma3:12b │ (per-rule override)
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

**Fail open by default.** If Ollama is down or a rule evaluation errors, Sentinel allows the action. Set `fail_open: false` for strict mode.

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

**PostToolUse synthesized** — Add `post: true` to the rule. After the tool executes, `sentinel.py --post` reads the session context summary and the tool's output, calls Ollama with the rule's domain-knowledge prompt, and returns `additionalContext`. The expected prompt response format is `{"context": "your message (max 80 words)"}`.

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
| `model` | `gemma3:4b` | Ollama model for evaluation |
| `ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `timeout_ms` | `5000` | Per-rule evaluation timeout |
| `confidence_threshold` | `0.7` | Minimum confidence to count as violation |
| `max_parallel` | `4` | Concurrent Ollama calls |
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
| `context.model` | `gemma3:4b` | Ollama model for accumulator (can differ from judge model) |
| `context.min_events` | `3` | Minimum new events before accumulator updates the summary |
| `context.lock_timeout_s` | `30` | Max seconds to wait for GPU lock before skipping update |
| `context.summary_max_words` | `150` | Token budget for rolling session summary |

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
  "model": "gemma3:4b"
}
```

## Session context accumulator

`sentinel_context.py` maintains a rolling summary of the agent's session for use by PostToolUse `info` rules. It runs on the `Stop` hook, async and non-blocking, so it never delays the agent.

On each `Stop` event it reads new transcript entries since the last checkpoint, compacts them (stripping meta-tools and raw payloads), and calls Ollama to produce an updated summary. The summary is written to `.sentinel/sessions/<session_id>/summary.json` and consumed by `sentinel.py --post` when evaluating `post: true` info rules.

If the summary doesn't exist yet (early in a session), the synthesizer runs using the rule's domain knowledge alone.

### GPU coordination

Three consumers share a single flock-based lockfile (`.sentinel/sessions/<session_id>/ollama.lock`):

| Consumer | Priority | Lock behavior |
|---|---|---|
| Judge (`block`/`warn`) | P0 | Non-blocking try — proceeds regardless if locked |
| Synthesizer (`info post`) | P1 | Blocks up to 5 s, skips on timeout |
| Accumulator | P2 | Blocks up to 30 s, skips on timeout (catches up next Stop) |

The judge is on the critical path and must never wait. The synthesizer is advisory but synchronous — a brief wait is acceptable. The accumulator is async and eventually consistent.

## Writing effective rules

**One rule = one concern.** Don't combine unrelated checks. Decomposition is the design.

**Use exclude patterns.** Test files, mocks, and examples rarely need protection.

**Start with `severity: warn`.** Promote to `block` after verifying precision via telemetry.

**Trust the glob, not the LLM, for deterministic checks.** If a rule is purely about file paths, use Claude Code permissions instead. Sentinel's value is semantic evaluation: "does this contain secrets?", "is this destructive SQL?".

## Plugin layout

```
sentinel/                          # Plugin (installed by Claude Code)
├── .claude-plugin/plugin.json     # Plugin manifest
├── hooks/hooks.json               # PreToolUse hook registration
├── sentinel.py                    # Evaluator script
├── examples/                      # Reference rules
│   └── *.yaml
└── skills/                        # Slash commands
    ├── sentinel-init/SKILL.md
    ├── sentinel-rule/SKILL.md
    └── sentinel-config/SKILL.md
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

## Scribe — rule learning from observations

Scribe watches agent sessions and proposes new rules based on patterns it observes. It runs passively, collecting observations, and surfaces draft rules for human review.

### Scribe configuration

Add a `scribe` block to your `.claude/sentinel/config.yaml`:

| Key | Default | Description |
|---|---|---|
| `scribe.enabled` | `false` | Enable the Scribe observation pipeline |
| `scribe.model` | `gemma3:4b` | Ollama model used for observation analysis and draft generation |
| `scribe.observe_interval` | `5` | Number of tool events between observation snapshots |
| `scribe.max_drafts` | `20` | Maximum number of draft rules to keep before oldest are pruned |
| `scribe.auto_observe` | `true` | Automatically collect observations during sessions |
| `scribe.min_observations` | `3` | Minimum observations before a draft rule can be generated |

Example:

```yaml
scribe:
  enabled: true
  model: "gemma3:4b"
  observe_interval: 5
  max_drafts: 20
```

### Slash commands

#### `/sentinel-learn`

Start or stop the Scribe learning mode for the current session. When active, Scribe observes tool events and records patterns that might warrant new rules.

#### `/sentinel-drafts`

List all draft rules that Scribe has generated. Each draft shows the proposed rule ID, trigger type, scope, and the observations that motivated it. Drafts are stored in `.claude/sentinel/drafts/`.

#### `/sentinel-promote`

Promote a draft rule to a live rule. The draft is validated, moved from `.claude/sentinel/drafts/` into the active `rules/` directory, and becomes part of the evaluation pipeline immediately.

#### `/sentinel-dismiss`

Dismiss a draft rule that is not useful. The draft is removed from `.claude/sentinel/drafts/` so it no longer appears in the drafts list.

### File locations

| Path | Purpose |
|---|---|
| `.claude/sentinel/drafts/` | Draft rules proposed by Scribe, pending human review |
| `.sentinel/scribe/` | Internal Scribe state — observations, analysis cache, and session data |
