#!/usr/bin/env python3
"""
Sentinel Scribe — Convention extraction and draft rule generation.

Observes coding sessions, extracts reusable conventions,
and proposes draft Sentinel rules. The human shifts from rule author
to rule reviewer.

Modes:
  --reflect   SessionEnd hook: analyze full transcript for conventions
  --learn     Slash command: scan documentation files for conventions

Dependencies: PyYAML (pip install pyyaml)
"""

import sys
import os
import json
import re
import time
import hashlib
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional
from sentinel_log import log_llm
from sentinel_backends import call_llm, resolve_backend

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
    "extraction_model": None,
    "synthesis_model": None,
    "guidance": None,
    "think": False,
    "extraction_timeout_ms": 15000,
    "extraction_num_predict": 1000,
    "synthesis_timeout_ms": 15000,
    "synthesis_num_predict": 1000,
    "temperature": 0.1,
    "transcript_budget_chars": 4000,
    "sources": {
        "documentation": True,
    },
    "thresholds": {
        "extraction_confidence": 0.7,
        "draft_confidence": 0.8,
    },
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


def _resolve_model(scribe_cfg: dict, config: dict, step: str) -> str:
    """Resolve model for a scribe step.

    Priority: scribe.<step>_model → scribe.model → top-level model → default.
    """
    step_key = f"{step}_model"
    return (scribe_cfg.get(step_key)
            or scribe_cfg.get("model")
            or config.get("model", "gemma3:4b"))


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


def read_compacted_transcript(transcript_path: str,
                               budget_chars: int = 4000) -> str:
    """Read full transcript, compact events, return formatted string.

    If the result exceeds budget_chars, keeps head + tail with a truncation marker.
    """
    if not os.path.exists(transcript_path):
        return ""

    try:
        from sentinel_context import compact_event
    except ImportError:
        return ""

    compacted = []
    state = {"pending_tools": []}
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
                evt = compact_event(entry, state)
                if evt:
                    compacted.append(evt)
    except OSError:
        return ""

    # Format as one-liner strings
    lines = []
    for evt in compacted:
        trigger = evt.get("trigger", "")
        text = evt.get("text", "")
        if trigger == "user":
            lines.append(f"[human] {text}")
        elif trigger == "tool_result":
            lines.append(f"[result] {text}")
        elif trigger == "stop":
            lines.append(f"[assistant] {text}")
        else:
            lines.append(f"[{trigger}] {text}")

    full_text = "\n".join(lines)
    if len(full_text) <= budget_chars:
        return full_text

    # Truncate: keep 40% head + 60% tail
    head_budget = int(budget_chars * 0.4)
    tail_budget = budget_chars - head_budget
    head_lines = []
    head_len = 0
    for ln in lines:
        if head_len + len(ln) + 1 > head_budget:
            break
        head_lines.append(ln)
        head_len += len(ln) + 1

    tail_lines = []
    tail_len = 0
    for ln in reversed(lines):
        if tail_len + len(ln) + 1 > tail_budget:
            break
        tail_lines.insert(0, ln)
        tail_len += len(ln) + 1

    skipped = len(lines) - len(head_lines) - len(tail_lines)
    marker = f"...[{skipped} events truncated]..."
    return "\n".join(head_lines + [marker] + tail_lines)


# ── Extraction prompts ──────────────────────────────────────────────

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


def build_doc_extraction_prompt(content: str, source_type: str,
                                 guidance: Optional[str]) -> str:
    """Build the documentation extraction prompt."""
    return DOC_EXTRACTION_PROMPT.format(
        source_type=source_type,
        content=content,
        guidance_block=_guidance_block(guidance),
    )


