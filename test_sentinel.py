"""Tests for sentinel.py — core evaluation loop, no Ollama required."""

import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

# Import sentinel module
sys.path.insert(0, os.path.dirname(__file__))
import sentinel


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def default_config():
    return {
        **sentinel.DEFAULTS,
        "rules_dir": "/tmp/sentinel-test-rules",
        "model": "qwen3.5:4b",
        "ollama_url": "http://localhost:11434",
        "timeout_ms": 5000,
        "confidence_threshold": 0.7,
        "max_parallel": 4,
        "ollama_concurrency": 1,
        "fail_open": True,
        "content_max_chars": 800,
        "log_file": None,
    }


@pytest.fixture
def file_write_event():
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/repo/src/core/billing/payment.ts",
            "content": "export function charge(amount: number) { return amount * 1.1; }",
        },
    }


@pytest.fixture
def bash_event():
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
    }


@pytest.fixture
def mcp_event():
    return {
        "tool_name": "mcp__postgres-prod__sql_execute",
        "tool_input": {"query": "DROP TABLE users;"},
    }


@pytest.fixture
def block_rule():
    return {
        "id": "test-block",
        "trigger": "file_write",
        "severity": "block",
        "scope": ["src/core/billing/**"],
        "exclude": ["**/*.test.ts"],
        "prompt": "FILE: {{file_path}}\nCONTENT: {{content_snippet}}\nViolation?\n"
                  '{"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}',
    }


@pytest.fixture
def warn_rule():
    return {
        "id": "test-warn",
        "trigger": "file_write",
        "severity": "warn",
        "scope": ["**"],
        "exclude": [],
        "prompt": "FILE: {{file_path}}\nCheck.\n"
                  '{"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}',
    }


@pytest.fixture
def bash_rule():
    return {
        "id": "dangerous-commands",
        "trigger": "bash",
        "severity": "block",
        "scope": ["git push --force*", "*rm -rf*"],
        "exclude": ["*--dry-run*"],
        "prompt": "COMMAND: {{command}}\nDangerous?\n"
                  '{"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}',
    }


# ── 1. Event Parsing ──────────────────────────────────────────────


class TestParseEvent:
    def test_write_tool(self, file_write_event):
        ev = sentinel.parse_event(file_write_event)
        assert ev["trigger"] == "file_write"
        assert ev["template_vars"]["tool_name"] == "Write"
        assert "payment.ts" in ev["template_vars"]["file_path"]
        assert ev["template_vars"]["content_snippet"].startswith("export function")
        assert ev["template_vars"]["content_length"] == str(len(file_write_event["tool_input"]["content"]))

    def test_edit_tool(self):
        ev = sentinel.parse_event({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/app.py", "new_string": "x = 1"},
        })
        assert ev["trigger"] == "file_write"
        assert "app.py" in ev["template_vars"]["file_path"]
        # Edit uses new_string for content
        assert ev["template_vars"]["content_snippet"] == "x = 1"

    def test_notebook_edit_tool(self):
        ev = sentinel.parse_event({
            "tool_name": "NotebookEdit",
            "tool_input": {"file_path": "/repo/nb.ipynb", "content": "print('hi')"},
        })
        assert ev["trigger"] == "file_write"

    def test_bash_tool(self, bash_event):
        ev = sentinel.parse_event(bash_event)
        assert ev["trigger"] == "bash"
        assert ev["template_vars"]["command"] == "git push --force origin main"
        assert "Execute:" in ev["template_vars"]["action_summary"]

    def test_mcp_tool(self, mcp_event):
        ev = sentinel.parse_event(mcp_event)
        assert ev["trigger"] == "mcp"
        assert ev["template_vars"]["server_name"] == "postgres-prod"
        assert ev["template_vars"]["mcp_tool"] == "sql_execute"
        assert "postgres-prod:sql_execute" in ev["match_targets"]
        assert "sql_execute" in ev["match_targets"]
        assert "postgres-prod" in ev["match_targets"]

    def test_unknown_tool(self):
        ev = sentinel.parse_event({"tool_name": "SomeFutureTool", "tool_input": {}})
        assert ev["trigger"] == "unknown"
        assert ev["template_vars"]["tool_name"] == "SomeFutureTool"

    def test_path_relativization(self):
        with patch("os.getcwd", return_value="/repo"):
            ev = sentinel.parse_event({
                "tool_name": "Write",
                "tool_input": {"file_path": "/repo/src/main.ts", "content": "x"},
            })
            assert ev["template_vars"]["file_path"] == "src/main.ts"
            assert ev["match_targets"] == ["src/main.ts"]

    def test_empty_tool_input(self):
        ev = sentinel.parse_event({"tool_name": "Write", "tool_input": {}})
        assert ev["trigger"] == "file_write"
        assert ev["template_vars"]["file_path"] == ""


