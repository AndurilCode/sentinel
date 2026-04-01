"""
Shared JSONL logging for Sentinel subsystems (context, scribe).

Writes to the same log_file as sentinel.py so all Ollama activity
is observable in one place via /sentinel-stats.
"""

import json
import time


def log_ollama(config: dict, level: str, action: str, model: str,
               elapsed_ms: float, *, error: str = "",
               response: str = ""):
    """Append a JSONL entry to the shared Sentinel log file.

    level:    subsystem identifier (e.g. "context", "scribe")
    action:   what was attempted (e.g. "accumulate", "extraction", "synthesis")
    response: truncated model output for diagnostics
    """
    log_path = config.get("log_file")
    if not log_path:
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "action": action,
        "model": model,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    if error:
        entry["error"] = error[:300]
    if response:
        entry["response"] = response[:400]
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
