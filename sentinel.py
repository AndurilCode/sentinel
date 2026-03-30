#!/usr/bin/env python3
"""
Sentinel — Local LLM rule evaluator for Claude Code hooks.

Runs as a PreToolUse hook. Receives the tool event on stdin,
filters applicable rules by trigger type + glob scope, evaluates
matching rules in parallel against a local Ollama model, and blocks
only on violations. Silent when all rules pass.

Exit codes:
  0  — allow (no violations, or warnings only)
  2  — block (at least one blocking violation)

Dependencies: PyYAML (pip install pyyaml)
"""

import sys
import os
import json
import fnmatch
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ── Defaults ────────────────────────────────────────────────────────

DEFAULTS = {
    "model": "qwen3.5:4b",
    "ollama_url": "http://localhost:11434",
    "timeout_ms": 5000,
    "confidence_threshold": 0.7,
    "max_parallel": 4,
    "think": False,
    "fail_open": True,
    "log_file": None,          # optional JSONL path for Vigil integration
    "content_max_chars": 800,  # truncate file content in prompts
}

SYSTEM_PROMPT = (
    "You are a code review gate. You evaluate whether an agent action "
    "violates a specific repository rule. "
    "Respond ONLY with valid JSON, no other text:\n"
    '{"violation": true|false, "confidence": 0.0-1.0, "reason": "one line"}'
)

# ── Loaders ─────────────────────────────────────────────────────────

def _load_file(path: str) -> dict:
    with open(path, "r") as f:
        if HAS_YAML and path.endswith((".yaml", ".yml")):
            return yaml.safe_load(f) or {}
        return json.load(f)


def load_config(sentinel_dir: str) -> dict:
    cfg = dict(DEFAULTS)
    cfg["rules_dir"] = os.path.join(sentinel_dir, "rules")

    for ext in ("yaml", "yml", "json"):
        p = os.path.join(sentinel_dir, f"config.{ext}")
        if os.path.exists(p):
            cfg.update(_load_file(p))
            break

    # Resolve relative rules_dir to sentinel_dir
    if not os.path.isabs(cfg["rules_dir"]):
        cfg["rules_dir"] = os.path.join(sentinel_dir, cfg["rules_dir"])

    return cfg


def load_rules(rules_dir: str) -> list[dict]:
    rules = []
    if not os.path.isdir(rules_dir):
        return rules
    for entry in sorted(os.listdir(rules_dir)):
        if not entry.endswith((".yaml", ".yml", ".json")):
            continue
        path = os.path.join(rules_dir, entry)
        try:
            rule = _load_file(path)
            rule.setdefault("id", Path(entry).stem)
            rule.setdefault("severity", "block")   # block | warn
            rule.setdefault("trigger", "any")       # file_write | bash | mcp | any
            rule.setdefault("scope", ["**"])
            rule.setdefault("exclude", [])
            rules.append(rule)
        except Exception as e:
            pass  # silently skip malformed rule files
    return rules

# ── Event parsing ───────────────────────────────────────────────────

TOOL_TRIGGER_MAP = {
    "Write":        "file_write",
    "Edit":         "file_write",
    "NotebookEdit": "file_write",
    "Bash":         "bash",
    # MCP tools are detected by prefix in parse_event(), not mapped here.
}


def parse_event(data: dict) -> dict:
    """Normalize Claude Code hook payload into evaluation context."""
    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})

    # Detect MCP tools by prefix (mcp__<server>__<tool>)
    if tool.startswith("mcp__"):
        trigger = "mcp"
    else:
        trigger = TOOL_TRIGGER_MAP.get(tool, "unknown")

    ev = {
        "raw_tool":       tool,
        "trigger":        trigger,
        "tool_input":     inp,
        "match_targets":  [],   # list of strings to match against scope globs
        "template_vars":  {},   # available to {{var}} in rule prompts
    }

    if ev["trigger"] == "file_write":
        fp = inp.get("file_path", "")
        content = inp.get("content", inp.get("new_string", ""))
        ev["match_targets"] = [fp]
        ev["template_vars"] = {
            "file_path":      fp,
            "content_snippet": content[:DEFAULTS["content_max_chars"]],
            "content_length":  str(len(content)),
            "action_summary":  f"Write {len(content)} chars to {fp}",
        }

    elif ev["trigger"] == "bash":
        cmd = inp.get("command", "")
        ev["match_targets"] = [cmd]
        ev["template_vars"] = {
            "command":         cmd[:1000],
            "action_summary":  f"Execute: {cmd[:200]}",
        }

    elif ev["trigger"] == "mcp":
        # Parse server and tool from tool_name: mcp__<server>__<tool>
        parts    = tool.split("__", 2)
        server   = parts[1] if len(parts) > 1 else ""
        mcp_tool = parts[2] if len(parts) > 2 else ""
        args     = json.dumps(inp)[:500]
        composite = f"{server}:{mcp_tool}" if server else mcp_tool
        ev["match_targets"] = [composite, mcp_tool, server]
        ev["template_vars"] = {
            "server_name":    server,
            "mcp_tool":       mcp_tool,
            "mcp_arguments":  args,
            "action_summary": f"MCP {composite}",
        }

    else:
        ev["match_targets"] = [tool]
        ev["template_vars"] = {
            "action_summary": f"Unknown tool: {tool}",
        }

    # Common vars
    ev["template_vars"]["tool_name"] = tool
    ev["template_vars"]["trigger"]   = ev["trigger"]

    return ev