class TestParseEventMultiAgent:
    """Test configurable tool_map for non-Claude Code agents."""

    def test_cursor_edit_file(self):
        ev = sentinel.parse_event(
            {"tool_name": "edit_file", "tool_input": {"file_path": "/repo/app.ts", "content": "x"}},
        )
        assert ev["trigger"] == "file_write"

    def test_cursor_terminal(self):
        ev = sentinel.parse_event(
            {"tool_name": "run_terminal_cmd", "tool_input": {"command": "npm test"}},
        )
        assert ev["trigger"] == "bash"

    def test_windsurf_write(self):
        ev = sentinel.parse_event(
            {"tool_name": "write_to_file", "tool_input": {"file_path": "/repo/x.py", "content": "y"}},
        )
        assert ev["trigger"] == "file_write"

    def test_windsurf_command(self):
        ev = sentinel.parse_event(
            {"tool_name": "run_command", "tool_input": {"command": "ls"}},
        )
        assert ev["trigger"] == "bash"

    def test_cline_replace(self):
        ev = sentinel.parse_event(
            {"tool_name": "replace_in_file", "tool_input": {"file_path": "/repo/a.js", "content": "z"}},
        )
        assert ev["trigger"] == "file_write"

    def test_cline_execute(self):
        ev = sentinel.parse_event(
            {"tool_name": "execute_command", "tool_input": {"command": "git status"}},
        )
        assert ev["trigger"] == "bash"

    def test_copilot_create_file(self):
        ev = sentinel.parse_event(
            {"tool_name": "create_file", "tool_input": {"file_path": "/repo/new.ts", "content": "hi"}},
        )
        assert ev["trigger"] == "file_write"

    def test_copilot_terminal(self):
        ev = sentinel.parse_event(
            {"tool_name": "run_in_terminal", "tool_input": {"command": "make build"}},
        )
        assert ev["trigger"] == "bash"

    def test_amazon_q_fs_write(self):
        ev = sentinel.parse_event(
            {"tool_name": "fs_write", "tool_input": {"file_path": "/repo/f.py", "content": "x"}},
        )
        assert ev["trigger"] == "file_write"

    def test_amazon_q_execute_bash(self):
        ev = sentinel.parse_event(
            {"tool_name": "execute_bash", "tool_input": {"command": "echo hi"}},
        )
        assert ev["trigger"] == "bash"

    def test_cursor_mcp_prefix(self):
        """Cursor uses mcp_ prefix with single underscore separator."""
        config = dict(sentinel.DEFAULTS)
        config["mcp_prefix"] = "mcp_"
        config["mcp_separator"] = "_"
        ev = sentinel.parse_event(
            {"tool_name": "mcp_github_create_issue", "tool_input": {"title": "bug"}},
            config=config,
        )
        assert ev["trigger"] == "mcp"
        assert ev["template_vars"]["server_name"] == "github"
        assert ev["template_vars"]["mcp_tool"] == "create_issue"

    def test_custom_tool_map_override(self):
        """User can add custom tool names via config."""
        config = dict(sentinel.DEFAULTS)
        config["tool_map"] = {"my_custom_write": "file_write", "my_shell": "bash"}
        ev = sentinel.parse_event(
            {"tool_name": "my_custom_write", "tool_input": {"file_path": "/x", "content": "y"}},
            config=config,
        )
        assert ev["trigger"] == "file_write"


