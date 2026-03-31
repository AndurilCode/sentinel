#!/usr/bin/env python3
"""
Sentinel Scribe — Convention extraction and draft rule generation.

Observes human prompts to coding agents, extracts reusable conventions,
and proposes draft Sentinel rules. The human shifts from rule author
to rule reviewer.

Modes:
  --observe   UserPromptSubmit hook: classify human prompt for conventions
  --flush     SessionEnd hook: process deferred observations
  --learn     Slash command: scan documentation files for conventions

Dependencies: PyYAML (pip install pyyaml)
"""

import sys
import os
import json
import re
import time
import hashlib
import urllib.request
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

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

SCRIBE_DEFAULTS = {
    "enabled": True,
    "model": None,
    "guidance": None,
    "sources": {
        "user_prompts": True,
        "documentation": True,
    },
    "thresholds": {
        "extraction_confidence": 0.7,
        "draft_confidence": 0.7,
    },
    "context_window_before": 5,
    "doc_globs": [
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        "docs/**/*.md",
        "ADR*.md",
    ],
    "notification": {
        "max_age_days": 7,
    },
}

BASE_DEFAULTS = {
    "model": "gemma3:4b",
    "ollama_url": "http://localhost:11434",
    "timeout_ms": 5000,
    "confidence_threshold": 0.7,
}


# ── Config loading ──────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins for non-dict values."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(sentinel_dir: str) -> dict:
    """Load config.yaml and merge scribe defaults."""
    cfg = dict(BASE_DEFAULTS)
    cfg["rules_dir"] = os.path.join(sentinel_dir, "rules")
    cfg["drafts_dir"] = os.path.join(sentinel_dir, "drafts")

    for ext in ("yaml", "yml", "json"):
        p = os.path.join(sentinel_dir, f"config.{ext}")
        if os.path.exists(p):
            with open(p) as f:
                if p.endswith((".yaml", ".yml")):
                    loaded = yaml.safe_load(f) or {}
                else:
                    loaded = json.load(f)
            cfg.update(loaded)
            break

    user_scribe = cfg.get("scribe", {})
    cfg["scribe"] = _deep_merge(SCRIBE_DEFAULTS, user_scribe)

    return cfg


# ── Directory helpers ───────────────────────────────────────────────

