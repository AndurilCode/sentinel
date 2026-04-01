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
| `ollama_concurrency` | `1` | Max concurrent Ollama HTTP calls (GPU-bound) |
| `think` | `false` | Enable /think mode (slower, more accurate) |
| `fail_open` | `true` | Skip rule on error vs block |
| `content_max_chars` | `800` | File content truncation in prompts |
| `log_file` | `null` | JSONL telemetry path |
| `rules_dir` | `rules` | Rules directory (relative to config dir) |
| `tool_map` | *(see reference.md)* | Tool name → trigger type mapping |
| `mcp_prefix` | `mcp__` | Prefix for detecting MCP tool names |
| `mcp_separator` | `__` | Separator for parsing MCP server/tool from tool name |
| `context.enabled` | `true` | Enable session context accumulator |
| `context.model` | `gemma3:4b` | Ollama model for accumulator |
| `context.min_events` | `3` | Minimum events before accumulator update |
| `context.lock_timeout_s` | `30` | Max wait for GPU lock |
| `context.summary_max_words` | `150` | Token budget for rolling summary |
| `scribe.enabled` | `true` | Enable the Scribe convention learning pipeline |
| `scribe.model` | *(top-level model)* | Default Ollama model for all scribe steps |
| `scribe.extraction_model` | *(scribe.model)* | Override for extraction (reflect + learn) |
| `scribe.synthesis_model` | *(scribe.model)* | Override for validation + synthesis |
| `scribe.guidance` | `null` | Priority guidance text for extraction |
| `scribe.think` | `false` | Enable /think mode for validation+synthesis |
| `scribe.extraction_timeout_ms` | `15000` | Timeout for extraction LLM calls |
| `scribe.extraction_num_predict` | `1000` | Max output tokens for extraction |
| `scribe.synthesis_timeout_ms` | `15000` | Timeout for validation+synthesis LLM calls |
| `scribe.synthesis_num_predict` | `1000` | Max output tokens for validation+synthesis |
| `scribe.temperature` | `0.1` | LLM temperature for all scribe calls |
| `scribe.transcript_budget_chars` | `4000` | Max compacted transcript size before truncation |
| `scribe.thresholds.extraction_confidence` | `0.7` | Minimum confidence to store an observation |
| `scribe.thresholds.draft_confidence` | `0.8` | Minimum confidence for learn mode drafts |
| `scribe.sources.documentation` | `true` | Enable doc scanning via /sentinel-learn |
| `scribe.doc_globs` | `[CLAUDE.md, AGENTS.md, ...]` | File patterns for /sentinel-learn |
| `scribe.notification.max_age_days` | `7` | Max age for draft notifications |

## Model recommendations

| Hardware | Model | Notes |
|---|---|---|
| 8 GB RAM | `gemma3:4b` | Dense, ~3 GB at Q4 |
| 16 GB RAM | `gemma3:12b` | Dense, best accuracy for block rules |