# ── Rule matching ───────────────────────────────────────────────────

def rule_matches(rule: dict, event: dict) -> bool:
    """Check trigger type + scope glob against event targets."""
    # 1. Trigger filter
    rt = rule.get("trigger", "any")
    if rt != "any" and rt != event["trigger"]:
        return False

    # 2. Scope — at least one glob must match at least one target
    scopes   = rule.get("scope", ["**"])
    excludes = rule.get("exclude", [])
    targets  = event.get("match_targets", [])

    for target in targets:
        # Check excludes first
        if any(fnmatch.fnmatch(target, ex) for ex in excludes):
            continue
        if any(fnmatch.fnmatch(target, pat) for pat in scopes):
            return True

    return False

# ── Prompt rendering ────────────────────────────────────────────────

def render_prompt(rule: dict, event: dict, config: dict) -> str:
    template = rule.get("prompt", "Evaluate this action against the rule.")
    result = template
    for key, val in event["template_vars"].items():
        result = result.replace("{{" + key + "}}", str(val))
    # Truncate content if config overrides default
    max_chars = config.get("content_max_chars", DEFAULTS["content_max_chars"])
    if "{{content_snippet}}" in template:
        content = event["template_vars"].get("content_snippet", "")[:max_chars]
        result = result.replace("{{content_snippet}}", content)
    return result

# ── Ollama evaluation ───────────────────────────────────────────────

def evaluate_rule(rule: dict, event: dict, config: dict) -> Optional[dict]:
    """
    Call Ollama for single-rule binary evaluation.
    Returns a violation dict if KO, None if OK.
    """
    prompt = render_prompt(rule, event, config)

    # Allow per-rule model override
    model = rule.get("model", config["model"])

    # think: false disables internal chain-of-thought in thinking models
    # (e.g. qwen3.5). Without this, the model burns all tokens on hidden
    # reasoning and returns empty content. Set think: true in config for
    # higher accuracy at the cost of latency.
    think = config.get("think", False)

    payload = json.dumps({
        "model":  model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "format": "json",
        "stream": False,
        "think":  think,
        "options": {
            "num_predict": 150 if not think else 1000,
            "temperature": 0.1,
        },
    }).encode()

    url = f"{config['ollama_url']}/api/chat"
    timeout_s = config["timeout_ms"] / 1000
    t0 = time.monotonic()

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())

        content = body.get("message", {}).get("content", "")
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Extract JSON — handle stray text around it
        js, je = content.find("{"), content.rfind("}") + 1
        if js < 0 or je <= js:
            return _make_error(rule, f"Unparseable: {content[:120]}", config)

        ev = json.loads(content[js:je])
        violation  = bool(ev.get("violation", False))
        confidence = float(ev.get("confidence", 0.5))
        reason     = ev.get("reason", "no reason")

        _log(config, rule, event, violation, confidence, reason, elapsed_ms)

        if violation and confidence >= config["confidence_threshold"]:
            return {
                "rule_id":    rule["id"],
                "severity":   rule.get("severity", "block"),
                "confidence": confidence,
                "reason":     reason,
            }
        return None

    except (urllib.error.URLError, ConnectionError) as e:
        return _handle_offline(rule, e, config)
    except Exception as e:
        return _handle_offline(rule, e, config)


