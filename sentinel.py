#!/usr/bin/env python3
"""
Sentinel — Local LLM rule evaluator for coding agent hooks.

Runs as a PreToolUse hook. Receives the tool event on stdin,
filters applicable rules by trigger type + glob scope, evaluates
matching rules in parallel against a local Ollama model, and blocks
only on violations. Silent when all rules pass.

Supports multiple agents via configurable tool_map: Claude Code,
Copilot, Cursor, Windsurf, Cline, Amazon Q, and custom agents.

Exit codes:
  0  — always (hook output controls blocking via permissionDecision JSON)

Dependencies: PyYAML (pip install pyyaml)
"""

import sys
import os
import json
import fnmatch
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from sentinel_backends import call_llm, resolve_backend, backend_reachable, init_ollama_semaphore


# Lazy import for draft notifications (only when needed)
def _check_scribe_drafts(session_id: str, config: dict, config_dir: str) -> Optional[str]:
    """Check for pending Scribe drafts — returns notification or None."""
    try:
        from sentinel_scribe import check_pending_drafts
        scribe_cfg = config.get("scribe", {})
        if not scribe_cfg.get("enabled", True):
            return None
        drafts_dir = os.path.join(config_dir, "drafts")
        session_d = _session_dir(session_id, config)
        max_age = scribe_cfg.get("notification", {}).get("max_age_days", 7)
        return check_pending_drafts(drafts_dir, session_d, max_age)
    except ImportError:
        return None


try:
    import yaml
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "pyyaml"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import yaml

# ── Defaults ────────────────────────────────────────────────────────

DEFAULTS = {
    "model": "gemma3:4b",
    "ollama_url": "http://localhost:11434",
    "timeout_ms": 5000,
    "confidence_threshold": 0.7,
    "max_parallel": 4,
    "ollama_concurrency": 1,    # actual concurrent Ollama calls (GPU bound)
    "think": False,
    "fail_open": True,
    "log_file": None,          # optional JSONL path for Vigil integration
    "content_max_chars": 800,  # truncate file content in prompts
    # Tool-to-trigger mapping (override for non-Claude Code agents)
    # Keys are exact tool_name strings from the hook payload.
    # Values must be one of: file_write, bash, mcp
    "tool_map": {
        # Claude Code (default)
        "Write":        "file_write",
        "Edit":         "file_write",
        "MultiEdit":    "file_write",
        "NotebookEdit": "file_write",
        "Bash":         "bash",
        # Copilot (VS Code agent mode)
        "create_file":             "file_write",
        "replace_string_in_file":  "file_write",
        "multi_replace_string_in_file": "file_write",
        "run_in_terminal":         "bash",
        # Cursor
        "edit_file":        "file_write",
        "run_terminal_cmd": "bash",
        # Windsurf
        "write_to_file": "file_write",
        # "edit_file" already mapped above (Cursor)
        "run_command":   "bash",
        # Cline
        # "write_to_file" already mapped above (Windsurf)
        "replace_in_file":  "file_write",
        "execute_command":  "bash",
        # Amazon Q CLI
        "fs_write":      "file_write",
        "execute_bash":  "bash",
    },
    # MCP tool detection: prefix and separator for parsing server/tool names.
    # Claude Code: mcp__server__tool  →  prefix="mcp__", separator="__"
    # Cursor:      mcp_server_tool    →  prefix="mcp_",  separator="_"
    "mcp_prefix": "mcp__",
    "mcp_separator": "__",
}

SYSTEM_PROMPT = (
    "You are a code review gate. You evaluate whether an agent action "
    "violates a specific repository rule. "
    "Respond ONLY with valid JSON, no other text:\n"
    '{"violation": true|false, "confidence": 0.0-1.0, "reason": "one line"}'
)

# ── Rule validation ────────────────────────────────────────────────

VALID_TRIGGERS = {"file_write", "bash", "mcp", "any"}
VALID_SEVERITIES = {"block", "warn", "info"}

TEMPLATE_VARS_BY_TRIGGER = {
    "file_write": {"file_path", "content_snippet", "content_length", "action_summary", "tool_name", "trigger"},
    "bash":       {"command", "action_summary", "tool_name", "trigger"},
    "mcp":        {"server_name", "mcp_tool", "mcp_arguments", "action_summary", "tool_name", "trigger"},
}
ALL_TEMPLATE_VARS = set().union(*TEMPLATE_VARS_BY_TRIGGER.values())
POST_TEMPLATE_VARS = {"tool_output", "session_context"}  # additional vars for info post rules


