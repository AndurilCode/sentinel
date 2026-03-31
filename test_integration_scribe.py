"""Integration tests for Sentinel Scribe — subprocess-level tests with mocked Ollama."""

import json
import os
import sys
import subprocess
import tempfile
import time

import pytest
import yaml


@pytest.fixture
def project_dir(tmp_path):
    """Create a full project directory structure for scribe testing."""
    sentinel_dir = tmp_path / ".claude" / "sentinel"
    sentinel_dir.mkdir(parents=True)
    rules_dir = sentinel_dir / "rules"
    rules_dir.mkdir()
    drafts_dir = sentinel_dir / "drafts"
    drafts_dir.mkdir()

    config = {
        "model": "gemma3:4b",
        "ollama_url": "http://localhost:11434",
        "scribe": {
            "enabled": True,
            "guidance": None,
        },
    }
    with open(sentinel_dir / "config.yaml", "w") as f:
        yaml.dump(config, f)

    return tmp_path


def test_observe_subprocess_exits_cleanly_without_ollama(project_dir):
    """--observe should exit 0 even when Ollama is unreachable."""
    transcript = project_dir / "transcript.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "test"}]},
            "timestamp": "T1",
        }) + "\n")

    event = {
        "session_id": "integration-test",
        "user_prompt": "never touch the billing module",
        "transcript_path": str(transcript),
    }

    env = os.environ.copy()
    env["SENTINEL_CONFIG_DIR"] = str(project_dir / ".claude" / "sentinel")
    proc = subprocess.run(
        [sys.executable, "sentinel_scribe.py", "--observe"],
        input=json.dumps(event),
        capture_output=True, text=True, env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        timeout=10,
    )
    assert proc.returncode == 0


def test_learn_subprocess_exits_cleanly_without_ollama(project_dir):
    """--learn should exit 0 even when Ollama is unreachable."""
    with open(project_dir / "CLAUDE.md", "w") as f:
        f.write("# Rules\nNever commit secrets.")

    env = os.environ.copy()
    env["SENTINEL_CONFIG_DIR"] = str(project_dir / ".claude" / "sentinel")
    proc = subprocess.run(
        [sys.executable, "sentinel_scribe.py", "--learn"],
        capture_output=True, text=True, env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        timeout=10,
    )
    assert proc.returncode == 0