def _make_error(rule, msg, config):
    if config.get("fail_open", True):
        return None
    return {
        "rule_id":    rule["id"],
        "severity":   "block",
        "confidence": 1.0,
        "reason":     msg,
        "error":      True,
    }


def _handle_offline(rule, exc, config):
    if config.get("fail_open", True):
        return None
    return {
        "rule_id":    rule["id"],
        "severity":   "block",
        "confidence": 1.0,
        "reason":     f"Sentinel offline: {exc}",
        "error":      True,
    }

# ── Logging (JSONL for Vigil) ───────────────────────────────────────

def _log(config, rule, event, violation, confidence, reason, elapsed_ms):
    log_path = config.get("log_file")
    if not log_path:
        return
    entry = {
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule_id":    rule["id"],
        "trigger":    event["trigger"],
        "target":     (event.get("match_targets") or [""])[0][:200],
        "violation":  violation,
        "confidence": confidence,
        "reason":     reason,
        "elapsed_ms": elapsed_ms,
        "model":      rule.get("model", config["model"]),
    }
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _err(msg):
    """Log to JSONL if configured, otherwise discard. Never write to stderr
    for non-violation messages — stderr output gets fed back to the agent."""
    pass  # Errors are logged via _log(); _err is for debug only


def _debug(msg, config):
    """Write debug info to the log file if configured, never to stderr."""
    log_path = config.get("log_file") if config else None
    if not log_path:
        return
    entry = {
        "ts":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": "debug",
        "msg":   msg,
    }
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

# ── Output formatting ──────────────────────────────────────────────

def format_report(violations: list[dict]) -> str:
    blockers = [v for v in violations if v["severity"] == "block"]
    warnings = [v for v in violations if v["severity"] == "warn"]
    lines = []

    if blockers:
        lines.append("SENTINEL: action blocked\n")
        for v in blockers:
            lines.append(f"  [{v['rule_id']}] ({v['confidence']:.0%}) {v['reason']}")

    if warnings:
        if blockers:
            lines.append("")
        lines.append("SENTINEL: warnings\n")
        for v in warnings:
            lines.append(f"  [{v['rule_id']}] ({v['confidence']:.0%}) {v['reason']}")

    return "\n".join(lines)

# ── Main ────────────────────────────────────────────────────────────

def _find_config_dir() -> Optional[str]:
    """Resolve repo-side config directory.

    Priority: SENTINEL_CONFIG_DIR env var > walk-up search for .claude/sentinel/
    """
    from_env = os.environ.get("SENTINEL_CONFIG_DIR")
    if from_env and os.path.isdir(from_env):
        return from_env

    cwd = os.getcwd()
    while True:
        candidate = os.path.join(cwd, ".claude", "sentinel")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent

    return None


def main():
    config_dir = _find_config_dir()
    if not config_dir:
        sys.exit(0)  # no sentinel config found, pass through

    config = load_config(config_dir)
    rules  = load_rules(config["rules_dir"])
    if not rules:
        sys.exit(0)

    # Pre-flight: check Ollama is reachable before doing any work.
    # Avoids N timeouts per rule when Ollama is simply not running.
    try:
        req = urllib.request.Request(f"{config['ollama_url']}/api/tags")
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        if config.get("fail_open", True):
            sys.exit(0)  # Ollama offline → silent pass-through
        else:
            print("SENTINEL: Ollama is not reachable, blocking action (fail_open: false)", file=sys.stderr)
            sys.exit(2)

    # Read hook event
    try:
        event_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)  # unparseable → fail open

    event = parse_event(event_data)

    # Filter applicable rules
    matching = [r for r in rules if rule_matches(r, event)]
    if not matching:
        sys.exit(0)

    # Parallel evaluation
    violations = []
    with ThreadPoolExecutor(max_workers=config["max_parallel"]) as pool:
        futures = {
            pool.submit(evaluate_rule, r, event, config): r
            for r in matching
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                violations.append(result)

    if not violations:
        sys.exit(0)  # all clear, silent

    report = format_report(violations)
    blockers = any(v["severity"] == "block" for v in violations)

    if blockers:
        # Block: stderr message is fed back to Claude as the reason
        print(report, file=sys.stderr)
        sys.exit(2)
    else:
        # Warnings only: structured JSON so Claude Code injects into context
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": report,
            }
        }))
        sys.exit(0)


if __name__ == "__main__":
    main()
