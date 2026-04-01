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


def test_build_human_prompt_with_context():
    import sentinel_scribe
    window_lines = [
        "[assistant] Tests are failing. Should I modify the test fixtures?",
        "[human] no",
    ]
    prompt = sentinel_scribe.build_human_extraction_prompt(window_lines, guidance=None)
    assert "Conversation context:" in prompt
    assert "test fixtures" in prompt
    assert "[human] no" in prompt
    assert "PRIORITY GUIDANCE" not in prompt


def test_build_human_prompt_with_guidance():
    import sentinel_scribe
    window_lines = ["[human] don't touch billing"]
    prompt = sentinel_scribe.build_human_extraction_prompt(
        window_lines, guidance="Focus on security and billing boundaries"
    )
    assert "PRIORITY GUIDANCE" in prompt
    assert "security and billing" in prompt


def test_build_doc_prompt():
    import sentinel_scribe
    prompt = sentinel_scribe.build_doc_extraction_prompt(
        content="Never commit .env files", source_type="CLAUDE.md", guidance=None
    )
    assert "Source type: CLAUDE.md" in prompt
    assert "Never commit .env" in prompt


def test_parse_extraction_response_valid():
    import sentinel_scribe
    response = '{"conventions": [{"statement": "No billing edits", "scope_hint": "src/billing", "trigger_hint": "file_write", "confidence": 0.9, "evidence": "don\'t touch billing"}]}'
    result = sentinel_scribe.parse_extraction_response(response)
    assert len(result) == 1
    assert result[0]["statement"] == "No billing edits"


def test_parse_extraction_response_empty():
    import sentinel_scribe
    result = sentinel_scribe.parse_extraction_response('{"conventions": []}')
    assert result == []


def test_parse_extraction_response_malformed():
    import sentinel_scribe
    result = sentinel_scribe.parse_extraction_response("not json at all")
    assert result == []


def test_parse_extraction_response_with_stray_text():
    import sentinel_scribe
    response = 'Here is my analysis:\n{"conventions": [{"statement": "test", "scope_hint": "src/", "trigger_hint": "bash", "confidence": 0.8, "evidence": "evidence"}]}\nDone.'
    result = sentinel_scribe.parse_extraction_response(response)
    assert len(result) == 1


def test_normalize_trigger_hint_pipe_separated():
    """Should extract first valid trigger from pipe-separated values."""
    import sentinel_scribe
    assert sentinel_scribe._normalize_trigger_hint("file_write|read|modify") == "file_write"
    assert sentinel_scribe._normalize_trigger_hint("bash|mcp|unknown") == "bash"
    assert sentinel_scribe._normalize_trigger_hint("read|modify") == "unknown"
    assert sentinel_scribe._normalize_trigger_hint("mcp") == "mcp"
    assert sentinel_scribe._normalize_trigger_hint("") == "unknown"
    assert sentinel_scribe._normalize_trigger_hint("file_write") == "file_write"


def test_parse_extraction_normalizes_trigger():
    """parse_extraction_response should normalize trigger_hint values."""
    import sentinel_scribe
    response = json.dumps({"conventions": [{
        "statement": "test",
        "scope_hint": "src/billing",
        "trigger_hint": "file_write|read|modify",
        "confidence": 0.9,
        "evidence": "test",
    }]})
    result = sentinel_scribe.parse_extraction_response(response)
    assert len(result) == 1
    assert result[0]["trigger_hint"] == "file_write"



