---
name: sentinel-config
description: View or update Sentinel configuration
user-invocable: true
---

# Sentinel Config

View or update the Sentinel configuration in the current repository.

## What to do

1. Check that `.claude/sentinel/config.yaml` exists. If not, tell the user to run `/sentinel-init` first and stop.

2. Read `.claude/sentinel/config.yaml`.

3. If the user asked to view the config, show the current values and explain what each one does. Stop here.

4. If the user asked to change something, use the Edit tool to update the specific value(s) in the config file. Preserve all comments. Do not rewrite the entire file.

## Configuration reference

| Key | Default | Description |
|---|---|---|
| `model` | `gemma3:4b` | Ollama model for evaluation |
| `ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `timeout_ms` | `5000` | Per-rule evaluation timeout |
| `confidence_threshold` | `0.7` | Minimum confidence to count as violation |
| `max_parallel` | `4` | Concurrent Ollama calls |
| `think` | `false` | Enable /think mode (slower, more accurate) |
| `fail_open` | `true` | Skip rule on error vs block |
| `content_max_chars` | `800` | File content truncation in prompts |
| `log_file` | `null` | JSONL telemetry path |
| `rules_dir` | `rules` | Rules directory (relative to config dir) |
| `context.enabled` | `true` | Enable session context accumulator |
| `context.model` | `gemma3:4b` | Ollama model for accumulator |
| `context.min_events` | `3` | Minimum events before accumulator update |
| `context.lock_timeout_s` | `30` | Max wait for GPU lock |
| `context.summary_max_words` | `150` | Token budget for rolling summary |

## Model recommendations

| Hardware | Model | Notes |
|---|---|---|
| 8 GB RAM | `gemma3:4b` | Dense, ~3 GB at Q4 |
| 16 GB RAM | `gemma3:12b` | Dense, best accuracy for block rules |
