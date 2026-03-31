"""Integration test for the info severity end-to-end flow.

Tests the full pipeline: rule loading → scope matching → template rendering
for PreToolUse info, and rule filtering for PostToolUse mode.
Does NOT require a running Ollama instance for PreToolUse static tests.
"""

import json
import os
import subprocess
import sys
import tempfile


def _run_sentinel(event_json: str, config_dir: str, args: list = None) -> tuple[str, int]:
    """Run sentinel.py with event on stdin, return (stdout, exit_code)."""
    cmd = [sys.executable, "sentinel.py"] + (args or [])
    env = os.environ.copy()
    env["SENTINEL_CONFIG_DIR"] = config_dir
    proc = subprocess.run(
        cmd, input=event_json, capture_output=True, text=True, env=env
    )
    return proc.stdout, proc.returncode


def test_pretool_info_static_context():
    """PreToolUse info rule renders prompt as additionalContext without LLM."""
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = os.path.join(tmp, ".claude", "sentinel")
        rules_dir = os.path.join(config_dir, "rules")
        os.makedirs(rules_dir)

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write("model: gemma3:4b\n")

        with open(os.path.join(rules_dir, "ownership.yaml"), "w") as f:
            f.write("""id: ownership
trigger: file_write
severity: info
scope:
  - "src/payments/**"
prompt: |
  Owned by Payments. Contact @payments-team.
  File: {{file_path}}
""")

        event = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/payments/charge.py",
                "content": "def charge(): pass"
            }
        })

        stdout, code = _run_sentinel(event, config_dir)
        assert code == 0
        assert stdout.strip()  # should have output

        output = json.loads(stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "Payments" in ctx
        assert "charge.py" in ctx
        assert "permissionDecision" not in output.get("hookSpecificOutput", {})


def test_pretool_info_no_match_silent():
    """PreToolUse info rule that doesn't match scope produces no output."""
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = os.path.join(tmp, ".claude", "sentinel")
        rules_dir = os.path.join(config_dir, "rules")
        os.makedirs(rules_dir)

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write("model: gemma3:4b\n")

        with open(os.path.join(rules_dir, "ownership.yaml"), "w") as f:
            f.write("""id: ownership
trigger: file_write
severity: info
scope:
  - "src/payments/**"
prompt: "Payments context"
""")

        event = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/auth/login.py",
                "content": "def login(): pass"
            }
        })

        stdout, code = _run_sentinel(event, config_dir)
        assert code == 0
        assert not stdout.strip()  # no output — scope didn't match


def test_post_mode_filters_non_info_rules():
    """--post mode ignores block and warn rules entirely."""
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = os.path.join(tmp, ".claude", "sentinel")
        rules_dir = os.path.join(config_dir, "rules")
        os.makedirs(rules_dir)

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write("model: gemma3:4b\n")

        with open(os.path.join(rules_dir, "blocker.yaml"), "w") as f:
            f.write("""id: blocker
trigger: file_write
severity: block
scope: ["**"]
prompt: "Block everything"
""")

        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "session_id": "test-123",
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "x"},
            "tool_response": {"success": True}
        })

        stdout, code = _run_sentinel(event, config_dir, args=["--post"])
        assert code == 0
        assert not stdout.strip()  # block rule ignored in post mode
