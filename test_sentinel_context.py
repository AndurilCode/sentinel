import json
import os
import tempfile
from sentinel_context import (
    compact_event, parse_transcript_entries,
    build_accumulator_prompt
)


def test_compact_event_tool_result_with_error():
    """Tool result entries should show error status."""
    state = {"pending_tools": []}
    # First: assistant entry with tool_use
    tool_use_entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "npm test"}}
    ]}, "timestamp": "T1"}
    result1 = compact_event(tool_use_entry, state)
    assert result1 is not None
    assert "Bash" in result1["text"]

    # Second: user entry with tool_result containing error
    tool_result_entry = {"type": "user", "message": {"role": "user", "content": json.dumps([
        {"tool_use_id": "tu_1", "type": "tool_result", "is_error": True, "content": "Error: 3 tests failed\nExpected foo got bar"}
    ])}, "timestamp": "T2"}
    result2 = compact_event(tool_result_entry, state)
    assert result2 is not None
    assert "ERROR" in result2["text"]
    assert "3 tests failed" in result2["text"]


def test_compact_event_tool_result_success():
    """Successful tool results should show OK status."""
    state = {"pending_tools": []}
    tool_use_entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu_1", "name": "Edit", "input": {"file_path": "src/app.py"}}
    ]}, "timestamp": "T1"}
    compact_event(tool_use_entry, state)

    tool_result_entry = {"type": "user", "message": {"role": "user", "content": json.dumps([
        {"tool_use_id": "tu_1", "type": "tool_result", "content": "File edited successfully"}
    ])}, "timestamp": "T2"}
    result = compact_event(tool_result_entry, state)
    assert result is not None
    assert "OK" in result["text"]


def test_compact_event_without_state_still_works():
    """Backward compatibility: calling without state should still work."""
    entry = {"type": "user", "message": {"role": "user", "content": "Add a login page"}, "timestamp": "T"}
    result = compact_event(entry)
    assert result is not None
    assert "login page" in result["text"]


def test_compact_event_user_message():
    entry = {"type": "user", "message": {"role": "user", "content": "Add a login page"}, "timestamp": "2026-03-31T10:00:00Z"}
    result = compact_event(entry)
    assert result is not None
    assert result["trigger"] == "user"
    assert "login page" in result["text"]


def test_compact_event_skips_tool_result():
    """Raw tool result messages (JSON arrays) should be skipped."""
    entry = {"type": "user", "message": {"role": "user", "content": "[{'tool_use_id': 'abc', 'type': 'tool_result'}]"}, "timestamp": "T"}
    result = compact_event(entry)
    assert result is None


def test_compact_event_strips_meta_tools():
    """Meta tools (TaskCreate, Skill, etc.) should be filtered out."""
    entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "TaskCreate", "input": {"subject": "test"}},
        {"type": "text", "text": "Creating a task"}
    ]}, "timestamp": "T"}
    result = compact_event(entry)
    assert result is not None
    # Should contain the text but not TaskCreate
    assert "TaskCreate" not in result["text"]
    assert "Creating a task" in result["text"]


def test_compact_event_compresses_tool_inputs():
    """Tool inputs should be reduced to essentials."""
    entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/src/app.py", "content": "x" * 5000}}
    ]}, "timestamp": "T"}
    result = compact_event(entry)
    assert result is not None
    assert "/src/app.py" in result["text"]
    assert "x" * 100 not in result["text"]  # content should NOT be included


def test_parse_transcript_from_offset():
    """Should only read entries after the byte offset."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        line1 = json.dumps({"type": "user", "message": {"role": "user", "content": "first"}, "timestamp": "T1"})
        line2 = json.dumps({"type": "user", "message": {"role": "user", "content": "second"}, "timestamp": "T2"})
        f.write(line1 + "\n")
        offset = f.tell()
        f.write(line2 + "\n")
        path = f.name

    try:
        events, new_offset = parse_transcript_entries(path, offset)
        assert len(events) == 1
        assert "second" in events[0]["text"]
        assert new_offset > offset
    finally:
        os.unlink(path)


def test_build_accumulator_prompt_initial():
    """First update (no existing summary) should produce a valid prompt."""
    events = [{"trigger": "user", "text": "Build a REST API", "ts": "T"}]
    prompt = build_accumulator_prompt(None, events, max_words=150)
    assert "Build a REST API" in prompt
    assert "task_scope" in prompt


def test_build_accumulator_prompt_update():
    """Subsequent updates should include the existing summary."""
    existing = {"task_scope": "Building REST API", "progress": "Started", "current_focus": "Routes"}
    events = [{"trigger": "stop", "text": "Created GET /users endpoint", "ts": "T"}]
    prompt = build_accumulator_prompt(existing, events, max_words=150)
    assert "Building REST API" in prompt
    assert "GET /users" in prompt
