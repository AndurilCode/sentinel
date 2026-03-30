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
severity: block                  # block (exit 2) | warn (exit 0 + warning)
scope:                           # glob patterns — rule fires if any match
  - "src/core/billing/**"
exclude:                         # glob patterns — exempt even if scope matches
  - "**/*.test.ts"
model: "gemma3:12b"             # optional per-rule model override
prompt: |                        # evaluation prompt with {{template_vars}}
  CONTEXT: {{action_summary}}
  RULE: ...
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
```

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
6. **End every prompt with:** `Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}`
7. **Don't use LLM for deterministic checks.** If a rule is purely about file paths, use Claude Code permissions instead.
8. **Match model size to severity.** `block` rules prevent the agent from acting — accuracy matters more than speed. Use a larger model (e.g., `model: "gemma3:12b"`) for block rules that evaluate content semantically (secrets, PII, SQL safety). Leave `warn` rules and simple pattern rules on the default small model for speed.

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
- **Severity** — should violations block the action or just warn? Recommend starting with `warn`.

Do not ask about prompt template details. Keep it focused. If the user picks `severity: block` and the rule evaluates content semantically (not just file paths), recommend adding `model: "gemma3:12b"` for better accuracy and explain the speed tradeoff (~2x latency).

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