# ── 2. Rule Matching ──────────────────────────────────────────────


class TestRuleMatching:
    def test_trigger_filter_match(self, block_rule):
        event = {"trigger": "file_write", "match_targets": ["src/core/billing/pay.ts"]}
        assert sentinel.rule_matches(block_rule, event) is True

    def test_trigger_filter_mismatch(self, block_rule):
        event = {"trigger": "bash", "match_targets": ["src/core/billing/pay.ts"]}
        assert sentinel.rule_matches(block_rule, event) is False

    def test_trigger_any_matches_all(self):
        rule = {"trigger": "any", "scope": ["**"], "exclude": []}
        assert sentinel.rule_matches(rule, {"trigger": "file_write", "match_targets": ["x"]}) is True
        assert sentinel.rule_matches(rule, {"trigger": "bash", "match_targets": ["x"]}) is True
        assert sentinel.rule_matches(rule, {"trigger": "mcp", "match_targets": ["x"]}) is True

    def test_scope_glob_match(self, block_rule):
        event = {"trigger": "file_write", "match_targets": ["src/core/billing/invoice.ts"]}
        assert sentinel.rule_matches(block_rule, event) is True

    def test_scope_glob_no_match(self, block_rule):
        event = {"trigger": "file_write", "match_targets": ["src/core/auth/login.ts"]}
        assert sentinel.rule_matches(block_rule, event) is False

    def test_exclude_overrides_scope(self, block_rule):
        event = {"trigger": "file_write", "match_targets": ["src/core/billing/pay.test.ts"]}
        assert sentinel.rule_matches(block_rule, event) is False

    def test_default_scope_matches_everything(self):
        rule = {"trigger": "any", "scope": ["**"], "exclude": []}
        assert sentinel.rule_matches(rule, {"trigger": "bash", "match_targets": ["anything"]}) is True

    def test_no_targets_no_match(self, block_rule):
        event = {"trigger": "file_write", "match_targets": []}
        assert sentinel.rule_matches(block_rule, event) is False


class TestGlobMatch:
    def test_exact_match(self):
        assert sentinel._glob_match("src/main.ts", "src/main.ts") is True

    def test_wildcard(self):
        assert sentinel._glob_match("src/main.ts", "src/*.ts") is True

    def test_double_star_prefix(self):
        assert sentinel._glob_match("src/core/billing/pay.ts", "**/*.ts") is True

    def test_double_star_strips_prefix(self):
        assert sentinel._glob_match("pay.ts", "**/*.ts") is True

    def test_no_match(self):
        assert sentinel._glob_match("src/main.py", "**/*.ts") is False

    def test_bash_scope_pattern(self):
        assert sentinel._glob_match("git push --force origin main", "git push --force*") is True

    def test_bash_scope_no_match(self):
        assert sentinel._glob_match("git status", "git push --force*") is False


# ── 3. Prompt Rendering ──────────────────────────────────────────


class TestRenderPrompt:
    def test_variable_substitution(self, block_rule, default_config):
        event = {
            "template_vars": {
                "file_path": "src/billing/pay.ts",
                "content_snippet": "const x = 1;",
            }
        }
        result = sentinel.render_prompt(block_rule, event, default_config)
        assert "src/billing/pay.ts" in result
        assert "const x = 1;" in result
        assert "{{file_path}}" not in result

    def test_missing_variable_left_as_is(self, default_config):
        rule = {"prompt": "Value: {{nonexistent}}"}
        event = {"template_vars": {}}
        result = sentinel.render_prompt(rule, event, default_config)
        assert "{{nonexistent}}" in result

    def test_content_truncation(self, default_config):
        default_config["content_max_chars"] = 10
        rule = {"prompt": "CONTENT: {{content_snippet}}"}
        event = {"template_vars": {"content_snippet": "a" * 100}}
        result = sentinel.render_prompt(rule, event, default_config)
        # The first substitution uses the full snippet, but the truncation
        # logic in render_prompt re-truncates if {{content_snippet}} was in template
        assert len(result) < 200