def test_write_draft_yaml(config_dir):
    """Should write a valid draft YAML with _draft metadata."""
    import sentinel_scribe
    drafts_dir = os.path.join(config_dir, "drafts")
    draft = {
        "id": "no-billing-edits",
        "trigger": "file_write",
        "severity": "block",
        "scope": ["src/billing/**"],
        "exclude": ["**/*.test.ts"],
        "prompt": "Test prompt {{file_path}}",
    }
    draft_meta = {
        "source": "user_prompt",
        "observed": 1,
        "first_seen": "2026-03-31",
        "evidence": ["don't touch billing"],
        "confidence": 0.91,
        "synthesized": "2026-03-31T10:01:45Z",
        "model": "gemma3:4b",
    }
    sentinel_scribe.write_draft(drafts_dir, draft, draft_meta)
    path = os.path.join(drafts_dir, "no-billing-edits.draft.yaml")
    assert os.path.exists(path)
    with open(path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["id"] == "no-billing-edits"
    assert loaded["trigger"] == "file_write"
    assert loaded["_draft"]["source"] == "user_prompt"
    assert loaded["_draft"]["confidence"] == 0.91


def test_build_synthesis_prompt():
    import sentinel_scribe
    observation = {
        "statement": "Never edit billing directly",
        "scope_hint": "src/billing",
        "trigger_hint": "file_write",
        "evidence": "don't touch billing",
    }
    prompt = sentinel_scribe.build_synthesis_prompt(
        observation=observation,
        matched_files=["src/billing/invoice.ts", "src/billing/payment.ts"],
        sample_rules=[{"id": "test-rule", "trigger": "bash", "scope": ["git *"], "prompt": "test"}],
    )
    assert "Never edit billing directly" in prompt
    assert "src/billing/invoice.ts" in prompt
    assert "test-rule" in prompt
    assert "file_write" in prompt
    assert "ACTUAL files" in prompt or "REAL paths" in prompt


from unittest.mock import patch, MagicMock


def test_observe_pipeline_extracts_convention(tmp_path, config_dir):
    """Full --observe pipeline with mocked Ollama calls."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Should I modify the test fixtures to match?"}
        ]}, "timestamp": "T1"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")

    extraction_response = json.dumps({
        "conventions": [{
            "statement": "Fix source code, don't patch tests",
            "scope_hint": "**/*.test.*",
            "trigger_hint": "file_write",
            "confidence": 0.9,
            "evidence": "no",
        }]
    })

    synthesis_response = """id: no-test-patching
trigger: file_write
severity: warn
scope:
  - "**/*.test.*"
prompt: |
  Test prompt {{file_path}}
"""

    call_count = {"n": 0}
    def mock_ollama(prompt, model, cfg, json_format=True, think=False, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return extraction_response
        return synthesis_response

    with patch.object(sentinel_scribe, "call_ollama", side_effect=mock_ollama):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.observe(
                    user_prompt="no",
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert os.path.exists(obs_path)
    with open(obs_path) as f:
        obs = json.loads(f.readline())
    assert obs["statement"] == "Fix source code, don't patch tests"

    drafts_dir = os.path.join(config_dir, "drafts")
    draft_files = [f for f in os.listdir(drafts_dir) if f.endswith(".draft.yaml")]
    assert len(draft_files) == 1


def test_observe_defers_when_lock_unavailable(tmp_path, config_dir):
    """Should write deferred file when GPU lock is unavailable."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "test"}
        ]}, "timestamp": "T1"}) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")

    # Mock acquire_lock to return None (lock unavailable)
    with patch("sentinel_lock.acquire_lock", return_value=None):
        sentinel_scribe.observe(
            user_prompt="never touch billing",
            transcript_path=str(transcript),
            session_id="test-session",
            config=config,
            config_dir=config_dir,
            scribe_dir=scribe_dir,
            session_dir=session_dir,
        )

    # Should have written a deferred file
    deferred_dir = os.path.join(scribe_dir, "deferred")
    assert os.path.isdir(deferred_dir)
    deferred_files = os.listdir(deferred_dir)
    assert len(deferred_files) == 1
    with open(os.path.join(deferred_dir, deferred_files[0])) as f:
        deferred = json.load(f)
    assert deferred["user_prompt"] == "never touch billing"
    assert deferred["session_id"] == "test-session"


