---
name: sentinel-stats
description: Show Sentinel telemetry stats — rule performance, latency, violations, and hottest targets
user-invocable: true
---

# Sentinel Stats

Show a performance dashboard from Sentinel's JSONL telemetry log.

## Process

Run the stats script from the repo root (it auto-discovers `.claude/sentinel/config.yaml` and reads `log_file` from it):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/sentinel-stats/sentinel-stats.py
```

If the script prints an error about no log file, tell the user:

> Telemetry logging is not enabled. Add `log_file: "telemetry.jsonl"` to your `.claude/sentinel/config.yaml` and re-run your workflow to collect data.

A specific log file path can also be passed directly: `python3 ... /path/to/log.jsonl`

Present the output to the user. Then provide a brief interpretation highlighting:

- **Noisy rules**: High eval count with 0% violation rate — candidates for scope narrowing or `fast_pattern` pre-filters.
- **Flaky rules**: Violation rate between 20-80% on the same target — the model is inconsistent, the prompt needs work.
- **Slow rules**: P95 or max latency near the timeout — risk of silent fail_open bypass.
- **Skipped/timeouts**: Any skipped entries indicate rules that silently passed without evaluation.
- **Hot targets**: Files or commands that trigger many evaluations — may need scope exclusions or rule consolidation.

If the user wants machine-readable output, run with `--json`.

## Next steps

Based on the stats interpretation, suggest the most relevant action:

- `/sentinel-rule` — edit or create rules to fix noisy, flaky, or missing coverage
- `/sentinel-config` — tune `confidence_threshold`, `timeout_ms`, or switch models if latency is high
- `/sentinel-learn` — discover new conventions from documentation if rule coverage is thin
