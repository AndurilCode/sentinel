---
name: sentinel-rule
description: Create or edit a Sentinel rule through guided conversation
user-invocable: true
---

# Sentinel Rule

Create a new Sentinel rule through guided conversation. Understand what the user wants to protect, ask clarifying questions, draft the rule, get approval, write it.

## Prerequisites

Check that `.claude/sentinel/rules/` exists. If not, tell the user to run `/sentinel-init` first and stop.

## Rule format reference

Rules are YAML files in `.claude/sentinel/rules/`. Each rule has these fields:

```yaml
id: rule-name                    # unique identifier (defaults to filename stem)
trigger: file_write              # file_write | bash | mcp | any
severity: block                  # block (deny) | warn (allow + warning) | info (context only)
scope:                           # glob patterns — rule fires if any match
  - "src/core/billing/**"
exclude:                         # glob patterns — exempt even if scope matches
  - "**/*.test.ts"
backend: "claude"               # optional per-rule backend override (ollama|claude|copilot)
model: "gemma3:12b"             # optional per-rule model override
post: true                       # (info only) opt-in to PostToolUse LLM synthesis
prompt: |                        # evaluation prompt with {{template_vars}}
  CONTEXT: {{action_summary}}
  RULE: ...
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
```

### Info severity

`severity: info` rules provide contextual advice to the agent without blocking or warning. Two modes:

**PreToolUse static** — the `prompt` field is rendered with template variables and returned directly as `additionalContext`. No LLM call, zero latency.

```yaml
id: payments-ownership
trigger: file_write
severity: info
scope:
  - "src/payments/**"
prompt: |
  This directory is owned by the Payments team.
  Changes require review from @payments-team.
  Slack: #payments-eng
```

**PostToolUse synthesized** — add `post: true` to opt the rule into PostToolUse evaluation. The LLM receives session context + tool output + domain knowledge and responds with `{"context": "..."}`.

```yaml
id: migration-awareness
trigger: file_write
severity: info
post: true
scope:
  - "**/migrations/**"
prompt: |
  DOMAIN KNOWLEDGE: This codebase uses a payments microservice.
  - OpenAPI spec at api/v2/openapi.yaml must reflect DB changes
  - CHANGELOG.md must be updated for any migration

  Based on the session context and the tool action,
  provide a brief, relevant contextual reminder.
  Respond with JSON: {"context": "your message (max 80 words)"}
```

New template variables available in PostToolUse rules:
- `{{tool_output}}` — the tool's response/result
- `{{session_context}}` — the accumulator's latest rolling summary of the session

### Trigger types and their template variables

**file_write** (Write, Edit, NotebookEdit):
- `{{file_path}}` — target file path
- `{{content_snippet}}` — first N chars of content
- `{{content_length}}` — total content length
- Scope globs match against the file path

**bash** (Bash):
- `{{command}}` — the full command string
- Scope globs match against the command string

**mcp** (MCP tools like `mcp__server__tool`):
- `{{server_name}}` — MCP server name
- `{{mcp_tool}}` — MCP tool name
- `{{mcp_arguments}}` — arguments JSON (truncated)
- Scope globs match against `server:tool`, `tool`, or `server`

**All triggers** also have:
- `{{action_summary}}` — human-readable action description
- `{{tool_name}}` — raw Claude Code tool name
- `{{trigger}}` — normalized trigger type

### What makes a good rule

1. **One rule = one concern.** Don't combine unrelated checks.
2. **Scope precisely.** Use glob patterns to limit when the rule fires. Narrow scope = fewer LLM calls, fewer false positives.
3. **Use exclude patterns.** Test files, mocks, and examples rarely need protection.
4. **Start with `severity: warn`.** Promote to `block` after verifying precision via telemetry.
5. **Keep prompts focused.** The LLM does binary classification on one rule. Give it clear context and a clear question.
6. **End every prompt with the correct response format.** For `block`/`warn` rules: `Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}`. For `info` rules with `post: true`: `Respond with JSON: {"context": "your message (max 80 words)"}`.
7. **Don't use LLM for deterministic checks.** If a rule is purely about file paths, use Claude Code permissions instead.
8. **Match model size to severity.** `block` rules prevent the agent from acting — accuracy matters more than speed. Use a larger model (e.g., `model: "gemma3:12b"`) for block rules that evaluate content semantically (secrets, PII, SQL safety). Leave `warn` rules and simple pattern rules on the default small model for speed.
9. **Use `info` for context, not enforcement.** Info rules provide knowledge that helps the agent make better decisions. Use for ownership info, related-file reminders, policy awareness. Add `post: true` when the advice needs to consider what the tool actually did.

### Examples

Read the example rules in `${CLAUDE_PLUGIN_ROOT}/examples/` for real-world patterns covering file_write, bash, and mcp triggers.

## Process

### Step 1: Understand the concern

Ask the user one question: **"What do you want to protect or prevent?"**

Listen for: what kind of action (file write, command, MCP call), what area of the codebase, what specific behavior to catch, and why it matters.

### Step 2: Clarify details

Ask 2-4 targeted follow-up questions, one at a time. Focus on:

- **Trigger type** — if not obvious from step 1. Offer: file_write, bash, mcp, or any.
- **Scope** — which file paths, commands, or MCP tools should this rule apply to? Suggest glob patterns based on the codebase structure.
- **Excludes** — any exceptions? (test files, specific directories, dry-run commands)
- **Severity** — should violations block the action, warn, or just provide context? Recommend starting with `warn`. Use `info` for ownership info, related-file reminders, or policy awareness that should never block.

Do not ask about prompt template details. Keep it focused. If the user picks `severity: block` and the rule evaluates content semantically (not just file paths), recommend either `model: "gemma3:12b"` (Ollama, ~2x latency) or `backend: "claude"` with `model: "haiku"` (cloud, higher accuracy) and explain the tradeoff.

### Step 3: Draft the rule

Generate the complete rule YAML. For the prompt field:
- Describe the context the LLM will see (using template variables)
- State the rule clearly in 2-3 sentences
- Ask a direct yes/no question about whether the action violates the rule
- End with the JSON response instruction

Present the full YAML to the user and ask: **"Does this look right?"**

### Step 4: Write the rule

On approval, write the rule to `.claude/sentinel/rules/<rule-id>.yaml`.

The `rule-id` should be a kebab-case slug derived from the rule's purpose (e.g., `billing-protection`, `no-force-push`, `mcp-prod-guard`).

If the user wants changes, revise and present again. Only write after explicit approval.

## Next steps

After the rule is written, suggest these to the user:

- `/sentinel-stats` — monitor this rule's performance after it fires (requires `log_file` in config)
- `/sentinel-rule` — create another rule to protect a different concern
- `/sentinel-config` — tune model, thresholds, or timeouts if needed