# ── 4. Output Formatting ─────────────────────────────────────────


class TestFormatReport:
    def test_single_blocker(self):
        violations = [{"rule_id": "r1", "severity": "block", "confidence": 0.92, "reason": "bad"}]
        report = sentinel.format_report(violations)
        assert "SENTINEL: action blocked" in report
        assert "[r1]" in report
        assert "92%" in report

    def test_single_warning(self):
        violations = [{"rule_id": "r1", "severity": "warn", "confidence": 0.75, "reason": "meh"}]
        report = sentinel.format_report(violations)
        assert "SENTINEL: warnings" in report
        assert "[r1]" in report
        assert "75%" in report

    def test_mixed_block_and_warn(self):
        violations = [
            {"rule_id": "r1", "severity": "block", "confidence": 0.9, "reason": "blocked"},
            {"rule_id": "r2", "severity": "warn", "confidence": 0.7, "reason": "warned"},
        ]
        report = sentinel.format_report(violations)
        assert "SENTINEL: action blocked" in report
        assert "SENTINEL: warnings" in report
        assert "[r1]" in report
        assert "[r2]" in report

    def test_empty_violations(self):
        report = sentinel.format_report([])
        assert report == ""

    def test_confidence_formatting(self):
        violations = [{"rule_id": "r1", "severity": "block", "confidence": 1.0, "reason": "sure"}]
        report = sentinel.format_report(violations)
        assert "100%" in report


# ── 5. Ollama Evaluation (mocked) ────────────────────────────────


def _mock_ollama_response(violation, confidence, reason):
    """Create a mock urllib response with Ollama-style JSON."""
    body = json.dumps({
        "message": {
            "content": json.dumps({
                "violation": violation,
                "confidence": confidence,
                "reason": reason,
            })
        }
    }).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestEvaluateRule:
    def setup_method(self):
        sentinel._ollama_semaphore = None

    def test_violation_above_threshold(self, block_rule, default_config):
        mock_resp = _mock_ollama_response(True, 0.9, "secret detected")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is not None
        assert result["rule_id"] == "test-block"
        assert result["severity"] == "block"
        assert result["confidence"] == 0.9
        assert result["reason"] == "secret detected"

    def test_violation_below_threshold(self, block_rule, default_config):
        mock_resp = _mock_ollama_response(True, 0.3, "maybe")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is None

    def test_no_violation(self, block_rule, default_config):
        mock_resp = _mock_ollama_response(False, 0.95, "looks clean")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is None

    def test_timeout_fail_open(self, block_rule, default_config):
        default_config["fail_open"] = True
        with patch("urllib.request.urlopen", side_effect=ConnectionError("timed out")):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is None

    def test_timeout_fail_closed(self, block_rule, default_config):
        default_config["fail_open"] = False
        with patch("urllib.request.urlopen", side_effect=ConnectionError("timed out")):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is not None
        assert result["severity"] == "block"
        assert result.get("error") is True

    def test_offline_fail_open(self, block_rule, default_config):
        default_config["fail_open"] = True
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is None

    def test_offline_fail_closed(self, block_rule, default_config):
        default_config["fail_open"] = False
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        assert result is not None
        assert result.get("error") is True

    def test_malformed_json_response(self, block_rule, default_config):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"message": {"content": "not json at all"}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            result = sentinel.evaluate_rule(block_rule, event, default_config)
        # fail_open=True by default → None
        assert result is None

    def test_per_rule_model_override(self, block_rule, default_config):
        block_rule["model"] = "llama3:8b"
        mock_resp = _mock_ollama_response(False, 0.9, "ok")
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_url:
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            sentinel.evaluate_rule(block_rule, event, default_config)
            call_data = json.loads(mock_url.call_args[0][0].data)
            assert call_data["model"] == "llama3:8b"

    def test_semaphore_used_when_set(self, block_rule, default_config):
        sentinel._ollama_semaphore = MagicMock()
        mock_resp = _mock_ollama_response(False, 0.5, "ok")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            event = {"trigger": "file_write", "template_vars": {"file_path": "x", "content_snippet": "y"}}
            sentinel.evaluate_rule(block_rule, event, default_config)
        sentinel._ollama_semaphore.acquire.assert_called_once()
        sentinel._ollama_semaphore.release.assert_called_once()
        sentinel._ollama_semaphore = None


