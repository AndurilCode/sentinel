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


def test_per_model_latency():
    entries = [
        {"level": "eval", "ts": "2026-04-01T10:00:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "a.py", "violation": False, "blocked": False,
         "confidence": 0.5, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:01:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "b.py", "violation": False, "blocked": False,
         "confidence": 0.4, "threshold": 0.7, "elapsed_ms": 300,
         "model": "llama3.2:3b"},
        {"level": "eval", "ts": "2026-04-01T10:02:00Z", "rule_id": "r2",
         "severity": "warn", "trigger": "bash", "tool": "Bash",
         "target": "ls", "violation": False, "blocked": False,
         "confidence": 0.3, "threshold": 0.7, "elapsed_ms": 500,
         "model": "qwen2.5:7b"},
    ]
    path = _write_log(entries)
    try:
        stats = sentinel_stats.compute_stats(sentinel_stats.load_entries(path))
        models = stats["performance"]["models"]
        assert "llama3.2:3b" in models
        assert models["llama3.2:3b"]["evals"] == 2
        assert models["llama3.2:3b"]["min_ms"] == 100
        assert models["llama3.2:3b"]["max_ms"] == 300
        assert "qwen2.5:7b" in models
        assert models["qwen2.5:7b"]["evals"] == 1
    finally:
        os.unlink(path)