def validate_rule(rule: dict, filepath: str) -> list[str]:
    """Validate a rule dict and return a list of warning messages (empty if valid)."""
    warnings = []
    fname = os.path.basename(filepath)

    # 1. Required: prompt
    if "prompt" not in rule:
        warnings.append(f"{fname}: missing required 'prompt' field")

    # 2. Trigger type
    trigger = rule.get("trigger")
    if trigger is not None and trigger not in VALID_TRIGGERS:
        warnings.append(f"{fname}: unknown trigger '{trigger}' (valid: {', '.join(sorted(VALID_TRIGGERS))})")

    # 3. Severity
    severity = rule.get("severity")
    if severity is not None and severity not in VALID_SEVERITIES:
        warnings.append(f"{fname}: unknown severity '{severity}' (valid: {', '.join(sorted(VALID_SEVERITIES))})")

    # 3b. post: true only valid with severity: info
    if rule.get("post") and severity and severity != "info":
        warnings.append(f"{fname}: 'post: true' is only valid with severity: info")

    # 4. Scope must be a list
    scope = rule.get("scope")
    if scope is not None and not isinstance(scope, list):
        warnings.append(f"{fname}: 'scope' must be a list of glob patterns, got {type(scope).__name__}")

    # 5. Exclude must be a list
    exclude = rule.get("exclude")
    if exclude is not None and not isinstance(exclude, list):
        warnings.append(f"{fname}: 'exclude' must be a list of glob patterns, got {type(exclude).__name__}")

    # 6. Template variable typos
    prompt = rule.get("prompt", "")
    if prompt:
        used_vars = set(re.findall(r"\{\{(\w+)\}\}", prompt))
        trigger_val = rule.get("trigger", "any")
        if trigger_val == "any" or trigger_val not in TEMPLATE_VARS_BY_TRIGGER:
            valid_vars = ALL_TEMPLATE_VARS
        else:
            valid_vars = TEMPLATE_VARS_BY_TRIGGER[trigger_val]
        # Info post rules can use additional template vars
        if rule.get("severity") == "info" and rule.get("post"):
            valid_vars = valid_vars | POST_TEMPLATE_VARS
        unknown = used_vars - valid_vars
        if unknown:
            warnings.append(f"{fname}: unknown template variable(s): {', '.join(sorted(unknown))}")

    # 7. ID format
    rule_id = rule.get("id")
    if rule_id is not None and (re.search(r"[A-Z\s]", rule_id)):
        warnings.append(f"{fname}: id '{rule_id}' should be kebab-case (no spaces or uppercase)")

    return warnings


# ── Loaders ─────────────────────────────────────────────────────────

def _load_file(path: str) -> dict:
    with open(path, "r") as f:
        if path.endswith((".yaml", ".yml")):
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
            # Validate before applying defaults so we catch user mistakes
            for w in validate_rule(rule, path):
                sys.stderr.write(f"SENTINEL: {w}\n")
            rule.setdefault("id", Path(entry).stem)
            rule.setdefault("severity", "block")   # block | warn
            rule.setdefault("trigger", "any")       # file_write | bash | mcp | any
            rule.setdefault("scope", ["**"])
            rule.setdefault("exclude", [])
            rules.append(rule)
        except Exception:
            sys.stderr.write(f"SENTINEL: {entry}: failed to parse (skipped)\n")
    return rules

# ── Input normalization ─────────────────────────────────────────────

def normalize_input(data: dict) -> tuple[dict, str]:
    """Normalize hook payloads from different agents into a common format.

    Returns (normalized_data, agent_format) where agent_format is used
    later to produce the correct output structure.

    Claude Code: {"tool_name": "Write", "tool_input": {...}}
    Copilot CLI: {"toolName": "bash", "toolArgs": "{...}"}  (toolArgs is a JSON string)
    """
    if "tool_name" in data:
        return data, "claude_code"

    if "toolName" in data:
        tool_args = data.get("toolArgs", "{}")
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except (json.JSONDecodeError, TypeError):
                tool_args = {}
        return {"tool_name": data["toolName"], "tool_input": tool_args}, "copilot"

    # Unknown format — pass through, best effort
    return data, "unknown"


# ── Content sampling ───────────────────────────────────────────────