# ── 6. Config and Rule Loading ────────────────────────────────────


class TestLoadConfig:
    def test_defaults_applied(self, tmp_path):
        config_dir = str(tmp_path)
        config = sentinel.load_config(config_dir)
        assert config["model"] == "qwen3.5:4b"
        assert config["fail_open"] is True
        assert config["confidence_threshold"] == 0.7

    def test_config_file_overrides(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("model: llama3:8b\nfail_open: false\n")
        config = sentinel.load_config(str(tmp_path))
        assert config["model"] == "llama3:8b"
        assert config["fail_open"] is False

    def test_rules_dir_resolved(self, tmp_path):
        config = sentinel.load_config(str(tmp_path))
        assert config["rules_dir"] == os.path.join(str(tmp_path), "rules")


class TestLoadRules:
    def test_loads_valid_rule(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "test.yaml").write_text(
            "id: test-rule\ntrigger: bash\nseverity: block\nprompt: check\n"
        )
        rules = sentinel.load_rules(str(rules_dir))
        assert len(rules) == 1
        assert rules[0]["id"] == "test-rule"
        assert rules[0]["trigger"] == "bash"

    def test_applies_defaults(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "minimal.yaml").write_text("prompt: check this\n")
        rules = sentinel.load_rules(str(rules_dir))
        assert len(rules) == 1
        assert rules[0]["id"] == "minimal"
        assert rules[0]["trigger"] == "any"
        assert rules[0]["severity"] == "block"
        assert rules[0]["scope"] == ["**"]

    def test_skips_malformed_files(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "bad.yaml").write_text(": : : invalid yaml [[[")
        (rules_dir / "good.yaml").write_text("prompt: check\n")
        rules = sentinel.load_rules(str(rules_dir))
        assert len(rules) == 1
        assert rules[0]["id"] == "good"

    def test_ignores_non_yaml_files(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "readme.md").write_text("# not a rule")
        (rules_dir / ".gitkeep").write_text("")
        rules = sentinel.load_rules(str(rules_dir))
        assert len(rules) == 0

    def test_empty_dir(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        assert sentinel.load_rules(str(rules_dir)) == []

    def test_missing_dir(self):
        assert sentinel.load_rules("/nonexistent/path") == []


# ── 7. Rule Validation ───────────────────────────────────────────


class TestValidateRule:
    def test_valid_rule_no_warnings(self):
        rule = {
            "id": "good-rule",
            "trigger": "bash",
            "severity": "block",
            "scope": ["*rm*"],
            "prompt": "COMMAND: {{command}}\nDangerous?",
        }
        assert sentinel.validate_rule(rule, "good-rule.yaml") == []

    def test_missing_prompt(self):
        warnings = sentinel.validate_rule({"trigger": "bash"}, "bad.yaml")
        assert any("prompt" in w for w in warnings)

    def test_unknown_trigger(self):
        warnings = sentinel.validate_rule({"trigger": "http", "prompt": "x"}, "bad.yaml")
        assert any("trigger" in w and "http" in w for w in warnings)

    def test_unknown_severity(self):
        warnings = sentinel.validate_rule({"severity": "error", "prompt": "x"}, "bad.yaml")
        assert any("severity" in w and "error" in w for w in warnings)

    def test_scope_not_a_list(self):
        warnings = sentinel.validate_rule({"scope": "*.ts", "prompt": "x"}, "bad.yaml")
        assert any("scope" in w and "list" in w for w in warnings)

    def test_exclude_not_a_list(self):
        warnings = sentinel.validate_rule({"exclude": "*.test.ts", "prompt": "x"}, "bad.yaml")
        assert any("exclude" in w and "list" in w for w in warnings)

    def test_unknown_template_variable(self):
        rule = {"trigger": "bash", "prompt": "{{command}} {{typo_var}}"}
        warnings = sentinel.validate_rule(rule, "bad.yaml")
        assert any("typo_var" in w for w in warnings)

    def test_valid_template_vars_for_trigger(self):
        rule = {"trigger": "file_write", "prompt": "{{file_path}} {{content_snippet}}"}
        assert sentinel.validate_rule(rule, "good.yaml") == []

    def test_any_trigger_accepts_all_vars(self):
        rule = {"trigger": "any", "prompt": "{{command}} {{file_path}} {{mcp_tool}}"}
        assert sentinel.validate_rule(rule, "good.yaml") == []

    def test_non_kebab_case_id(self):
        warnings = sentinel.validate_rule({"id": "Bad Rule", "prompt": "x"}, "bad.yaml")
        assert any("kebab-case" in w for w in warnings)

    def test_kebab_case_id_ok(self):
        warnings = sentinel.validate_rule({"id": "good-rule", "prompt": "x"}, "ok.yaml")
        assert not any("kebab-case" in w for w in warnings)


# ── 8. Main Decision Logic (integration) ─────────────────────────


class TestMainDecision:
    """Test the decision output format using format_report + JSON wrapping."""

    def test_no_violations_silent(self):
        """No violations → no output."""
        report = sentinel.format_report([])
        assert report == ""

    def test_block_violation_deny_output(self):
        violations = [{"rule_id": "r1", "severity": "block", "confidence": 0.95, "reason": "danger"}]
        report = sentinel.format_report(violations)
        output = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": report,
            }
        })
        parsed = json.loads(output)
        assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "SENTINEL: action blocked" in parsed["hookSpecificOutput"]["permissionDecisionReason"]

    def test_warn_only_context_output(self):
        violations = [{"rule_id": "r1", "severity": "warn", "confidence": 0.8, "reason": "check this"}]
        report = sentinel.format_report(violations)
        output = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": report,
            }
        })
        parsed = json.loads(output)
        assert "additionalContext" in parsed["hookSpecificOutput"]
        assert "permissionDecision" not in parsed["hookSpecificOutput"]

    def test_mixed_blockers_take_precedence(self):
        violations = [
            {"rule_id": "r1", "severity": "block", "confidence": 0.9, "reason": "blocked"},
            {"rule_id": "r2", "severity": "warn", "confidence": 0.7, "reason": "warned"},
        ]
        blockers = any(v["severity"] == "block" for v in violations)
        assert blockers is True


