import json
import tempfile
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "skills", "sentinel-stats"))
import importlib
sentinel_stats = importlib.import_module("sentinel-stats")


def _write_log(entries):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for e in entries:
        f.write(json.dumps(e) + "\n")
    f.close()
    return f.name


def test_trigger_breakdown():
    entries = [
        {"level": "eval", "ts": "2026-04-01T10:00:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "a.py", "violation": True, "blocked": True,
         "confidence": 0.9, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:01:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "bash", "tool": "Bash",
         "target": "rm -rf /", "violation": False, "blocked": False,
         "confidence": 0.3, "threshold": 0.7, "elapsed_ms": 200,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:02:00Z", "rule_id": "r2",
         "severity": "warn", "trigger": "file_write", "tool": "Edit",
         "target": "b.py", "violation": True, "blocked": False,
         "confidence": 0.8, "threshold": 0.7, "elapsed_ms": 150,
         "model": "qwen2.5:7b"},
    ]
    path = _write_log(entries)
    try:
        stats = sentinel_stats.compute_stats(sentinel_stats.load_entries(path))
        triggers = stats["evaluation"]["triggers"]
        assert triggers["file_write"]["evals"] == 2
        assert triggers["file_write"]["violations"] == 2
        assert triggers["file_write"]["blocks"] == 1
        assert triggers["bash"]["evals"] == 1
        assert triggers["bash"]["violations"] == 0
    finally:
        os.unlink(path)


def test_tool_breakdown():
    entries = [
        {"level": "eval", "ts": "2026-04-01T10:00:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "a.py", "violation": True, "blocked": True,
         "confidence": 0.9, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:01:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "b.py", "violation": False, "blocked": False,
         "confidence": 0.3, "threshold": 0.7, "elapsed_ms": 200,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:02:00Z", "rule_id": "r2",
         "severity": "warn", "trigger": "bash", "tool": "Bash",
         "target": "ls", "violation": True, "blocked": False,
         "confidence": 0.8, "threshold": 0.7, "elapsed_ms": 150,
         "model": "qwen2.5:7b"},
    ]
    path = _write_log(entries)
    try:
        stats = sentinel_stats.compute_stats(sentinel_stats.load_entries(path))
        tools = stats["evaluation"]["tools"]
        assert tools["Write"]["evals"] == 2
        assert tools["Write"]["blocks"] == 1
        assert tools["Bash"]["evals"] == 1
        assert tools["Bash"]["violations"] == 1
    finally:
        os.unlink(path)