TRANSCRIPT_EXTRACTION_PROMPT = """You are observing a completed coding session between a developer and a coding agent.
Your job: extract PERMANENT conventions that apply to ALL future sessions in this repository.

{summary_block}
SESSION TRANSCRIPT:
{transcript_text}
{guidance_block}
Look for TWO types of signals:

1. HUMAN-EXPRESSED RULES — the developer explicitly states a permanent convention:
  - "never modify billing directly" — boundary (permanent)
  - "always run tests before pushing" — process (permanent)
  - Language: "never", "always", "from now on", "in this repo we...", "the rule is..."
  - The developer must be stating a GENERAL RULE, not a task-specific instruction

2. REPEATED AGENT ERRORS — the agent hits the SAME kind of error 2+ times in the session (also treat agent self-corrections as a signal and label responses with "source": "agent_self_correction"):
   - Same tool fails repeatedly with a similar error (indicates a repo-specific trap)
   - Agent tries the same wrong approach multiple times before finding the right one
   - The convention should describe the CORRECT approach any future agent should use
   - Ignore transient bug fixes fixed on first retry — those are normal work

MOST sessions yield ZERO conventions. Default to empty.

RETURN EMPTY for:
- Normal task execution (writing code, reading files, running commands)
- One-off debugging or task-specific decisions
- Agent exploring/learning about the codebase
- Implementation details of the current task (e.g. "use adapter pattern", "bump version")
- Architectural decisions for a specific feature being built
- Agent transient self-corrections that are clearly about the current task
- Config changes, timeout tuning, or parameter adjustments that are operational-only
- Anything that describes WHAT was built rather than HOW to work in this repo going forward

A convention is a rule that a NEW agent starting a NEW session should know.
Ask: "Would this matter to someone who knows nothing about today's task?"
If no → return empty.

Signals may include a "source" field in extracted convention objects. Common values:
- "user_feedback" — the human explicitly stated the rule
- "agent_self_correction" — the agent corrected itself and described the convention
- "heuristic" — the model inferred a likely convention from repeated patterns

SPECIAL INSTRUCTION: If rule format, configuration keys, template variables, or severity values were mentioned or implied and differ from the repository's current sentinel.py schema, list which skill files need updating so docs and skills stay in sync.
The skill files to check/update are:
- skills/sentinel-rule/SKILL.md
- skills/sentinel-config/SKILL.md
- skills/sentinel-init/SKILL.md
- docs/reference.md

RESPONSE FORMAT: Always respond with valid JSON only. If no conventions are found, respond with:
{{"conventions": [], "skills_to_update": []}}

If conventions are found, respond with this JSON structure ONLY:
{{"conventions": [{{"statement": "...", "scope_hint": "file glob like src/billing/** or ** for all files", "trigger_hint": "file_write|bash|mcp|unknown", "confidence": 0.0-1.0, "evidence": "exact transcript excerpt showing the signal"}}], "skills_to_update": ["skills/sentinel-rule/SKILL.md", "skills/sentinel-config/SKILL.md"]}}

Notes:
- "skills_to_update" must list the subset of the four skill files above that need changes (include relative paths).
- Keep all strings concise. Evidence should be an exact transcript snippet (one or two lines).
- The JSON must be the only content in the model's output; do not wrap in markdown or any extra text.

Example (no conventions found):
{{"conventions": [], "skills_to_update": []}}

Example (one convention):
{{"conventions": [{{"statement": "Always include severity in new rules.", "scope_hint": "**", "trigger_hint": "file_write", "confidence": 0.9, "evidence": "From now on, every rule must include severity: block or warn"}}], "skills_to_update": ["skills/sentinel-rule/SKILL.md", "docs/reference.md"]}}
"""


def build_transcript_extraction_prompt(transcript_text: str,
                                        summary: Optional[dict],
                                        guidance: Optional[str]) -> str:
    """Build the transcript-level extraction prompt."""
    if summary:
        summary_block = f"SESSION SUMMARY:\n{json.dumps(summary)}\n"
    else:
        summary_block = ""
    return TRANSCRIPT_EXTRACTION_PROMPT.format(
        summary_block=summary_block,
        transcript_text=transcript_text,
        guidance_block=_guidance_block(guidance),
    )



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
Scope hint (free text, NOT a file path): {scope_hint}
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
        scope_hint=observation.get("scope_hint", "**"),
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


# ── Validation + Synthesis ───────────────────────────────────────────

VALIDATION_SYNTHESIS_PROMPT = """You are validating a convention extracted from a coding session and deciding if it
should become a new Sentinel rule.

EXTRACTED CONVENTION:
Statement: {statement}
Evidence: "{evidence}"
Source: {source}
Scope hint: {scope_hint}
Trigger hint: {trigger_hint}

ACTUAL files in the repository matching this convention:
{matched_files}

EXISTING RULES AND PENDING DRAFTS in this repository:
{existing_rules}

TASK:
1. Is this convention SEMANTICALLY REDUNDANT with any existing rule above?
   (Same intent, even if worded differently or with different scope.)
2. If NOT redundant, generate a complete Sentinel rule YAML.

If REDUNDANT, return ONLY this JSON:
{{"redundant": true, "reason": "explanation of which rule covers this"}}

If NOT redundant, return ONLY valid YAML for a new rule with these fields:
- id: kebab-case identifier
- trigger: one of file_write, bash, mcp, or any
- severity: block or warn
- scope: list of glob patterns derived from the ACTUAL file paths above
- exclude: list of glob patterns for exceptions (omit if none)
- prompt: the evaluation prompt using {{{{template_vars}}}} for the chosen trigger:
  - file_write trigger: use {{{{file_path}}}}, {{{{content_snippet}}}}
  - bash trigger: use {{{{command}}}}
  - mcp trigger: use {{{{server_name}}}}, {{{{mcp_tool}}}}, {{{{mcp_arguments}}}}

The prompt must end with:
Respond ONLY with JSON: {{"violation": true/false, "confidence": 0.0-1.0, "reason": "one line"}}

Return ONLY the JSON or YAML, no other text."""