# Patterns that hint at secrets, credentials, or sensitive content.
# Used to find suspicious regions in content that falls outside the
# head/tail window, so the LLM can evaluate them.
_SUSPICIOUS_RE = re.compile(
    r"(?i)"
    r"(?:api[_-]?key|secret[_-]?key|password|passwd|token|credential"
    r"|auth[_-]?token|access[_-]?key|private[_-]?key|client[_-]?secret"
    r"|connection[_-]?string|bearer)\s*[:=]"
    r"|['\"][A-Za-z0-9+/]{40,}['\"]"          # long base64-ish strings
    r"|sk-[a-zA-Z0-9]{20,}"                    # OpenAI-style keys
    r"|ghp_[a-zA-Z0-9]{36}"                    # GitHub PATs
    r"|AKIA[0-9A-Z]{16}"                       # AWS access key IDs
)


def _smart_truncate(content: str, max_chars: int) -> str:
    """Sample content intelligently instead of a blind prefix truncation.

    Strategy:
    - If content fits in max_chars, return it as-is.
    - Otherwise, allocate budget: 60% head, 25% tail, 15% suspicious
      regions found via regex in the middle.
    - This ensures secrets/credentials that appear later in a file are
      still visible to the evaluating LLM.
    """
    if len(content) <= max_chars:
        return content

    head_budget = int(max_chars * 0.60)
    tail_budget = int(max_chars * 0.25)
    mid_budget  = max_chars - head_budget - tail_budget

    head = content[:head_budget]
    tail = content[-tail_budget:]

    # Scan the middle region for suspicious patterns
    middle = content[head_budget:-tail_budget] if tail_budget else content[head_budget:]
    mid_snippet = ""
    if mid_budget > 0 and middle:
        hits = list(_SUSPICIOUS_RE.finditer(middle))
        if hits:
            # Collect context around each hit, up to mid_budget
            fragments = []
            remaining = mid_budget
            for m in hits:
                if remaining <= 0:
                    break
                # 40 chars before match, match itself, 80 chars after
                start = max(0, m.start() - 40)
                end = min(len(middle), m.end() + 80)
                frag = middle[start:end]
                if len(frag) > remaining:
                    frag = frag[:remaining]
                fragments.append(frag)
                remaining -= len(frag) + 5  # 5 for separator
            mid_snippet = " ... ".join(fragments)

    if mid_snippet:
        return f"{head}\n[... middle content, suspicious regions:]\n{mid_snippet}\n[... end of file:]\n{tail}"
    else:
        return f"{head}\n[... {len(content) - head_budget - tail_budget} chars omitted ...]\n{tail}"


# ── Event parsing ───────────────────────────────────────────────────

def _relativize(path: str) -> str:
    """Strip cwd prefix from absolute paths so relative scope globs match."""
    if not os.path.isabs(path):
        return path
    try:
        return os.path.relpath(path, os.getcwd())
    except ValueError:
        # Windows: relpath fails across drives
        return path