def test_observe_pipeline_skips_low_confidence(tmp_path, config_dir):
    """Should not store observation if confidence is below threshold."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I'll add the function now"}
        ]}, "timestamp": "T1"}) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")

    extraction_response = json.dumps({
        "conventions": [{
            "statement": "something vague",
            "scope_hint": "**",
            "trigger_hint": "unknown",
            "confidence": 0.3,
            "evidence": "yes",
        }]
    })

    with patch.object(sentinel_scribe, "call_ollama", return_value=extraction_response):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.observe(
                    user_prompt="yes",
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert not os.path.exists(obs_path)


def test_observe_pipeline_no_convention(tmp_path, config_dir):
    """Should do nothing when no convention is extracted."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "add a login page"}, "timestamp": "T1"}) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")

    with patch.object(sentinel_scribe, "call_ollama", return_value='{"conventions": []}'):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.observe(
                    user_prompt="add a login page",
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert not os.path.exists(obs_path)


def test_flush_processes_deferred(tmp_path, config_dir):
    """Flush should process deferred observations."""
    import sentinel_scribe

    scribe_dir = str(tmp_path / "scribe")
    deferred_dir = os.path.join(scribe_dir, "deferred")
    os.makedirs(deferred_dir)

    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Should I delete the database?"}
        ]}, "timestamp": "T1"}) + "\n")

    deferred = {
        "user_prompt": "no, never do that",
        "transcript_path": str(transcript),
        "session_id": "test",
        "window_lines": ["[assistant] Should I delete the database?", "[human] no, never do that"],
        "ts": "2026-03-31T10:00:00Z",
    }
    with open(os.path.join(deferred_dir, "1.json"), "w") as f:
        json.dump(deferred, f)

    config = sentinel_scribe.load_config(config_dir)
    session_dir = str(tmp_path / "sessions" / "test")

    extraction_response = json.dumps({"conventions": [{
        "statement": "Never delete the database",
        "scope_hint": "**",
        "trigger_hint": "bash",
        "confidence": 0.95,
        "evidence": "no, never do that",
    }]})
    synthesis_response = """id: no-db-deletion
trigger: bash
severity: block
scope:
  - "*"
prompt: |
  Test {{command}}
"""

    call_count = {"n": 0}
    def mock_ollama(prompt, model, cfg, json_format=True, think=False, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return extraction_response
        return synthesis_response

    with patch.object(sentinel_scribe, "call_ollama", side_effect=mock_ollama):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.flush(
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                    session_id="test",
                )

    assert len(os.listdir(deferred_dir)) == 0


def test_flush_no_deferred_is_noop(tmp_path, config_dir):
    """Flush with no deferred files should do nothing."""
    import sentinel_scribe
    scribe_dir = str(tmp_path / "scribe")
    os.makedirs(scribe_dir)
    config = sentinel_scribe.load_config(config_dir)
    session_dir = str(tmp_path / "sessions" / "test")
    sentinel_scribe.flush(
        config=config, config_dir=config_dir,
        scribe_dir=scribe_dir, session_dir=session_dir,
        session_id="test",
    )


def test_learn_scans_documentation(tmp_path, config_dir):
    """Learn should scan doc files and extract conventions."""
    import sentinel_scribe

    project_root = os.path.dirname(os.path.dirname(config_dir))
    claude_md = os.path.join(project_root, "CLAUDE.md")
    with open(claude_md, "w") as f:
        f.write("# Rules\n\nNever commit .env files.\nAlways run tests before pushing.")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")

    extraction_response = json.dumps({"conventions": [{
        "statement": "Never commit .env files",
        "scope_hint": "**/.env",
        "trigger_hint": "file_write",
        "confidence": 0.95,
        "evidence": "Never commit .env files",
    }]})
    synthesis_response = """id: no-env-commit
trigger: file_write
severity: block
scope:
  - "**/.env"
prompt: |
  Test {{file_path}}
"""

    call_count = {"n": 0}
    def mock_ollama(prompt, model, cfg, json_format=True, think=False, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return extraction_response
        return synthesis_response

    with patch.object(sentinel_scribe, "call_ollama", side_effect=mock_ollama):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                result = sentinel_scribe.learn(
                    config=config, config_dir=config_dir,
                    scribe_dir=scribe_dir, session_dir=session_dir,
                )

    assert result["files_scanned"] >= 1
    assert result["conventions_found"] >= 1


def test_check_pending_drafts_returns_notification(config_dir, tmp_path):
    """Should return notification text when recent drafts exist."""
    import sentinel_scribe
    drafts_dir = os.path.join(config_dir, "drafts")
    draft = {
        "id": "test-draft",
        "trigger": "file_write",
        "scope": ["src/**"],
        "prompt": "test",
        "_draft": {
            "source": "user_prompt",
            "synthesized": datetime.now(timezone.utc).isoformat(),
        },
    }
    with open(os.path.join(drafts_dir, "test-draft.draft.yaml"), "w") as f:
        yaml.dump(draft, f)

    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    notification = sentinel_scribe.check_pending_drafts(
        drafts_dir=drafts_dir,
        session_dir=session_dir,
        max_age_days=7,
    )
    assert notification is not None
    assert "draft" in notification.lower()
    assert "/sentinel-drafts" in notification


def test_check_pending_drafts_skips_if_already_notified(config_dir, tmp_path):
    """Should return None if already notified this session."""
    import sentinel_scribe
    drafts_dir = os.path.join(config_dir, "drafts")
    draft = {
        "id": "test-draft",
        "trigger": "file_write",
        "scope": ["src/**"],
        "prompt": "test",
        "_draft": {"source": "user_prompt", "synthesized": datetime.now(timezone.utc).isoformat()},
    }
    with open(os.path.join(drafts_dir, "test-draft.draft.yaml"), "w") as f:
        yaml.dump(draft, f)

    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    n1 = sentinel_scribe.check_pending_drafts(drafts_dir, session_dir, 7)
    assert n1 is not None

    n2 = sentinel_scribe.check_pending_drafts(drafts_dir, session_dir, 7)
    assert n2 is None


def test_check_pending_drafts_skips_old_drafts(config_dir, tmp_path):
    """Should not notify for drafts older than max_age_days."""
    import sentinel_scribe
    drafts_dir = os.path.join(config_dir, "drafts")
    old_time = "2026-03-01T10:00:00+00:00"
    draft = {
        "id": "old-draft",
        "trigger": "bash",
        "scope": ["*"],
        "prompt": "test",
        "_draft": {"source": "user_prompt", "synthesized": old_time},
    }
    with open(os.path.join(drafts_dir, "old-draft.draft.yaml"), "w") as f:
        yaml.dump(draft, f)

    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    notification = sentinel_scribe.check_pending_drafts(drafts_dir, session_dir, 7)
    assert notification is None


def test_read_compacted_transcript(tmp_path):
    """Should read and compact full transcript with budget truncation."""
    import sentinel_scribe
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Add a login page"}, "timestamp": "T1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "src/login.py"}}
        ]}, "timestamp": "T2"},
        {"type": "user", "message": {"role": "user", "content": json.dumps([
            {"tool_use_id": "tu_1", "type": "tool_result", "content": "File written"}
        ])}, "timestamp": "T3"},
        {"type": "user", "message": {"role": "user", "content": "never use eval"}, "timestamp": "T4"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    result = sentinel_scribe.read_compacted_transcript(str(transcript), budget_chars=10000)
    assert "[human] Add a login page" in result
    assert "[assistant]" in result
    assert "[result]" in result
    assert "[human] never use eval" in result


def test_read_compacted_transcript_truncation(tmp_path):
    """Should truncate long transcripts keeping head + tail."""
    import sentinel_scribe
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": f"message {i}" * 20}, "timestamp": f"T{i}"}
        for i in range(50)
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    result = sentinel_scribe.read_compacted_transcript(str(transcript), budget_chars=500)
    assert len(result) <= 600  # some tolerance for the truncation marker
    assert "truncated" in result.lower()


def test_read_compacted_transcript_missing_file():
    """Should return empty string for missing transcript."""
    import sentinel_scribe
    result = sentinel_scribe.read_compacted_transcript("/nonexistent/path.jsonl")
    assert result == ""


def test_build_transcript_extraction_prompt():
    import sentinel_scribe
    transcript_text = "[human] Add login\n[assistant] [tools: Write(src/login.py)]\n[result] → OK\n[human] never use eval"
    summary = {"task_scope": "Building auth", "progress": "Started", "current_focus": "Login"}
    prompt = sentinel_scribe.build_transcript_extraction_prompt(
        transcript_text=transcript_text,
        summary=summary,
        guidance=None,
    )
    assert "never use eval" in prompt
    assert "Building auth" in prompt
    assert "agent_self_correction" in prompt
    assert "user_feedback" in prompt


def test_build_transcript_extraction_prompt_no_summary():
    import sentinel_scribe
    transcript_text = "[human] fix the bug"
    prompt = sentinel_scribe.build_transcript_extraction_prompt(
        transcript_text=transcript_text,
        summary=None,
        guidance="Focus on security",
    )
    assert "fix the bug" in prompt
    assert "PRIORITY GUIDANCE" in prompt


def test_build_validation_prompt():
    import sentinel_scribe
    observation = {
        "statement": "Never edit billing directly",
        "scope_hint": "src/billing",
        "trigger_hint": "file_write",
        "evidence": "agent tried to edit billing, got corrected",
        "source": "user_feedback",
    }
    existing_rules = [
        {"id": "no-eval", "trigger": "file_write", "scope": ["**"], "prompt": "Check for eval usage"}
    ]
    matched_files = ["src/billing/invoice.ts", "src/billing/payment.ts"]
    prompt = sentinel_scribe.build_validation_prompt(
        observation=observation,
        existing_rules=existing_rules,
        matched_files=matched_files,
    )
    assert "Never edit billing directly" in prompt
    assert "no-eval" in prompt
    assert "src/billing/invoice.ts" in prompt
    assert "redundant" in prompt.lower()


def test_parse_validation_response_redundant():
    import sentinel_scribe
    response = '{"redundant": true, "reason": "Already covered by no-billing-edits rule"}'
    result = sentinel_scribe.parse_validation_response(response)
    assert result is not None
    assert result["redundant"] is True


def test_parse_validation_response_new_rule():
    import sentinel_scribe
    response = """id: no-billing-edits
trigger: file_write
severity: block
scope:
  - "src/billing/**"
prompt: |
  Test {{file_path}}
"""
    result = sentinel_scribe.parse_validation_response(response)
    assert result is not None
    assert result.get("redundant") is not True
    assert result["rule"]["id"] == "no-billing-edits"
    assert result["rule"]["prompt"] is not None


def test_parse_validation_response_malformed():
    import sentinel_scribe
    result = sentinel_scribe.parse_validation_response("garbage output")
    assert result is None


def test_reflect_pipeline_extracts_and_drafts(tmp_path, config_dir):
    """Full --reflect pipeline: extract from transcript, validate, write draft."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Add a login page"}, "timestamp": "T1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "npm test"}}
        ]}, "timestamp": "T2"},
        {"type": "user", "message": {"role": "user", "content": json.dumps([
            {"tool_use_id": "tu_1", "type": "tool_result", "is_error": True, "content": "Error: eval is not allowed"}
        ])}, "timestamp": "T3"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I see, eval is forbidden. Let me fix this."}
        ]}, "timestamp": "T4"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    extraction_response = json.dumps({"conventions": [{
        "statement": "Never use eval() in this codebase",
        "scope_hint": "**",
        "trigger_hint": "file_write",
        "confidence": 0.9,
        "evidence": "eval is not allowed",
        "source": "agent_self_correction",
    }]})

    validation_response = """id: no-eval-usage
trigger: file_write
severity: block
scope:
  - "**"
prompt: |
  Check if this file uses eval(). File: {{file_path}}
  Content: {{content_snippet}}
  Respond ONLY with JSON: {"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}
"""

    call_count = {"n": 0}
    def mock_ollama(prompt, model, cfg, json_format=True, think=False, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return extraction_response
        return validation_response

    with patch.object(sentinel_scribe, "call_ollama", side_effect=mock_ollama):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.reflect(
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    # Should have stored observation
    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert os.path.exists(obs_path)
    with open(obs_path) as f:
        obs = json.loads(f.readline())
    assert obs["statement"] == "Never use eval() in this codebase"
    assert obs["source"] == "agent_self_correction"

    # Should have written draft
    drafts_dir = os.path.join(config_dir, "drafts")
    draft_files = [f for f in os.listdir(drafts_dir) if f.endswith(".draft.yaml")]
    assert len(draft_files) == 1
    with open(os.path.join(drafts_dir, draft_files[0])) as f:
        draft = yaml.safe_load(f)
    assert draft["_draft"]["source"] == "agent_self_correction"


def test_reflect_skips_redundant_conventions(tmp_path, config_dir):
    """Should not write draft if validation says redundant."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "never use eval"}, "timestamp": "T1"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    # Create an existing rule
    rules_dir = os.path.join(config_dir, "rules")
    with open(os.path.join(rules_dir, "no-eval.yaml"), "w") as f:
        yaml.dump({"id": "no-eval", "trigger": "file_write", "scope": ["**"], "prompt": "no eval"}, f)

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    extraction_response = json.dumps({"conventions": [{
        "statement": "Do not use eval",
        "scope_hint": "**",
        "trigger_hint": "file_write",
        "confidence": 0.9,
        "evidence": "never use eval",
        "source": "user_feedback",
    }]})
    validation_response = '{"redundant": true, "reason": "Covered by existing no-eval rule"}'

    call_count = {"n": 0}
    def mock_ollama(prompt, model, cfg, json_format=True, think=False, timeout_ms=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return extraction_response
        return validation_response

    with patch.object(sentinel_scribe, "call_ollama", side_effect=mock_ollama):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.reflect(
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    # Should have stored observation but NOT written draft
    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert os.path.exists(obs_path)
    drafts_dir = os.path.join(config_dir, "drafts")
    draft_files = [f for f in os.listdir(drafts_dir) if f.endswith(".draft.yaml")]
    assert len(draft_files) == 0


def test_reflect_no_conventions(tmp_path, config_dir):
    """Should do nothing when no conventions extracted."""
    import sentinel_scribe

    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "add a login page"}, "timestamp": "T1"},
    ]
    with open(transcript, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    config = sentinel_scribe.load_config(config_dir)
    scribe_dir = str(tmp_path / "scribe")
    session_dir = str(tmp_path / "sessions" / "test")
    os.makedirs(session_dir, exist_ok=True)

    with patch.object(sentinel_scribe, "call_ollama", return_value='{"conventions": []}'):
        with patch("sentinel_lock.acquire_lock", return_value=99):
            with patch("sentinel_lock.release_lock"):
                sentinel_scribe.reflect(
                    transcript_path=str(transcript),
                    session_id="test-session",
                    config=config,
                    config_dir=config_dir,
                    scribe_dir=scribe_dir,
                    session_dir=session_dir,
                )

    obs_path = os.path.join(scribe_dir, "observations.jsonl")
    assert not os.path.exists(obs_path)