def _find_config_dir() -> Optional[str]:
    """Walk up from cwd to find .claude/sentinel/ config directory."""
    env_dir = os.environ.get("SENTINEL_CONFIG_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
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


def _scribe_dir(config_dir: str) -> str:
    """Resolve .sentinel/scribe/ path from config dir."""
    project_root = os.path.dirname(os.path.dirname(config_dir))
    d = os.path.join(project_root, ".sentinel", "scribe")
    os.makedirs(d, exist_ok=True)
    return d


def _session_dir(session_id: str, config_dir: str) -> str:
    """Resolve .sentinel/sessions/<id>/ path."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', session_id)
    project_root = os.path.dirname(os.path.dirname(config_dir))
    d = os.path.join(project_root, ".sentinel", "sessions", safe_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── Observation store ───────────────────────────────────────────────

def append_observation(scribe_dir: str, observation: dict) -> None:
    """Append an observation to the JSONL store."""
    os.makedirs(scribe_dir, exist_ok=True)
    path = os.path.join(scribe_dir, "observations.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(observation) + "\n")


# ── Dismissed blocklist ─────────────────────────────────────────────

def _statement_hash(statement: str) -> str:
    """Short hash of a convention statement for blocklist matching."""
    return hashlib.sha256(statement.encode()).hexdigest()[:12]


def add_dismissal(scribe_dir: str, scope: str, trigger: str, statement: str) -> None:
    """Add a scope+trigger to the dismissed blocklist."""
    os.makedirs(scribe_dir, exist_ok=True)
    path = os.path.join(scribe_dir, "dismissed.jsonl")
    entry = {
        "scope": scope,
        "trigger": trigger,
        "statement_hash": _statement_hash(statement),
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def is_dismissed(scribe_dir: str, scope: str, trigger: str) -> bool:
    """Check if a scope+trigger combination has been dismissed."""
    path = os.path.join(scribe_dir, "dismissed.jsonl")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("scope") == scope and entry.get("trigger") == trigger:
                    return True
            except json.JSONDecodeError:
                continue
    return False


# ── Context window assembly ─────────────────────────────────────────

from sentinel_context import compact_event


def build_context_window(transcript_path: str, max_events: int = 5) -> list[str]:
    """Read transcript JSONL and return the last N compacted events as strings.

    Returns a list of human-readable one-liners for the classification prompt.
    Reads the entire transcript and takes the tail — transcripts are small
    enough that this is faster than seeking backwards in JSONL.
    """
    if not os.path.exists(transcript_path):
        return []

    compacted = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evt = compact_event(entry)
                if evt:
                    compacted.append(evt)
    except OSError:
        return []

    # Take the last max_events entries
    tail = compacted[-max_events:] if len(compacted) > max_events else compacted

    # Format each event as a one-liner
    lines = []
    for evt in tail:
        trigger = evt.get("trigger", "")
        text = evt.get("text", "")
        if trigger == "user":
            lines.append(f"[human] {text}")
        elif trigger == "stop":
            lines.append(f"[assistant] {text}")
        else:
            lines.append(f"[{trigger}] {text}")
    return lines


# ── Extraction prompts ──────────────────────────────────────────────

HUMAN_EXTRACTION_PROMPT = """You are observing a conversation between a developer and a coding agent.
The developer just responded. Read the full context and determine whether
the developer is expressing a reusable convention — a constraint that
should apply to ALL future agent sessions on this repository.

Conversation context:
{context_window}
{guidance_block}
Conventions include:
- Boundaries: areas the agent should not modify
- Process: steps that must always happen
- Constraints: things the agent must never do
- Architectural knowledge that affects decisions

The developer may express conventions through:
- Direct instructions: "never modify billing directly"
- Rejections: "no" to an agent proposal (the proposal reveals the boundary)
- Approvals of non-obvious steps: "yes" to "should I create a migration?"
- Corrections: "stop, revert that — this file is autogenerated"
- Any language

NOT a convention:
- Task instructions: "add a login page", "fix the bug in auth.ts"
- Routine approvals: "yes" to "I'll add the function"
- Clarifications about the current task only

If no convention: {{"conventions": []}}
If found: {{"conventions": [{{"statement": "...", "scope_hint": "...", "trigger_hint": "file_write|bash|mcp|unknown", "confidence": 0.0-1.0, "evidence": "what the developer said or did that expresses this"}}]}}"""

DOC_EXTRACTION_PROMPT = """You are reading a human artifact from a software repository.
Extract any conventions, constraints, or rules being expressed.

Source type: {source_type}
Content: {content}
{guidance_block}
A convention is: a statement about what SHOULD or SHOULD NOT happen
in this codebase, expressed by a human with authority.

If no convention: {{"conventions": []}}
If found: {{"conventions": [{{"statement": "...", "scope_hint": "...", "trigger_hint": "file_write|bash|mcp|unknown", "confidence": 0.0-1.0, "evidence": "exact phrase that expresses this"}}]}}"""

GUIDANCE_BLOCK = """
PRIORITY GUIDANCE FROM THE REPOSITORY OWNER:
{guidance}

Conventions matching this guidance should receive higher confidence.
But do not ignore conventions outside this guidance — still extract
them if clearly expressed.
"""


def _guidance_block(guidance: Optional[str]) -> str:
    """Return guidance block or empty string."""
    if guidance:
        return GUIDANCE_BLOCK.format(guidance=guidance)
    return ""


def build_human_extraction_prompt(window_lines: list[str],
                                   guidance: Optional[str]) -> str:
    """Build the human channel extraction prompt."""
    context = "\n".join(window_lines)
    return HUMAN_EXTRACTION_PROMPT.format(
        context_window=context,
        guidance_block=_guidance_block(guidance),
    )


def build_doc_extraction_prompt(content: str, source_type: str,
                                 guidance: Optional[str]) -> str:
    """Build the documentation extraction prompt."""
    return DOC_EXTRACTION_PROMPT.format(
        source_type=source_type,
        content=content,
        guidance_block=_guidance_block(guidance),
    )


# ── Ollama integration ──────────────────────────────────────────────

def call_ollama(prompt: str, model: str, config: dict,
                think: bool = False, json_format: bool = True) -> str:
    """Call Ollama chat endpoint. Returns response content string.

    json_format=True forces JSON output (for extraction). Set to False for
    synthesis which returns YAML.
    """
    system_msg = ("You are a JSON-only responder. Always respond with valid JSON, no other text."
                  if json_format else
                  "You are a YAML-only responder. Always respond with valid YAML, no other text.")
    payload_dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "num_predict": 1000 if not json_format else 150,
            "temperature": 0.1,
        },
    }
    if json_format:
        payload_dict["format"] = "json"
    payload = json.dumps(payload_dict).encode()

    url = f"{config.get('ollama_url', 'http://localhost:11434')}/api/chat"
    timeout_s = config.get("timeout_ms", 5000) / 1000

    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read())
    return body.get("message", {}).get("content", "")


_VALID_TRIGGERS = {"file_write", "bash", "mcp", "unknown"}


def _normalize_trigger_hint(hint: str) -> str:
    """Normalize trigger_hint to a valid single trigger type.

    The SLM sometimes returns pipe-separated values like 'file_write|read|modify'
    or free-form text. Extract the first valid trigger type, default to 'unknown'.
    """
    if not hint:
        return "unknown"
    # Direct match
    if hint in _VALID_TRIGGERS:
        return hint
    # Split on common separators and find first valid
    for part in re.split(r'[|,/\s]+', hint):
        part = part.strip().lower()
        if part in _VALID_TRIGGERS:
            return part
    # Heuristic: check if hint contains a valid trigger as substring
    for valid in ("file_write", "bash", "mcp"):
        if valid in hint.lower():
            return valid
    return "unknown"


def _normalize_conventions(conventions: list[dict]) -> list[dict]:
    """Normalize extracted conventions — fix trigger_hint, ensure required fields."""
    for conv in conventions:
        conv["trigger_hint"] = _normalize_trigger_hint(conv.get("trigger_hint", "unknown"))
        conv.setdefault("scope_hint", "**")
        conv.setdefault("confidence", 0.0)
        conv.setdefault("evidence", "")
        conv.setdefault("statement", "")
    return conventions


def parse_extraction_response(response: str) -> list[dict]:
    """Parse the LLM extraction response. Returns list of conventions."""
    # Try direct parse
    try:
        data = json.loads(response.strip())
        return _normalize_conventions(data.get("conventions", []))
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Try stripping markdown fences
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', response.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        data = json.loads(cleaned.strip())
        return _normalize_conventions(data.get("conventions", []))
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Find JSON object in response
    start = response.find("{")
    end = response.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(response[start:end + 1])
            return _normalize_conventions(data.get("conventions", []))
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    return []


# ── Active rule and draft checks ────────────────────────────────────

def load_active_rules(rules_dir: str) -> list[dict]:
    """Load active rules from rules directory."""
    rules = []
    if not os.path.isdir(rules_dir):
        return rules
    for entry in os.listdir(rules_dir):
        if not entry.endswith((".yaml", ".yml", ".json")):
            continue
        try:
            with open(os.path.join(rules_dir, entry)) as f:
                if entry.endswith((".yaml", ".yml")):
                    rule = yaml.safe_load(f) or {}
                else:
                    rule = json.load(f)
            rule.setdefault("id", Path(entry).stem)
            rule.setdefault("trigger", "any")
            rule.setdefault("scope", ["**"])
            rules.append(rule)
        except Exception:
            continue
    return rules



def write_draft(drafts_dir: str, rule: dict, draft_meta: dict) -> str:
    """Write a draft rule YAML file. Returns the file path."""
    os.makedirs(drafts_dir, exist_ok=True)
    rule_id = rule.get("id", "unnamed-rule")
    path = os.path.join(drafts_dir, f"{rule_id}.draft.yaml")
    output = dict(rule)
    output["_draft"] = draft_meta
    with open(path, "w") as f:
        yaml.dump(output, f, default_flow_style=False, sort_keys=False)
    return path


# ── Synthesis ───────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """You are generating a Sentinel rule YAML file. Sentinel evaluates coding agent
actions against repository-defined rules using a local LLM.

A developer expressed this convention:
Statement: {statement}
Evidence: "{evidence}"
Trigger hint: {trigger_hint}

ACTUAL files in the repository that relate to this convention:
{matched_files}

IMPORTANT: The "scope" field MUST use glob patterns that match REAL paths from the
file list above. Do NOT invent paths. If the files above are under "src/billing/",
use "src/billing/**". If no files were found, use a broad pattern like "**".

Existing rules for style reference:
{sample_rules}

Generate a complete rule YAML with these fields:
- id: kebab-case identifier
- trigger: one of file_write, bash, mcp, or any (pick the single most appropriate)
- severity: block or warn (choose based on how critical the convention is)
- scope: list of glob patterns derived from the ACTUAL file paths above
- exclude: list of glob patterns for exceptions (e.g., test files). Omit if none.
- prompt: the evaluation prompt using {{{{template_vars}}}} for the chosen trigger:
  - file_write trigger: use {{{{file_path}}}}, {{{{content_snippet}}}}
  - bash trigger: use {{{{command}}}}
  - mcp trigger: use {{{{server_name}}}}, {{{{mcp_tool}}}}, {{{{mcp_arguments}}}}

The prompt must end with:
Respond ONLY with JSON: {{"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}}

Return ONLY valid YAML, no other text."""


def build_synthesis_prompt(observation: dict, matched_files: list[str],
                            sample_rules: list[dict]) -> str:
    """Build the synthesis prompt with all context."""
    files_text = "\n".join(f"  - {f}" for f in matched_files[:20]) if matched_files else "  (no matching files found)"
    rules_text = ""
    for r in sample_rules[:3]:
        rules_text += f"\n---\n{yaml.dump(r, default_flow_style=False)}"
    if not rules_text:
        rules_text = "\n  (no existing rules)"

    return SYNTHESIS_PROMPT.format(
        statement=observation["statement"],
        evidence=observation.get("evidence", ""),
        trigger_hint=observation.get("trigger_hint", "any"),
        matched_files=files_text,
        sample_rules=rules_text,
    )


def parse_synthesis_response(response: str) -> Optional[dict]:
    """Parse YAML rule from synthesis LLM response."""
    # Strip markdown fences
    cleaned = re.sub(r'^```(?:yaml)?\s*\n?', '', response.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        rule = yaml.safe_load(cleaned)
        if isinstance(rule, dict) and "prompt" in rule:
            return rule
    except yaml.YAMLError:
        pass
    return None


# ── Observe pipeline ────────────────────────────────────────────────

def _glob_repo_files(scope_hint: str, project_root: str) -> list[str]:
    """Find files matching scope_hint in the repo.

    Handles both proper globs ('src/billing/**') and free-text hints
    ('billing module') by falling back to keyword-based directory search.
    """
    matched = []
    pattern = scope_hint

    # If it looks like a glob pattern already, use it directly
    if "*" in pattern or "/" in pattern or "." in pattern:
        if not pattern.endswith("*"):
            pattern = pattern.rstrip("/") + "/**"
        for root, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "venv", ".venv", "__pycache__")]
            for fname in files:
                rel = os.path.relpath(os.path.join(root, fname), project_root)
                if fnmatch(rel, pattern):
                    matched.append(rel)
                if len(matched) >= 20:
                    return matched
        if matched:
            return matched

    # Fallback: extract keywords from free-text hint and search for
    # directories/files containing those keywords
    keywords = [w.lower() for w in re.split(r'[\s_/\\-]+', scope_hint)
                if len(w) > 2 and w.lower() not in ("the", "and", "for", "all", "any", "module", "folder", "file", "code")]
    if not keywords:
        return []

    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "venv", ".venv", "__pycache__")]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), project_root)
            rel_lower = rel.lower()
            if any(kw in rel_lower for kw in keywords):
                matched.append(rel)
            if len(matched) >= 20:
                return matched
    return matched


def observe(user_prompt: str, transcript_path: str, session_id: str,
            config: dict, config_dir: str, scribe_dir: str,
            session_dir: str) -> None:
    """Full --observe pipeline: extract convention from human prompt, optionally synthesize draft."""
    from sentinel_lock import acquire_lock, release_lock, LockPriority

    scribe_cfg = config.get("scribe", SCRIBE_DEFAULTS)
    model = scribe_cfg.get("model") or config.get("model", "gemma3:4b")
    guidance = scribe_cfg.get("guidance")
    thresholds = scribe_cfg.get("thresholds", {})
    extraction_confidence = thresholds.get("extraction_confidence", 0.7)
    draft_confidence = thresholds.get("draft_confidence", 0.7)

    # 1. Build context window from transcript
    max_events = scribe_cfg.get("context_window_before", 5)
    window_lines = build_context_window(transcript_path, max_events=max_events)
    window_lines.append(f"[human] {user_prompt[:300]}")

    # 2. Acquire GPU lock (P3)
    lock_path = os.path.join(session_dir, "ollama.lock")
    os.makedirs(session_dir, exist_ok=True)
    fd = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
    if fd is None:
        deferred_dir = os.path.join(scribe_dir, "deferred")
        os.makedirs(deferred_dir, exist_ok=True)
        deferred = {
            "user_prompt": user_prompt[:300],
            "transcript_path": transcript_path,
            "session_id": session_id,
            "window_lines": window_lines,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        deferred_path = os.path.join(deferred_dir, f"{int(time.time() * 1000)}.json")
        with open(deferred_path, "w") as f:
            json.dump(deferred, f)
        return

    try:
        # 3. SLM classify + extract
        prompt = build_human_extraction_prompt(window_lines, guidance)
        response = call_ollama(prompt, model, config, think=False)
    except Exception:
        return
    finally:
        release_lock(fd)

    conventions = parse_extraction_response(response)
    if not conventions:
        return

    # 4. Process each extracted convention
    for conv in conventions:
        confidence = conv.get("confidence", 0.0)
        if confidence < extraction_confidence:
            continue

        observation = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "user_prompt",
            "session_id": session_id,
            "statement": conv.get("statement", ""),
            "scope_hint": conv.get("scope_hint", "**"),
            "trigger_hint": conv.get("trigger_hint", "unknown"),
            "confidence": confidence,
            "evidence": conv.get("evidence", ""),
            "drafted": False,
        }
        append_observation(scribe_dir, observation)

        if confidence < draft_confidence:
            continue

        scope_hint = conv.get("scope_hint", "**")
        trigger_hint = conv.get("trigger_hint", "unknown")
        rules_dir = config.get("rules_dir", os.path.join(config_dir, "rules"))
        drafts_dir = config.get("drafts_dir", os.path.join(config_dir, "drafts"))

        # No deterministic pre-synthesis checks on scope_hint — the hint is
        # LLM-generated free text (possibly in any language) and cannot be
        # reliably matched against active rule globs or dismissed entries.
        # The human reviews drafts via /sentinel-drafts.

        project_root = os.path.dirname(os.path.dirname(config_dir))
        matched_files = _glob_repo_files(scope_hint, project_root)
        active_rules = load_active_rules(rules_dir)
        sample_rules = active_rules[:3]

        fd2 = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
        try:
            synth_prompt = build_synthesis_prompt(conv, matched_files, sample_rules)
            synth_response = call_ollama(synth_prompt, model, config, json_format=False)
        except Exception:
            continue
        finally:
            release_lock(fd2)

        rule = parse_synthesis_response(synth_response)
        if rule and rule.get("prompt"):
            rule.setdefault("id", re.sub(r'[^a-z0-9-]', '-',
                            conv["statement"][:40].lower().strip()).strip("-"))
            draft_meta = {
                "source": "user_prompt",
                "observed": 1,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "evidence": [conv.get("evidence", "")],
                "confidence": confidence,
                "synthesized": datetime.now(timezone.utc).isoformat(),
                "model": model,
            }
            write_draft(drafts_dir, rule, draft_meta)
            observation["drafted"] = True


# ── Draft notification ──────────────────────────────────────────────

def check_pending_drafts(drafts_dir: str, session_dir: str,
                          max_age_days: int = 7) -> Optional[str]:
    """Check for recent drafts and return a notification string, or None.

    Returns notification only once per session (tracked via scribe_notified flag file).
    """
    notified_path = os.path.join(session_dir, "scribe_notified")
    if os.path.exists(notified_path):
        return None

    if not os.path.isdir(drafts_dir):
        return None

    now = datetime.now(timezone.utc)
    recent_count = 0

    for entry in os.listdir(drafts_dir):
        if not entry.endswith(".draft.yaml"):
            continue
        try:
            with open(os.path.join(drafts_dir, entry)) as f:
                draft = yaml.safe_load(f) or {}
            synthesized = draft.get("_draft", {}).get("synthesized", "")
            if not synthesized:
                continue
            draft_time = datetime.fromisoformat(synthesized)
            age_days = (now - draft_time).days
            if age_days <= max_age_days:
                recent_count += 1
        except Exception:
            continue

    if recent_count == 0:
        return None

    os.makedirs(session_dir, exist_ok=True)
    with open(notified_path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())

    plural = "s" if recent_count > 1 else ""
    return f"Sentinel Scribe: {recent_count} new draft rule{plural} pending review. Run /sentinel-drafts to see them."


# ── Flush mode ──────────────────────────────────────────────────────

def flush(config: dict, config_dir: str, scribe_dir: str,
          session_dir: str, session_id: str) -> None:
    """Process deferred observations from lock timeouts."""
    deferred_dir = os.path.join(scribe_dir, "deferred")
    if not os.path.isdir(deferred_dir):
        return

    files = sorted(os.listdir(deferred_dir))
    for fname in files:
        if not fname.endswith(".json"):
            continue
        path = os.path.join(deferred_dir, fname)
        try:
            with open(path) as f:
                deferred = json.load(f)
            observe(
                user_prompt=deferred.get("user_prompt", ""),
                transcript_path=deferred.get("transcript_path", ""),
                session_id=deferred.get("session_id", session_id),
                config=config,
                config_dir=config_dir,
                scribe_dir=scribe_dir,
                session_dir=session_dir,
            )
            os.unlink(path)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass


# ── Learn mode ──────────────────────────────────────────────────────

def _find_doc_files(project_root: str, doc_globs: list[str]) -> list[str]:
    """Find documentation files matching configured globs."""
    found = []
    for pattern in doc_globs:
        if "**" in pattern:
            for root, dirs, files in os.walk(project_root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    rel = os.path.relpath(os.path.join(root, fname), project_root)
                    match_pattern = pattern.replace("**/", "")
                    if fnmatch(fname, match_pattern) or fnmatch(rel, pattern):
                        found.append(os.path.join(root, fname))
        else:
            full = os.path.join(project_root, pattern)
            if os.path.exists(full):
                found.append(full)
    return found


def learn(config: dict, config_dir: str, scribe_dir: str,
          session_dir: str) -> dict:
    """Scan documentation files and extract conventions."""
    from sentinel_lock import acquire_lock, release_lock, LockPriority

    scribe_cfg = config.get("scribe", SCRIBE_DEFAULTS)
    model = scribe_cfg.get("model") or config.get("model", "gemma3:4b")
    guidance = scribe_cfg.get("guidance")
    thresholds = scribe_cfg.get("thresholds", {})
    extraction_confidence = thresholds.get("extraction_confidence", 0.7)
    draft_confidence = thresholds.get("draft_confidence", 0.7)
    doc_globs = scribe_cfg.get("doc_globs", SCRIBE_DEFAULTS["doc_globs"])

    project_root = os.path.dirname(os.path.dirname(config_dir))
    doc_files = _find_doc_files(project_root, doc_globs)

    result = {"files_scanned": 0, "conventions_found": 0, "drafts_created": 0}
    lock_path = os.path.join(session_dir, "ollama.lock")
    os.makedirs(session_dir, exist_ok=True)

    for doc_path in doc_files:
        try:
            with open(doc_path) as f:
                content = f.read()
        except OSError:
            continue

        if not content.strip():
            continue

        result["files_scanned"] += 1
        source_type = os.path.basename(doc_path)

        max_chunk = 3000
        chunks = [content[i:i + max_chunk] for i in range(0, len(content), max_chunk)]

        for chunk in chunks:
            fd = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
            try:
                prompt = build_doc_extraction_prompt(chunk, source_type, guidance)
                response = call_ollama(prompt, model, config, think=False)
            except Exception:
                continue
            finally:
                release_lock(fd)

            conventions = parse_extraction_response(response)
            for conv in conventions:
                confidence = conv.get("confidence", 0.0)
                if confidence < extraction_confidence:
                    continue

                result["conventions_found"] += 1
                observation = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "source": "documentation",
                    "session_id": "learn",
                    "statement": conv.get("statement", ""),
                    "scope_hint": conv.get("scope_hint", "**"),
                    "trigger_hint": conv.get("trigger_hint", "unknown"),
                    "confidence": confidence,
                    "evidence": conv.get("evidence", ""),
                    "drafted": False,
                }
                append_observation(scribe_dir, observation)

                if confidence < draft_confidence:
                    continue

                scope_hint = conv.get("scope_hint", "**")
                trigger_hint = conv.get("trigger_hint", "unknown")
                rules_dir = config.get("rules_dir", os.path.join(config_dir, "rules"))
                drafts_dir = config.get("drafts_dir", os.path.join(config_dir, "drafts"))

                matched_files = _glob_repo_files(scope_hint, project_root)
                active_rules = load_active_rules(rules_dir)
                sample_rules = active_rules[:3]

                fd2 = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
                try:
                    synth_prompt = build_synthesis_prompt(conv, matched_files, sample_rules)
                    synth_response = call_ollama(synth_prompt, model, config, json_format=False)
                except Exception:
                    continue
                finally:
                    release_lock(fd2)

                rule = parse_synthesis_response(synth_response)
                if rule and rule.get("prompt"):
                    rule.setdefault("id", re.sub(r'[^a-z0-9-]', '-',
                                    conv["statement"][:40].lower().strip()).strip("-"))
                    draft_meta = {
                        "source": "documentation",
                        "observed": 1,
                        "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        "evidence": [conv.get("evidence", "")],
                        "confidence": confidence,
                        "synthesized": datetime.now(timezone.utc).isoformat(),
                        "model": model,
                    }
                    write_draft(drafts_dir, rule, draft_meta)
                    result["drafts_created"] += 1

    return result


# ── Main entry point ────────────────────────────────────────────────

def main():
    config_dir = _find_config_dir()
    if not config_dir:
        sys.exit(0)

    config = load_config(config_dir)
    scribe_cfg = config.get("scribe", {})
    if not scribe_cfg.get("enabled", True):
        sys.exit(0)

    project_root = os.path.dirname(os.path.dirname(config_dir))
    scribe_d = _scribe_dir(config_dir)

    if "--observe" in sys.argv:
        if not scribe_cfg.get("sources", {}).get("user_prompts", True):
            sys.exit(0)
        try:
            raw_data = json.loads(sys.stdin.read())
        except Exception:
            sys.exit(0)

        session_id = raw_data.get("session_id", "unknown")
        user_prompt = raw_data.get("user_prompt", "")
        transcript_path = raw_data.get("transcript_path", "")
        if not user_prompt:
            sys.exit(0)

        session_d = _session_dir(session_id, config_dir)
        observe(
            user_prompt=user_prompt,
            transcript_path=transcript_path,
            session_id=session_id,
            config=config,
            config_dir=config_dir,
            scribe_dir=scribe_d,
            session_dir=session_d,
        )

    elif "--flush" in sys.argv:
        try:
            raw_data = json.loads(sys.stdin.read())
        except Exception:
            raw_data = {}
        session_id = raw_data.get("session_id", "unknown")
        session_d = _session_dir(session_id, config_dir)
        flush(
            config=config,
            config_dir=config_dir,
            scribe_dir=scribe_d,
            session_dir=session_d,
            session_id=session_id,
        )

    elif "--learn" in sys.argv:
        session_d = os.path.join(project_root, ".sentinel", "sessions", "learn")
        os.makedirs(session_d, exist_ok=True)
        result = learn(
            config=config,
            config_dir=config_dir,
            scribe_dir=scribe_d,
            session_dir=session_d,
        )
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