def parse_event(data: dict, config: Optional[dict] = None) -> dict:
    """Normalize hook payload into evaluation context.

    Uses config["tool_map"] for trigger detection and config["mcp_prefix"]
    / config["mcp_separator"] for MCP tool parsing. Falls back to DEFAULTS
    when config is None (backwards-compatible).
    """
    cfg = config or DEFAULTS
    tool_map = cfg.get("tool_map", DEFAULTS["tool_map"])
    mcp_prefix = cfg.get("mcp_prefix", DEFAULTS["mcp_prefix"])
    mcp_separator = cfg.get("mcp_separator", DEFAULTS["mcp_separator"])

    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})

    # Detect MCP tools by configurable prefix
    if tool.startswith(mcp_prefix):
        trigger = "mcp"
    else:
        trigger = tool_map.get(tool, "unknown")

    ev = {
        "raw_tool":       tool,
        "trigger":        trigger,
        "tool_input":     inp,
        "match_targets":  [],   # list of strings to match against scope globs
        "template_vars":  {},   # available to {{var}} in rule prompts
    }

    if ev["trigger"] == "file_write":
        fp = inp.get("file_path", "")
        # Relativize absolute paths so scope globs like "src/**" work
        fp_rel = _relativize(fp)
        content = inp.get("content", inp.get("new_string", ""))
        max_chars = cfg.get("content_max_chars", DEFAULTS["content_max_chars"])
        snippet = _smart_truncate(content, max_chars)
        ev["match_targets"] = [fp_rel]
        ev["template_vars"] = {
            "file_path":      fp_rel,
            "content_snippet": snippet,
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
        # Parse server and tool using configurable separator
        remainder = tool[len(mcp_prefix):]
        parts = remainder.split(mcp_separator, 1)
        server   = parts[0]
        mcp_tool = parts[1] if len(parts) > 1 else ""
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

def _glob_match(target: str, pattern: str) -> bool:
    """fnmatch with ** support: strip leading **/ and retry on relative paths."""
    if fnmatch.fnmatch(target, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(target, pattern[3:])
    return False


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
        if any(_glob_match(target, ex) for ex in excludes):
            continue
        if any(_glob_match(target, pat) for pat in scopes):
            return True

    return False

# ── Prompt rendering ────────────────────────────────────────────────

def render_prompt(rule: dict, event: dict, config: dict) -> str:
    template = rule.get("prompt", "Evaluate this action against the rule.")
    result = template
    for key, val in event["template_vars"].items():
        result = result.replace("{{" + key + "}}", str(val))
    return result

# ── LLM evaluation ─────────────────────────────────────────────────


def _fail(rule: dict, reason: str, config: dict,
          event: dict = None, elapsed_ms: int = 0) -> Optional[dict]:
    """Return None (fail-open) or a block dict (fail-closed).

    Unified error path for unparseable responses, timeouts, and offline.
    """
    if event:
        _log(config, rule, event, violation=False, confidence=0.0,
             reason=reason, elapsed_ms=elapsed_ms, level="skipped")
    if config.get("fail_open", True):
        return None
    return {
        "rule_id":    rule["id"],
        "severity":   "block",
        "confidence": 1.0,
        "reason":     reason,
        "error":      True,
    }


def evaluate_rule(rule: dict, event: dict, config: dict) -> Optional[dict]:
    """Call Ollama for single-rule binary evaluation.

    Returns a violation dict if KO, None if OK.
    """
    prompt = render_prompt(rule, event, config)
    backend, model = resolve_backend(config, rule.get("backend"), rule.get("model"))
    t0 = time.monotonic()

    try:
        content = call_llm(prompt, SYSTEM_PROMPT, model, backend, config,
                           think=config.get("think", False))
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        is_timeout = "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower()
        error_type = "timeout" if is_timeout else "offline"
        return _fail(rule, f"Sentinel {error_type}: {e}", config, event, elapsed_ms)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Extract JSON — handle stray text around it
    js, je = content.find("{"), content.rfind("}") + 1
    if js < 0 or je <= js:
        return _fail(rule, f"Unparseable: {content[:120]}", config, event, elapsed_ms)

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

# ── Logging (JSONL for Vigil) ───────────────────────────────────────

def _log(config, rule, event, violation, confidence, reason, elapsed_ms,
         *, level="eval"):
    log_path = config.get("log_file")
    if not log_path:
        return
    tvars = event.get("template_vars", {})
    severity = rule.get("severity", "block")
    threshold = config.get("confidence_threshold", DEFAULTS["confidence_threshold"])
    blocked = violation and confidence >= threshold and severity == "block"
    entry = {
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level":      level,
        "rule_id":    rule["id"],
        "severity":   severity,
        "tool":       event.get("raw_tool", ""),
        "trigger":    event["trigger"],
        "target":     (event.get("match_targets") or [""])[0][:200],
        "action":     tvars.get("action_summary", "")[:200],
        "violation":  violation,
        "confidence": confidence,
        "threshold":  threshold,
        "blocked":    blocked,
        "reason":     reason,
        "elapsed_ms": elapsed_ms,
        "model":      rule.get("model", config["model"]),
        "content":    tvars.get("content_snippet", tvars.get("command", ""))[:400],
    }
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


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


def format_decision(report: str, blockers: bool, agent_format: str) -> Optional[str]:
    """Format the decision JSON for the calling agent's expected structure.

    Claude Code: {"hookSpecificOutput": {"hookEventName": "PreToolUse", ...}}
    Copilot CLI: {"permissionDecision": "deny", "permissionDecisionReason": "..."}
    """
    if agent_format == "copilot":
        # Copilot has no additionalContext equivalent — use allow + reason
        decision = "deny" if blockers else "allow"
        return json.dumps({
            "permissionDecision": decision,
            "permissionDecisionReason": report,
        })

    # Claude Code (default) and unknown formats
    if blockers:
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": report,
            }
        })
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": report,
        }
    })


def format_decision_info(context: str, agent_format: str) -> str:
    """Format info-only output — always additionalContext, never deny."""
    if agent_format == "copilot":
        return json.dumps({
            "permissionDecision": "allow",
            "permissionDecisionReason": context,
        })
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    })


# ── Session helpers ─────────────────────────────────────────────────

