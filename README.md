# Sentinel

Local LLM rule evaluator for coding agent hooks.
Gates agent actions against repo-defined rules using Ollama. Silent when everything passes, blocks on violations.

## Quick start

```bash
# Add the marketplace
/plugin marketplace add AndurilCode/sentinel

# Install the plugin
/plugin install sentinel@sentinel

# Initialize in your repo
/sentinel-init
```

`/sentinel-init` handles everything: installs Ollama if missing, pulls the model, starts the server, and scaffolds `.claude/sentinel/` in your repo.

## Create rules

```bash
/sentinel-rule
```

Walks you through creating a rule: what to protect, trigger type, scope, severity. Writes the YAML to `.claude/sentinel/rules/`.

## Commands

| Command | Description |
|---|---|
| `/sentinel-init` | Install prerequisites, scaffold config and rules |
| `/sentinel-rule` | Create a rule through guided conversation |
| `/sentinel-config` | View or update configuration |

## How it works

Sentinel runs as a `PreToolUse` hook. On every agent action:

1. Matches the action against rule scope globs (no LLM call if nothing matches)
2. Evaluates matching rules in parallel — one Ollama call per rule, binary classification
3. Blocks on violations (outputs `permissionDecision: deny`), silent on pass

Rules live in your repo at `.claude/sentinel/rules/*.yaml`. The plugin evaluator lives outside your repo.

## Example rule

```yaml
id: dangerous-commands
trigger: bash
severity: block
scope:
  - "git push --force*"
  - "*rm -rf*"
exclude:
  - "*--dry-run*"
prompt: |
  A coding agent is about to execute: {{command}}
  RULE: Force-pushing and recursive deletion are prohibited.
  Does this command violate the rule?
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
```

See `examples/` for more: file write guards, MCP production gates, secret detection.

## Supported agents

Sentinel recognizes tool names from multiple coding agents out of the box:

| Agent | File write | Terminal | MCP format |
|---|---|---|---|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `Bash` | `mcp__server__tool` |
| [GitHub Copilot](https://github.com/features/copilot) (VS Code) | `create_file`, `replace_string_in_file` | `run_in_terminal` | native tool names |
| [Cursor](https://cursor.sh) | `edit_file` | `run_terminal_cmd` | `mcp_server_tool` |
| [Windsurf](https://codeium.com/windsurf) | `write_to_file`, `edit_file` | `run_command` | native tool names |
| [Cline](https://github.com/cline/cline) | `write_to_file`, `replace_in_file` | `execute_command` | `use_mcp_tool` wrapper |
| [Amazon Q](https://aws.amazon.com/q/developer/) | `fs_write` | `execute_bash` | `@server/tool` |

To add a custom agent or override mappings, set `tool_map` in your `config.yaml`:

```yaml
tool_map:
  my_write_tool: file_write
  my_shell_tool: bash
```

For agents with different MCP naming conventions (e.g. Cursor), configure the prefix and separator:

```yaml
mcp_prefix: "mcp_"
mcp_separator: "_"
```

See [docs/reference.md](docs/reference.md) for the full configuration reference.

## Requirements

- Python 3
- [Ollama](https://ollama.com) with a pulled model (default: `gemma3:4b`)

PyYAML is auto-installed on first run if missing.
