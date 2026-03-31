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