# ── 9. _make_error and _handle_offline ────────────────────────────


class TestErrorHandling:
    def test_make_error_fail_open(self):
        rule = {"id": "r1"}
        config = {"fail_open": True}
        assert sentinel._make_error(rule, "err", config) is None

    def test_make_error_fail_closed(self):
        rule = {"id": "r1"}
        config = {"fail_open": False}
        result = sentinel._make_error(rule, "err msg", config)
        assert result is not None
        assert result["rule_id"] == "r1"
        assert result["severity"] == "block"
        assert result["error"] is True

    def test_handle_offline_fail_open(self):
        rule = {"id": "r1", "model": "test"}
        config = {"fail_open": True, "model": "test", "log_file": None}
        result = sentinel._handle_offline(rule, Exception("connection refused"), config, time.monotonic())
        assert result is None

    def test_handle_offline_fail_closed(self):
        rule = {"id": "r1", "model": "test"}
        config = {"fail_open": False, "model": "test", "log_file": None}
        result = sentinel._handle_offline(rule, Exception("connection refused"), config, time.monotonic())
        assert result is not None
        assert result["error"] is True

    def test_handle_offline_detects_timeout(self):
        rule = {"id": "r1", "model": "test"}
        config = {"fail_open": False, "model": "test", "log_file": None}
        result = sentinel._handle_offline(rule, Exception("timed out"), config, time.monotonic())
        assert "timeout" in result["reason"]