def test_near_miss_detection():
    entries = [
        # Near miss: confidence 0.65, threshold 0.7 (gap = 0.05 < 0.1)
        {"level": "eval", "ts": "2026-04-01T10:00:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "sensitive.py", "violation": False, "blocked": False,
         "confidence": 0.65, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
        # Not a near miss: confidence 0.3, threshold 0.7 (gap = 0.4)
        {"level": "eval", "ts": "2026-04-01T10:01:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "safe.py", "violation": False, "blocked": False,
         "confidence": 0.3, "threshold": 0.7, "elapsed_ms": 200,
         "model": "llama3.2:3b"},
        # Near miss: confidence 0.62, threshold 0.7 (gap = 0.08 < 0.1)
        {"level": "eval", "ts": "2026-04-01T10:02:00Z", "rule_id": "r2",
         "severity": "warn", "trigger": "bash", "tool": "Bash",
         "target": "deploy.sh", "violation": False, "blocked": False,
         "confidence": 0.62, "threshold": 0.7, "elapsed_ms": 150,
         "model": "llama3.2:3b"},
        # Violation — not a near miss even if close
        {"level": "eval", "ts": "2026-04-01T10:03:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "bad.py", "violation": True, "blocked": True,
         "confidence": 0.75, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
    ]
    path = _write_log(entries)
    try:
        stats = sentinel_stats.compute_stats(sentinel_stats.load_entries(path))
        nm = stats["health"]["near_misses"]
        assert nm["total"] == 2
        assert nm["by_rule"]["r1"]["count"] == 1
        assert "sensitive.py" in nm["by_rule"]["r1"]["example_targets"]
        assert nm["by_rule"]["r2"]["count"] == 1
    finally:
        os.unlink(path)


def test_near_miss_max_examples():
    """Near miss example_targets capped at 3 per rule."""
    entries = [
        {"level": "eval", "ts": f"2026-04-01T10:0{i}:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": f"file{i}.py", "violation": False, "blocked": False,
         "confidence": 0.65, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"}
        for i in range(5)
    ]
    path = _write_log(entries)
    try:
        stats = sentinel_stats.compute_stats(sentinel_stats.load_entries(path))
        nm = stats["health"]["near_misses"]
        assert nm["total"] == 5
        assert nm["by_rule"]["r1"]["count"] == 5
        assert len(nm["by_rule"]["r1"]["example_targets"]) == 3
    finally:
        os.unlink(path)


def test_pipeline_stats():
    entries = [
        {"level": "scribe", "ts": "2026-04-01T10:00:00Z",
         "action": "reflect_extraction", "model": "llama3.2:3b",
         "elapsed_ms": 1200},
        {"level": "scribe", "ts": "2026-04-01T10:01:00Z",
         "action": "reflect_extraction", "model": "llama3.2:3b",
         "elapsed_ms": 1400, "error": "parse failure"},
        {"level": "scribe", "ts": "2026-04-01T10:02:00Z",
         "action": "reflect_validation", "model": "llama3.2:3b",
         "elapsed_ms": 800},
        {"level": "context", "ts": "2026-04-01T10:03:00Z",
         "action": "accumulate", "model": "llama3.2:3b",
         "elapsed_ms": 350},
        {"level": "context", "ts": "2026-04-01T10:04:00Z",
         "action": "accumulate", "model": "llama3.2:3b",
         "elapsed_ms": 400, "error": "timeout"},
    ]
    path = _write_log(entries)
    try:
        all_entries = sentinel_stats.load_entries(path)
        pipeline = sentinel_stats.compute_pipeline_stats(all_entries)
        assert pipeline["reflect_extraction"]["count"] == 2
        assert pipeline["reflect_extraction"]["errors"] == 1
        assert pipeline["reflect_extraction"]["success_rate"] == 0.5
        assert pipeline["reflect_extraction"]["max_ms"] == 1400
        assert pipeline["reflect_validation"]["count"] == 1
        assert pipeline["reflect_validation"]["errors"] == 0
        assert pipeline["reflect_validation"]["success_rate"] == 1.0
        assert pipeline["accumulate"]["count"] == 2
        assert pipeline["accumulate"]["errors"] == 1
    finally:
        os.unlink(path)


def test_pipeline_stats_empty():
    entries = [
        {"level": "eval", "ts": "2026-04-01T10:00:00Z", "rule_id": "r1",
         "severity": "block", "trigger": "file_write", "tool": "Write",
         "target": "a.py", "violation": False, "blocked": False,
         "confidence": 0.5, "threshold": 0.7, "elapsed_ms": 100,
         "model": "llama3.2:3b"},
    ]
    path = _write_log(entries)
    try:
        all_entries = sentinel_stats.load_entries(path)
        pipeline = sentinel_stats.compute_pipeline_stats(all_entries)
        assert pipeline == {}
    finally:
        os.unlink(path)


def test_observation_stats(tmp_path):
    obs_path = tmp_path / "observations.jsonl"
    observations = [
        {"ts": "2026-04-01T10:00:00Z", "source": "user_feedback",
         "session_id": "s1", "statement": "use snake_case",
         "scope_hint": "**", "trigger_hint": "file_write",
         "confidence": 0.8, "evidence": "...", "drafted": True},
        {"ts": "2026-04-01T10:01:00Z", "source": "user_feedback",
         "session_id": "s1", "statement": "no print statements",
         "scope_hint": "**", "trigger_hint": "file_write",
         "confidence": 0.6, "evidence": "...", "drafted": False},
        {"ts": "2026-04-01T10:02:00Z", "source": "agent_self_correction",
         "session_id": "s2", "statement": "prefer pathlib",
         "scope_hint": "**", "trigger_hint": "file_write",
         "confidence": 0.9, "evidence": "...", "drafted": True},
    ]
    with open(obs_path, "w") as f:
        for obs in observations:
            f.write(json.dumps(obs) + "\n")

    stats = sentinel_stats.compute_observation_stats(str(obs_path))
    assert stats["total"] == 3
    assert stats["by_source"]["user_feedback"] == 2
    assert stats["by_source"]["agent_self_correction"] == 1
    assert stats["draft_rate"] == round(2 / 3, 2)
    assert stats["by_session"]["s1"] == 2
    assert stats["by_session"]["s2"] == 1


def test_dismissal_stats(tmp_path):
    dis_path = tmp_path / "dismissed.jsonl"
    dismissals = [
        {"scope": "**", "trigger": "file_write",
         "statement_hash": "abc123", "dismissed_at": "2026-04-01T10:00:00Z"},
        {"scope": "src/**", "trigger": "file_write",
         "statement_hash": "abc123", "dismissed_at": "2026-04-01T10:01:00Z"},
        {"scope": "**", "trigger": "bash",
         "statement_hash": "def456", "dismissed_at": "2026-04-01T10:02:00Z"},
    ]
    with open(dis_path, "w") as f:
        for d in dismissals:
            f.write(json.dumps(d) + "\n")

    stats = sentinel_stats.compute_dismissal_stats(str(dis_path))
    assert stats["total"] == 3
    assert len(stats["top_patterns"]) == 2
    assert stats["top_patterns"][0]["count"] == 2
    assert stats["top_patterns"][0]["key"] == "abc123"


def test_observation_stats_missing_file():
    stats = sentinel_stats.compute_observation_stats("/nonexistent/path.jsonl")
    assert stats is None


def test_dismissal_stats_missing_file():
    stats = sentinel_stats.compute_dismissal_stats("/nonexistent/path.jsonl")
    assert stats is None