def build_validation_prompt(observation: dict, existing_rules: list[dict],
                             matched_files: list[str]) -> str:
    """Build the validation + synthesis prompt."""
    files_text = "\n".join(f"  - {f}" for f in matched_files[:20]) if matched_files else "  (no matching files found)"

    rules_text = ""
    for r in existing_rules:
        rules_text += f"\n  - id: {r.get('id', '?')}, trigger: {r.get('trigger', '?')}, scope: {r.get('scope', [])}, prompt: {str(r.get('prompt', ''))[:100]}"
    if not rules_text:
        rules_text = "\n  (no existing rules)"

    return VALIDATION_SYNTHESIS_PROMPT.format(
        statement=observation["statement"],
        evidence=observation.get("evidence", ""),
        source=observation.get("source", "unknown"),
        scope_hint=observation.get("scope_hint", "**"),
        trigger_hint=observation.get("trigger_hint", "any"),
        matched_files=files_text,
        existing_rules=rules_text,
    )


def parse_validation_response(response: str) -> Optional[dict]:
    """Parse validation LLM response.

    Returns:
      {"redundant": True, "reason": "..."} if redundant
      {"redundant": False, "rule": {...}} if a new rule was generated
      None if unparseable
    """
    stripped = response.strip()

    # Try JSON first (redundancy response)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and "redundant" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try after stripping markdown fences
    cleaned = re.sub(r'^```(?:json|yaml)?\s*\n?', '', stripped)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)

    # Try JSON again
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "redundant" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try YAML (new rule response)
    try:
        rule = yaml.safe_load(cleaned)
        if isinstance(rule, dict) and "prompt" in rule:
            return {"redundant": False, "rule": rule}
    except yaml.YAMLError:
        pass

    return None


# ── File matching ────────────────────────────────────────────────────

def _glob_repo_files(scope_hint: str, project_root: str) -> list[str]:
    """Find files matching scope_hint in the repo.

    Handles both proper globs ('src/billing/**') and free-text hints
    ('billing module') by falling back to keyword-based directory search.
    """
    import glob as glob_mod

    # If it looks like a glob pattern, use glob.glob with recursive=True
    if "*" in scope_hint or "/" in scope_hint or "." in scope_hint:
        pattern = scope_hint
        if not pattern.endswith("*"):
            pattern = pattern.rstrip("/") + "/**"
        full_pattern = os.path.join(project_root, pattern)
        matched = []
        for path in glob_mod.glob(full_pattern, recursive=True):
            if os.path.isfile(path):
                rel = os.path.relpath(path, project_root)
                # Skip hidden dirs and common non-source dirs
                parts = rel.split(os.sep)
                if any(p.startswith(".") or p in ("node_modules", "venv", ".venv", "__pycache__") for p in parts):
                    continue
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

    matched = []
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


# ── Reflect pipeline ─────────────────────────────────────────────────

