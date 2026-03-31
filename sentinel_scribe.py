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
                think: bool = False) -> str:
    """Call Ollama chat endpoint. Returns response content string."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a JSON-only responder. Always respond with valid JSON, no other text."},
            {"role": "user", "content": prompt},
        ],
        "format": "json",
        "stream": False,
        "think": think,
        "options": {
            "num_predict": 1000 if think else 150,
            "temperature": 0.1,
        },
    }).encode()

    url = f"{config.get('ollama_url', 'http://localhost:11434')}/api/chat"
    timeout_s = config.get("timeout_ms", 5000) / 1000

    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read())
    return body.get("message", {}).get("content", "")


def parse_extraction_response(response: str) -> list[dict]:
    """Parse the LLM extraction response. Returns list of conventions."""
    # Try direct parse
    try:
        data = json.loads(response.strip())
        return data.get("conventions", [])
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Try stripping markdown fences
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', response.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        data = json.loads(cleaned.strip())
        return data.get("conventions", [])
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Find JSON object in response
    start = response.find("{")
    end = response.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(response[start:end + 1])
            return data.get("conventions", [])
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    return []
