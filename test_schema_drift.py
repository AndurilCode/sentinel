"""Schema drift detection — ensures reference.md and skills stay in sync with code.

These tests extract the source-of-truth schema values from Python modules
(DEFAULTS, VALID_TRIGGERS, VALID_SEVERITIES, TEMPLATE_VARS, SCRIBE_DEFAULTS,
context config) and verify that the documentation files contain all of them.

Run with: pytest test_schema_drift.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import sentinel
import sentinel_scribe

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Helpers ────────────────────────────────────────────────────────


def _read(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath)) as f:
        return f.read()


def _flatten_keys(d: dict, prefix: str = "") -> set[str]:
    """Flatten nested dict keys with dot notation, skipping dict-valued leaves."""
    keys = set()
    for k, v in d.items():
        full = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


# ── Fixtures ───────────────────────────────────────────────────────

REFERENCE_MD = _read("docs/reference.md")
SENTINEL_RULE_SKILL = _read("skills/sentinel-rule/SKILL.md")
SENTINEL_CONFIG_SKILL = _read("skills/sentinel-config/SKILL.md")


# ── Config key tests ───────────────────────────────────────────────


class TestConfigKeysInReference:
    """Every config key from DEFAULTS must appear in reference.md."""

    # Top-level DEFAULTS (sentinel.py) — skip compound keys like tool_map
    TOP_LEVEL_KEYS = {k for k in sentinel.DEFAULTS if k != "tool_map"}

    @pytest.mark.parametrize("key", sorted(TOP_LEVEL_KEYS))
    def test_top_level_key_in_reference(self, key):
        assert f"`{key}`" in REFERENCE_MD, (
            f"Config key '{key}' from sentinel.DEFAULTS missing in docs/reference.md"
        )

    @pytest.mark.parametrize("key", sorted(TOP_LEVEL_KEYS))
    def test_top_level_key_in_config_skill(self, key):
        assert f"`{key}`" in SENTINEL_CONFIG_SKILL, (
            f"Config key '{key}' from sentinel.DEFAULTS missing in sentinel-config SKILL.md"
        )


class TestScribeConfigInReference:
    """Every scribe config key must appear in reference.md and config skill."""

    SCRIBE_KEYS = _flatten_keys(sentinel_scribe.SCRIBE_DEFAULTS, "scribe")

    @pytest.mark.parametrize("key", sorted(SCRIBE_KEYS))
    def test_scribe_key_in_reference(self, key):
        assert f"`{key}`" in REFERENCE_MD, (
            f"Scribe config key '{key}' from SCRIBE_DEFAULTS missing in docs/reference.md"
        )

    @pytest.mark.parametrize("key", sorted(SCRIBE_KEYS))
    def test_scribe_key_in_config_skill(self, key):
        assert f"`{key}`" in SENTINEL_CONFIG_SKILL, (
            f"Scribe config key '{key}' from SCRIBE_DEFAULTS missing in sentinel-config SKILL.md"
        )


class TestContextConfigInReference:
    """Every context config key must appear in reference.md and config skill."""

    # Extract context sub-config keys from sentinel_context.load_config defaults
    _CTX_DEFAULTS = {"enabled": True, "model": "gemma3:4b", "min_events": 3,
                     "lock_timeout_s": 30, "summary_max_words": 150}
    CONTEXT_KEYS = {f"context.{k}" for k in _CTX_DEFAULTS}

    @pytest.mark.parametrize("key", sorted(CONTEXT_KEYS))
    def test_context_key_in_reference(self, key):
        assert f"`{key}`" in REFERENCE_MD, (
            f"Context config key '{key}' missing in docs/reference.md"
        )

    @pytest.mark.parametrize("key", sorted(CONTEXT_KEYS))
    def test_context_key_in_config_skill(self, key):
        assert f"`{key}`" in SENTINEL_CONFIG_SKILL, (
            f"Context config key '{key}' missing in sentinel-config SKILL.md"
        )


# ── Trigger / severity / template var tests ────────────────────────


class TestTriggersAndSeverities:
    """VALID_TRIGGERS and VALID_SEVERITIES must be documented."""

    @pytest.mark.parametrize("trigger", sorted(sentinel.VALID_TRIGGERS))
    def test_trigger_in_reference(self, trigger):
        assert trigger in REFERENCE_MD, (
            f"Trigger '{trigger}' missing in docs/reference.md"
        )

    @pytest.mark.parametrize("trigger", sorted(sentinel.VALID_TRIGGERS))
    def test_trigger_in_rule_skill(self, trigger):
        assert trigger in SENTINEL_RULE_SKILL, (
            f"Trigger '{trigger}' missing in sentinel-rule SKILL.md"
        )

    @pytest.mark.parametrize("severity", sorted(sentinel.VALID_SEVERITIES))
    def test_severity_in_reference(self, severity):
        assert severity in REFERENCE_MD, (
            f"Severity '{severity}' missing in docs/reference.md"
        )

    @pytest.mark.parametrize("severity", sorted(sentinel.VALID_SEVERITIES))
    def test_severity_in_rule_skill(self, severity):
        assert severity in SENTINEL_RULE_SKILL, (
            f"Severity '{severity}' missing in sentinel-rule SKILL.md"
        )


class TestTemplateVars:
    """All template variables must be documented in reference.md and rule skill."""

    ALL_VARS = sentinel.ALL_TEMPLATE_VARS | sentinel.POST_TEMPLATE_VARS

    @pytest.mark.parametrize("var", sorted(ALL_VARS))
    def test_template_var_in_reference(self, var):
        pattern = f"{{{{{var}}}}}"  # {{var}}
        assert pattern in REFERENCE_MD, (
            f"Template variable '{{{{{var}}}}}' missing in docs/reference.md"
        )

    @pytest.mark.parametrize("var", sorted(ALL_VARS))
    def test_template_var_in_rule_skill(self, var):
        pattern = f"{{{{{var}}}}}"  # {{var}}
        assert pattern in SENTINEL_RULE_SKILL, (
            f"Template variable '{{{{{var}}}}}' missing in sentinel-rule SKILL.md"
        )


# ── Rule field tests ───────────────────────────────────────────────


class TestRuleFields:
    """Rule fields accepted by validate_rule must be documented."""

    # Fields that validate_rule checks or load_rules sets defaults for
    RULE_FIELDS = {"id", "trigger", "severity", "post", "scope", "exclude",
                   "prompt", "model"}

    @pytest.mark.parametrize("field", sorted(RULE_FIELDS))
    def test_rule_field_in_reference(self, field):
        assert f"`{field}`" in REFERENCE_MD or f"{field}:" in REFERENCE_MD, (
            f"Rule field '{field}' missing in docs/reference.md"
        )

    @pytest.mark.parametrize("field", sorted(RULE_FIELDS))
    def test_rule_field_in_rule_skill(self, field):
        assert f"{field}:" in SENTINEL_RULE_SKILL, (
            f"Rule field '{field}' missing in sentinel-rule SKILL.md"
        )


# ── Existing rules validation ─────────────────────────────────────


class TestExistingRulesValid:
    """All existing rules in the repo must pass validate_rule."""

    RULES_DIR = os.path.join(ROOT, ".claude", "sentinel", "rules")
    EXAMPLES_DIR = os.path.join(ROOT, "examples")

    @staticmethod
    def _collect_rule_files(directory: str) -> list[str]:
        if not os.path.isdir(directory):
            return []
        return [
            os.path.join(directory, f)
            for f in sorted(os.listdir(directory))
            if f.endswith((".yaml", ".yml"))
        ]

    @pytest.mark.parametrize("rule_path",
                             _collect_rule_files(RULES_DIR) +
                             _collect_rule_files(EXAMPLES_DIR),
                             ids=lambda p: os.path.basename(p))
    def test_rule_validates(self, rule_path):
        import yaml
        with open(rule_path) as f:
            rule = yaml.safe_load(f)
        warnings = sentinel.validate_rule(rule, rule_path)
        assert warnings == [], f"Validation warnings: {warnings}"


# ── Tool map coverage ──────────────────────────────────────────────


class TestToolMapInReference:
    """All agents in the default tool_map should be documented in reference.md."""

    # Extract unique trigger mappings (tool_name -> trigger)
    TOOL_NAMES = set(sentinel.DEFAULTS["tool_map"].keys())

    def test_all_tool_names_documented(self):
        """At least mention each tool name in the docs."""
        missing = []
        for tool in sorted(self.TOOL_NAMES):
            if tool not in REFERENCE_MD:
                missing.append(tool)
        assert not missing, (
            f"Tool map entries missing from docs/reference.md: {missing}"
        )