def _sanitize_session_id(session_id: str) -> str:
    """Strip path separators and restrict to safe characters."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', session_id)


def _project_root() -> str:
    """Find project root by walking up to find .claude/ directory."""
    cwd = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(cwd, ".claude")):
            return cwd
        parent = os.path.dirname(cwd)
        if parent == cwd:
            return os.getcwd()
        cwd = parent


def _session_dir(session_id: str, config: dict) -> str:
    safe_id = _sanitize_session_id(session_id)
    config_dir = _find_config_dir()
    if config_dir:
        # Place sessions dir as sibling to config: .claude/sentinel/../.sentinel/sessions/
        project_root = os.path.dirname(os.path.dirname(config_dir))
    else:
        project_root = _project_root()
    return os.path.join(project_root, ".sentinel", "sessions", safe_id)


def _session_lock_path(session_id: str, config: dict) -> str:
    d = _session_dir(session_id, config)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ollama.lock")


def _read_session_context(session_id: str, config: dict) -> Optional[dict]:
    """Read the accumulator's latest summary.json. Returns None if not available."""
    summary_path = os.path.join(_session_dir(session_id, config), "summary.json")
    try:
        with open(summary_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── Info deduplication ─────────────────────────────────────────────

_DEDUP_TTL_S = 30  # suppress duplicate info for same (rule, target) within this window


def _dedup_dir(session_id: str, config: dict) -> str:
    """Dedup cache dir, inside the session's .sentinel folder."""
    d = _session_dir(session_id, config)
    if not d:
        return ""
    dedup = os.path.join(d, "dedup")
    os.makedirs(dedup, exist_ok=True)
    return dedup


def _dedup_check(session_id: str, rule_id: str, target: str,
                 config: dict) -> bool:
    """Return True if this (rule, target) was seen recently and should be skipped."""
    d = _dedup_dir(session_id, config)
    if not d:
        return False
    path = os.path.join(d, "info_dedup.json")
    now = time.time()
    cache = {}
    try:
        with open(path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    key = f"{rule_id}:{target}"
    last_seen = cache.get(key, 0)
    if now - last_seen < _DEDUP_TTL_S:
        return True  # duplicate, skip

    # Update cache, prune expired entries
    cache = {k: v for k, v in cache.items() if now - v < _DEDUP_TTL_S}
    cache[key] = now
    try:
        with open(path, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return False


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


def evaluate_info_rule(rule: dict, event: dict, config: dict,
                       session_id: str) -> Optional[str]:
    """Call Ollama for info synthesis. Returns context string or None."""
    from sentinel_lock import acquire_lock, release_lock, LockPriority

    prompt = render_prompt(rule, event, config)
    ctx_config = config.get("context", {})
    backend, model = resolve_backend(
        config,
        override_backend=rule.get("backend", ctx_config.get("backend")),
        override_model=rule.get("model", ctx_config.get("model")),
    )
    lock_path = _session_lock_path(session_id, config)
    t0 = time.monotonic()

    fd = None
    if backend == "ollama":
        fd = acquire_lock(lock_path, LockPriority.P1_SYNTHESIZER, timeout_s=5)
    try:
        content = call_llm(prompt, "You are a JSON-only responder. Always respond with valid JSON, no other text.",
                           model, backend, config)
    except Exception:
        return None
    finally:
        if fd is not None:
            release_lock(fd)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _log(config, rule, event, violation=False, confidence=0.0,
         reason="info", elapsed_ms=elapsed_ms, level="info")

    try:
        parsed = json.loads(re.sub(r'^```(?:json)?\s*\n?', '',
                           re.sub(r'\n?```\s*$', '', content.strip())))
        return parsed.get("context", content)
    except (json.JSONDecodeError, AttributeError):
        return content.strip()


def main_pre(raw_data: dict, rules: list, config: dict):
    """PreToolUse mode: evaluate block/warn rules and static info rules."""
    event_data, agent_format = normalize_input(raw_data)

    event = parse_event(event_data, config)

    # Filter applicable rules
    matching = [r for r in rules if rule_matches(r, event)]
    if not matching:
        sys.exit(0)

    # Split info rules (static context, no LLM) from judge rules (need Ollama)
    info_rules = [r for r in matching if r.get("severity") == "info" and not r.get("post")]
    judge_rules = [r for r in matching if r.get("severity") != "info"]

    # Render info rules as static context (no Ollama required)
    # Deduplicate: skip if same (rule, target) was seen recently
    session_id = raw_data.get("session_id", "")
    target = (event.get("match_targets") or [""])[0]
    info_contexts = []
    for r in info_rules:
        if session_id and _dedup_check(session_id, r["id"], target, config):
            continue
        rendered = render_prompt(r, event, config)
        info_contexts.append(f"[{r['id']}] {rendered}")

    # Pre-flight: avoid N timeouts per rule when backend is not reachable
    # Only needed if there are judge rules to evaluate
    if judge_rules:
        global_backend = config.get("backend", "ollama")
        if not backend_reachable(global_backend, config):
            if config.get("fail_open", True):
                _debug(f"{global_backend} unreachable, fail_open=true, skipping judge rules", config)
                # Still output info context if any
                if info_contexts:
                    print(format_decision_info("\n\n".join(info_contexts), agent_format))
                sys.exit(0)
            else:
                fail_msg = f"SENTINEL: {global_backend} is not reachable (fail_open: false)"
                if info_contexts:
                    fail_msg = "\n\n".join(info_contexts) + "\n\n" + fail_msg
                print(format_decision(fail_msg, blockers=True, agent_format=agent_format))
                sys.exit(0)

        # Initialize concurrency gate (Ollama only — GPU contention)
        if global_backend == "ollama":
            init_ollama_semaphore(config.get("ollama_concurrency", 1))

        # Parallel evaluation (semaphore gates actual Ollama calls)
        violations = []
        with ThreadPoolExecutor(max_workers=config["max_parallel"]) as pool:
            futures = {
                pool.submit(evaluate_rule, r, event, config): r
                for r in judge_rules
            }
            for fut in as_completed(futures):
                result = fut.result()
                if result is not None:
                    violations.append(result)

        if violations:
            report = format_report(violations)
            if info_contexts:
                report = "\n\n".join(info_contexts) + "\n\n" + report
            blockers = any(v["severity"] == "block" for v in violations)
            print(format_decision(report, blockers, agent_format))
            sys.exit(0)

    # No violations from judge rules — output info context if any, then exit
    # Check for Scribe draft notifications
    draft_note = _check_scribe_drafts(session_id, config, _find_config_dir()) if _find_config_dir() else None
    if draft_note:
        info_contexts.append(draft_note)
    if info_contexts:
        print(format_decision_info("\n\n".join(info_contexts), agent_format))
    sys.exit(0)


def main_post(raw_data: dict, rules: list, config: dict):
    """PostToolUse mode: evaluate info post rules with synthesizer."""
    event_data, agent_format = normalize_input(raw_data)
    session_id = raw_data.get("session_id", "unknown")

    event = parse_event(event_data, config)
    matching = [r for r in rules
                if rule_matches(r, event)
                and r.get("severity") == "info"
                and r.get("post")]
    if not matching:
        sys.exit(0)

    post_backend = config.get("backend", "ollama")
    if not backend_reachable(post_backend, config):
        sys.exit(0)  # info is advisory, always fail open

    session_context = _read_session_context(session_id, config)

    event["template_vars"]["tool_output"] = json.dumps(
        raw_data.get("tool_response", {}))[:800]
    event["template_vars"]["session_context"] = json.dumps(
        session_context) if session_context else "{}"

    # Deduplicate: skip if same (rule, target) was seen recently
    target = (event.get("match_targets") or [""])[0]
    deduped = [r for r in matching
               if not _dedup_check(session_id, r["id"], target, config)]
    if not deduped:
        sys.exit(0)

    contexts = []
    for r in deduped:
        result = evaluate_info_rule(r, event, config, session_id)
        if result:
            contexts.append(result)

    if not contexts:
        config_dir_val = _find_config_dir()
        draft_note = _check_scribe_drafts(session_id, config, config_dir_val) if config_dir_val else None
        if draft_note:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": draft_note,
                }
            }))
        sys.exit(0)

    config_dir_val = _find_config_dir()
    draft_note = _check_scribe_drafts(session_id, config, config_dir_val) if config_dir_val else None
    if draft_note:
        contexts.append(draft_note)
    combined = "\n".join(contexts)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": combined,
        }
    }))
    sys.exit(0)


def main():
    post_mode = "--post" in sys.argv

    config_dir = _find_config_dir()
    if not config_dir:
        sys.exit(0)

    config = load_config(config_dir)
    rules = load_rules(config["rules_dir"])
    if not rules:
        sys.exit(0)

    try:
        raw_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if post_mode:
        main_post(raw_data, rules, config)
    else:
        main_pre(raw_data, rules, config)


if __name__ == "__main__":
    main()
