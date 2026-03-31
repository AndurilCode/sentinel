"""Tests for sentinel_scribe.py — no Ollama required."""

import json
import os
import sys
import tempfile
import hashlib
import time
from datetime import datetime, timezone

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture
def scribe_dir(tmp_path):
    """Create a temp directory structure mimicking .sentinel/scribe/."""
    scribe = tmp_path / "scribe"
    scribe.mkdir()
    return str(scribe)


@pytest.fixture
def config_dir(tmp_path):
    """Create a temp .claude/sentinel/ config dir with drafts/."""
    sentinel_dir = tmp_path / ".claude" / "sentinel"
    sentinel_dir.mkdir(parents=True)
    rules_dir = sentinel_dir / "rules"
    rules_dir.mkdir()
    drafts_dir = sentinel_dir / "drafts"
    drafts_dir.mkdir()
    return str(sentinel_dir)


@pytest.fixture
def base_config(config_dir):
    import sentinel_scribe
    return sentinel_scribe.load_config(config_dir)


def test_load_config_has_scribe_defaults(base_config):
    import sentinel_scribe
    scribe = base_config.get("scribe", {})
    assert scribe.get("enabled") is True
    assert scribe.get("model") is None
    assert scribe.get("guidance") is None
    assert scribe["thresholds"]["extraction_confidence"] == 0.7
    assert scribe["thresholds"]["draft_confidence"] == 0.7
    assert scribe["context_window_before"] == 5
    assert scribe["notification"]["max_age_days"] == 7
    assert "CLAUDE.md" in scribe["doc_globs"]


def test_load_config_merges_user_overrides(config_dir):
    import sentinel_scribe
    config_path = os.path.join(config_dir, "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump({"scribe": {"guidance": "focus on security", "context_window_before": 3}}, f)
    cfg = sentinel_scribe.load_config(config_dir)
    assert cfg["scribe"]["guidance"] == "focus on security"
    assert cfg["scribe"]["context_window_before"] == 3
    assert cfg["scribe"]["thresholds"]["extraction_confidence"] == 0.7


def test_append_observation(scribe_dir):
    import sentinel_scribe
    obs = {
        "ts": "2026-03-31T10:01:23Z",
        "source": "user_prompt",
        "session_id": "abc123",
        "statement": "Don't modify billing directly",
        "scope_hint": "src/core/billing",
        "trigger_hint": "file_write",
        "confidence": 0.91,
        "evidence": "don't touch billing",
        "drafted": False,
    }
    sentinel_scribe.append_observation(scribe_dir, obs)
    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert os.path.exists(obs_path)
    with open(obs_path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["statement"] == "Don't modify billing directly"


def test_append_observation_creates_dir(tmp_path):
    import sentinel_scribe
    scribe_dir = str(tmp_path / "nonexistent" / "scribe")
    obs = {"ts": "T", "source": "user_prompt", "statement": "test"}
    sentinel_scribe.append_observation(scribe_dir, obs)
    assert os.path.exists(os.path.join(scribe_dir, "observations.jsonl"))


def test_is_dismissed_empty(scribe_dir):
    import sentinel_scribe
    assert sentinel_scribe.is_dismissed(scribe_dir, "src/billing/**", "file_write") is False


def test_dismiss_and_check(scribe_dir):
    import sentinel_scribe
    sentinel_scribe.add_dismissal(scribe_dir, "src/billing/**", "file_write", "test statement")
    assert sentinel_scribe.is_dismissed(scribe_dir, "src/billing/**", "file_write") is True
    assert sentinel_scribe.is_dismissed(scribe_dir, "src/api/**", "file_write") is False


def test_dismiss_uses_scope_trigger_match(scribe_dir):
    import sentinel_scribe
    sentinel_scribe.add_dismissal(scribe_dir, "src/billing/**", "file_write", "billing protection")
    assert sentinel_scribe.is_dismissed(scribe_dir, "src/billing/**", "bash") is False


def test_build_context_window_from_transcript(tmp_path):
    """Should read last N events before the human prompt."""
    import sentinel_scribe
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/api/routes.ts"}}
        ]}, "timestamp": "T1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}
        ]}, "timestamp": "T2"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Tests are failing. Should I modify the test fixtures?"}
        ]}, "timestamp": "T3"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    window = sentinel_scribe.build_context_window(str(transcript), max_events=5)
    assert len(window) == 3
    assert "Edit" in window[0]
    assert "Bash" in window[1] or "npm test" in window[1]
    assert "test fixtures" in window[2]


def test_build_context_window_limits_events(tmp_path):
    """Should only return up to max_events entries."""
    import sentinel_scribe
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": f"msg {i}"}, "timestamp": f"T{i}"}
        for i in range(10)
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    window = sentinel_scribe.build_context_window(str(transcript), max_events=3)
    assert len(window) == 3
    # Should be the LAST 3 events
    assert "msg 7" in window[0]
    assert "msg 8" in window[1]
    assert "msg 9" in window[2]


def test_build_context_window_missing_transcript():
    """Should return empty list for missing transcript."""
    import sentinel_scribe
    window = sentinel_scribe.build_context_window("/nonexistent/path.jsonl", max_events=5)
    assert window == []


def test_build_context_window_skips_meta_tools(tmp_path):
    """Meta tools (TaskCreate, Skill, etc.) should be filtered out."""
    import sentinel_scribe
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "TaskCreate", "input": {"subject": "test"}}
        ]}, "timestamp": "T1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "src/app.py"}}
        ]}, "timestamp": "T2"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    window = sentinel_scribe.build_context_window(str(transcript), max_events=5)
    assert len(window) == 1
    assert "Write" in window[0] or "app.py" in window[0]
