# Sentinel

Local LLM rule evaluator for Claude Code hooks.
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
3. Blocks on violations (exit 2), silent on pass (exit 0)

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

## Requirements

- Python 3
- [Ollama](https://ollama.com) with a pulled model (default: `qwen3.5:4b`)

PyYAML is auto-installed on first run if missing.