def reflect(transcript_path: str, session_id: str,
            config: dict, config_dir: str, scribe_dir: str,
            session_dir: str) -> None:
    """Full --reflect pipeline: analyze transcript for conventions, validate, draft."""
    from sentinel_lock import acquire_lock, release_lock, LockPriority

    scribe_cfg = config.get("scribe", SCRIBE_DEFAULTS)
    backend, _ = resolve_backend(config, override_backend=scribe_cfg.get("backend"))
    extraction_model = _resolve_model(scribe_cfg, config, "extraction")
    synthesis_model = _resolve_model(scribe_cfg, config, "synthesis")
    guidance = scribe_cfg.get("guidance")
    thresholds = scribe_cfg.get("thresholds", {})
    extraction_confidence = thresholds.get("extraction_confidence", 0.7)

    # 1. Read and compact transcript
    budget = scribe_cfg.get("transcript_budget_chars", 4000)
    transcript_text = read_compacted_transcript(transcript_path, budget_chars=budget)
    if not transcript_text.strip():
        log_llm(config, "scribe", "reflect", extraction_model, 0,
                   backend=backend, response="empty_transcript")
        return

    # 2. Load session summary (optional context)
    summary = None
    summary_path = os.path.join(session_dir, "summary.json")
    try:
        with open(summary_path) as f:
            summary = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 3. Phase 1: Extraction
    lock_path = os.path.join(session_dir, "ollama.lock")
    os.makedirs(session_dir, exist_ok=True)
    fd = None
    if backend == "ollama":
        fd = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
        if fd is None:
            log_llm(config, "scribe", "reflect_extraction", extraction_model, 0,
                       backend=backend, error="lock_timeout")
            return

    t0 = time.time()
    try:
        prompt = build_transcript_extraction_prompt(transcript_text, summary, guidance)
        response = call_llm(prompt,
                            "You are a JSON-only responder. Always respond with valid JSON, no other text.",
                            extraction_model, backend, config,
                            think=False,
                            timeout_ms=scribe_cfg.get("extraction_timeout_ms", 15000),
                            num_predict=scribe_cfg.get("extraction_num_predict", 1000))
    except Exception as exc:
        log_llm(config, "scribe", "reflect_extraction", extraction_model,
                   (time.time() - t0) * 1000, backend=backend, error=str(exc))
        return
    finally:
        if fd is not None:
            release_lock(fd)
    log_llm(config, "scribe", "reflect_extraction", extraction_model,
               (time.time() - t0) * 1000, backend=backend, response=response)

    conventions = parse_extraction_response(response)
    if not conventions:
        return

    # 4. Phase 2: Validate each convention
    rules_dir = config.get("rules_dir", os.path.join(config_dir, "rules"))
    drafts_dir = config.get("drafts_dir", os.path.join(config_dir, "drafts"))
    project_root = os.path.dirname(os.path.dirname(config_dir))
    active_rules = load_active_rules(rules_dir)
    # Include existing drafts so the LLM doesn't re-propose the same concept
    draft_rules = load_active_rules(drafts_dir)
    all_known_rules = active_rules + draft_rules

    for conv in conventions:
        confidence = conv.get("confidence", 0.0)
        if confidence < extraction_confidence:
            continue

        source = conv.get("source", "unknown")
        observation = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "session_id": session_id,
            "statement": conv.get("statement", ""),
            "scope_hint": conv.get("scope_hint", "**"),
            "trigger_hint": conv.get("trigger_hint", "unknown"),
            "confidence": confidence,
            "evidence": conv.get("evidence", ""),
            "drafted": False,
        }
        append_observation(scribe_dir, observation)

        # Phase 2A: Structural dedup
        if is_dismissed(scribe_dir, conv.get("scope_hint", "**"),
                        conv.get("trigger_hint", "unknown")):
            continue

        # Phase 2B: Semantic dedup + synthesis (LLM judge)
        scope_hint = conv.get("scope_hint", "**")
        matched_files = _glob_repo_files(scope_hint, project_root)

        fd2 = None
        if backend == "ollama":
            fd2 = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
            if fd2 is None:
                log_llm(config, "scribe", "reflect_validation", synthesis_model, 0,
                           backend=backend, error="lock_timeout")
                continue

        t1 = time.time()
        try:
            val_prompt = build_validation_prompt(conv, all_known_rules, matched_files)
            val_response = call_llm(
                val_prompt,
                "You are a YAML-only responder. Always respond with valid YAML, no other text.",
                synthesis_model, backend, config,
                think=scribe_cfg.get("think", False),
                timeout_ms=scribe_cfg.get("synthesis_timeout_ms", 15000),
                num_predict=scribe_cfg.get("synthesis_num_predict", 1000))
        except Exception as exc:
            log_llm(config, "scribe", "reflect_validation", synthesis_model,
                       (time.time() - t1) * 1000, backend=backend, error=str(exc))
            continue
        finally:
            if fd2 is not None:
                release_lock(fd2)
        log_llm(config, "scribe", "reflect_validation", synthesis_model,
                   (time.time() - t1) * 1000, backend=backend, response=val_response)

        result = parse_validation_response(val_response)
        if result is None:
            continue
        if result.get("redundant"):
            log_llm(config, "scribe", "reflect_validation", synthesis_model, 0,
                       backend=backend, response=f"redundant: {result.get('reason', '')}")
            continue

        rule = result.get("rule", {})
        if rule and rule.get("prompt"):
            rule.setdefault("id", re.sub(r'[^a-z0-9-]', '-',
                            conv["statement"][:40].lower().strip()).strip("-"))
            draft_meta = {
                "source": source,
                "observed": 1,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "evidence": [conv.get("evidence", "")],
                "confidence": confidence,
                "synthesized": datetime.now(timezone.utc).isoformat(),
                "model": synthesis_model,
            }
            write_draft(drafts_dir, rule, draft_meta)


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
    backend, _ = resolve_backend(config, override_backend=scribe_cfg.get("backend"))
    extraction_model = _resolve_model(scribe_cfg, config, "extraction")
    synthesis_model = _resolve_model(scribe_cfg, config, "synthesis")
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
            fd = None
            if backend == "ollama":
                fd = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
                if fd is None:
                    log_llm(config, "scribe", "learn_extraction", extraction_model, 0,
                               backend=backend, error="lock_timeout")
                    continue
            t0 = time.time()
            try:
                prompt = build_doc_extraction_prompt(chunk, source_type, guidance)
                response = call_llm(prompt,
                                    "You are a JSON-only responder. Always respond with valid JSON, no other text.",
                                    extraction_model, backend, config,
                                    think=False,
                                    timeout_ms=scribe_cfg.get("extraction_timeout_ms", 15000),
                                    num_predict=scribe_cfg.get("extraction_num_predict", 1000))
            except Exception as exc:
                log_llm(config, "scribe", "learn_extraction", extraction_model,
                           (time.time() - t0) * 1000, backend=backend, error=str(exc))
                continue
            finally:
                if fd is not None:
                    release_lock(fd)
            log_llm(config, "scribe", "learn_extraction", extraction_model,
                       (time.time() - t0) * 1000, backend=backend, response=response)

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
                rules_dir = config.get("rules_dir", os.path.join(config_dir, "rules"))
                drafts_dir = config.get("drafts_dir", os.path.join(config_dir, "drafts"))

                matched_files = _glob_repo_files(scope_hint, project_root)
                active_rules = load_active_rules(rules_dir)
                draft_rules = load_active_rules(drafts_dir)
                all_known_rules = active_rules + draft_rules
                sample_rules = all_known_rules[:3]

                fd2 = None
                if backend == "ollama":
                    fd2 = acquire_lock(lock_path, LockPriority.P3_SCRIBE)
                    if fd2 is None:
                        log_llm(config, "scribe", "learn_synthesis", synthesis_model, 0,
                                   backend=backend, error="lock_timeout")
                        continue
                t1 = time.time()
                try:
                    synth_prompt = build_synthesis_prompt(conv, matched_files, sample_rules)
                    synth_response = call_llm(
                        synth_prompt,
                        "You are a YAML-only responder. Always respond with valid YAML, no other text.",
                        synthesis_model, backend, config,
                        think=scribe_cfg.get("think", False),
                        timeout_ms=scribe_cfg.get("synthesis_timeout_ms", 15000),
                        num_predict=scribe_cfg.get("synthesis_num_predict", 1000))
                except Exception as exc:
                    log_llm(config, "scribe", "learn_synthesis", synthesis_model,
                               (time.time() - t1) * 1000, backend=backend, error=str(exc))
                    continue
                finally:
                    if fd2 is not None:
                        release_lock(fd2)
                log_llm(config, "scribe", "learn_synthesis", synthesis_model,
                           (time.time() - t1) * 1000, backend=backend, response=synth_response)

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
                        "model": synthesis_model,
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

    log_model = _resolve_model(scribe_cfg, config, "extraction")
    log_backend, _ = resolve_backend(config, override_backend=scribe_cfg.get("backend"))

    if "--reflect" in sys.argv:
        try:
            raw_data = json.loads(sys.stdin.read())
        except Exception:
            log_llm(config, "scribe", "reflect", log_model, 0,
                       backend=log_backend, error="stdin_parse_failed")
            sys.exit(0)

        session_id = raw_data.get("session_id", "unknown")
        transcript_path = raw_data.get("transcript_path", "")
        if not transcript_path:
            log_llm(config, "scribe", "reflect", log_model, 0,
                       backend=log_backend, error="no_transcript_path")
            sys.exit(0)

        session_d = _session_dir(session_id, config_dir)
        reflect(
            transcript_path=transcript_path,
            session_id=session_id,
            config=config,
            config_dir=config_dir,
            scribe_dir=scribe_d,
            session_dir=session_d,
        )

    elif "--learn" in sys.argv:
        if not scribe_cfg.get("sources", {}).get("documentation", True):
            sys.exit(0)
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
