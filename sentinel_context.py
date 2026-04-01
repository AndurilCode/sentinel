#!/usr/bin/env python3
"""
Sentinel Context Accumulator — maintains rolling session summary.

Runs as an async Stop hook. Reads the Claude Code session transcript,
compacts events, and calls Ollama to produce a bounded summary of
what the agent is working on. The summary is consumed by sentinel.py
--post mode for scope-aware info rule synthesis.

State: .sentinel/sessions/<session_id>/summary.json + checkpoint
"""

import sys
import os
import json
import ast
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional
from sentinel_log import log_ollama

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "pyyaml"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import yaml

from sentinel_lock import acquire_lock, release_lock, LockPriority

# Meta tools to skip — not relevant to task scope
_SKIP_TOOLS = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput",
               "Skill", "ToolSearch", "SendMessage", "TaskStop"}


def compact_event(entry: dict, state: Optional[dict] = None) -> Optional[dict]:
    """Extract task-relevant info from a transcript entry. Returns None to skip.

    Args:
        entry: A transcript entry dict.
        state: Optional mutable dict with key "pending_tools" (list). When
               provided, tool_use ids/names are tracked and resolved when the
               next tool_result user entry arrives, annotating results with
               error/success status.
    """
    t = entry.get("type", "")
    msg = entry.get("message", {})
    ts = entry.get("timestamp", "")

    if t == "user" and isinstance(msg.get("content"), str):
        content = msg["content"]
        stripped = content.strip()
        if stripped.startswith("[{") or stripped.startswith("[{'"):
            # Potential tool_result payload — process only when state is provided
            if state is None:
                return None
            # Try to parse as JSON first, fall back to ast.literal_eval
            try:
                items = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                try:
                    items = ast.literal_eval(stripped)
                except Exception:
                    return None
            if not isinstance(items, list):
                return None
            parts = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "tool_result":
                    continue
                is_error = item.get("is_error", False)
                raw_content = item.get("content", "")
                if isinstance(raw_content, list):
                    # content may be a list of content blocks
                    text_parts = [
                        block.get("text", "") for block in raw_content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    raw_content = " ".join(text_parts)
                first_line = raw_content.split("\n")[0][:150] if raw_content else ""
                if is_error:
                    parts.append(f"→ ERROR: {first_line}")
                else:
                    parts.append("→ OK")
            if not parts:
                return None
            return {"trigger": "tool_result", "ts": ts, "text": " ".join(parts)}
        return {"trigger": "user", "ts": ts, "text": content[:300]}

    elif t == "assistant":
        texts = []
        tools = []
        for c in msg.get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                text = c["text"].strip()
                if len(text) < 10:
                    continue
                texts.append(text[:200])
            elif c.get("type") == "tool_use":
                name = c.get("name", "")
                if name in _SKIP_TOOLS:
                    continue
                inp = c.get("input", {})
                if name in ("Read", "Glob", "Grep"):
                    compact_inp = inp.get("file_path") or inp.get("pattern") or inp.get("path", "")
                    tools.append(f"{name}({compact_inp[:80]})")
                elif name == "Bash":
                    tools.append(f"Bash({inp.get('command', '')[:100]})")
                elif name in ("Write", "Edit"):
                    tools.append(f"{name}({inp.get('file_path', '')})")
                elif name == "Agent":
                    tools.append(f"Agent({inp.get('description', '')})")
                else:
                    tools.append(f"{name}({json.dumps(inp)[:60]})")

        if not texts and not tools:
            return None

        parts = []
        if texts:
            parts.append(texts[0])
        if tools:
            parts.append(f"[tools: {', '.join(tools)}]")
        return {"trigger": "stop", "ts": ts, "text": " ".join(parts)}

    return None


def parse_transcript_entries(transcript_path: str, byte_offset: int = 0
                             ) -> tuple[list[dict], int]:
    """Read transcript from byte offset, return (compacted_events, new_offset)."""
    events = []
    state = {"pending_tools": []}
    with open(transcript_path, "r") as f:
        f.seek(byte_offset)
        while True:
            line = f.readline()
            if not line:
                break
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = compact_event(entry, state)
            if evt:
                events.append(evt)
        new_offset = f.tell()
    return events, new_offset


def build_accumulator_prompt(existing_summary: Optional[dict],
                             events: list[dict],
                             max_words: int = 150) -> str:
    """Build the prompt for the accumulator LLM."""
    batch_lines = [f"[{e['trigger']}] {e['text']}" for e in events]
    batch_text = "\n".join(batch_lines)
    if len(batch_text) > 3000:
        batch_text = batch_text[:400] + "\n...[truncated]...\n" + batch_text[-2600:]

    if existing_summary:
        return f"""You are a session context accumulator. Update the summary with new events.
Be concise and factual. Max {max_words} words total.

CURRENT SUMMARY:
{json.dumps(existing_summary)}

NEW EVENTS ({len(events)}):
{batch_text}

Return JSON only: {{"task_scope": "what the user is building", "progress": "what is done", "current_focus": "what is happening now"}}"""
    else:
        return f"""You are a session context accumulator. Summarize this session.
Be concise and factual. Max {max_words} words total.

EVENTS ({len(events)}):
{batch_text}

Return JSON only: {{"task_scope": "what the user is building", "progress": "what is done", "current_focus": "what is happening now"}}"""


def extract_json(text: str) -> Optional[dict]:
    """Robust JSON extraction from LLM output."""
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        return json.loads(cleaned.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def call_ollama(prompt: str, model: str, config: dict) -> str:
    """Call Ollama chat endpoint."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a JSON-only responder. Always respond with valid JSON, no other text."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 300},
    }).encode()

    url = f"{config.get('ollama_url', 'http://localhost:11434')}/api/chat"
    timeout_s = config.get("timeout_ms", 10000) / 1000

    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read())
    return body.get("message", {}).get("content", "")


def update_summary(transcript_path: str, session_dir: str,
                   config: dict) -> Optional[dict]:
    """Core accumulator logic. Returns updated summary or None."""
    ctx_config = config.get("context", {})
    min_events = ctx_config.get("min_events", 3)
    max_words = ctx_config.get("summary_max_words", 150)
    model = ctx_config.get("model", config.get("model", "gemma3:4b"))
    lock_timeout = ctx_config.get("lock_timeout_s", 30)

    os.makedirs(session_dir, exist_ok=True)
    checkpoint_path = os.path.join(session_dir, "checkpoint")
    summary_path = os.path.join(session_dir, "summary.json")
    lock_path = os.path.join(session_dir, "ollama.lock")

    # Read checkpoint
    byte_offset = 0
    try:
        with open(checkpoint_path) as f:
            byte_offset = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass

    # Parse new events
    events, new_offset = parse_transcript_entries(transcript_path, byte_offset)
    if len(events) < min_events:
        return None  # not enough new events

    # Read existing summary
    existing = None
    try:
        with open(summary_path) as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Wait for GPU lock
    fd = acquire_lock(lock_path, LockPriority.P2_ACCUMULATOR,
                      timeout_s=lock_timeout)
    if fd is None:
        log_ollama(config, "context", "accumulate", model, 0,
                   error="lock_timeout")
        return None  # timed out, skip this update

    t0 = time.time()
    try:
        prompt = build_accumulator_prompt(existing, events, max_words)
        content = call_ollama(prompt, model, config)
    except Exception as exc:
        elapsed = (time.time() - t0) * 1000
        log_ollama(config, "context", "accumulate", model, elapsed,
                    error=str(exc))
        release_lock(fd)
        return None
    finally:
        release_lock(fd)
    elapsed = (time.time() - t0) * 1000
    log_ollama(config, "context", "accumulate", model, elapsed,
               response=content)

    # Parse and write
    summary = extract_json(content)
    if summary is None:
        return None

    with open(summary_path, "w") as f:
        json.dump(summary, f)
    with open(checkpoint_path, "w") as f:
        f.write(str(new_offset))

    return summary


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


def load_config(sentinel_dir: str) -> dict:
    """Load config.yaml with defaults."""
    cfg = {
        "model": "gemma3:4b",
        "ollama_url": "http://localhost:11434",
        "timeout_ms": 10000,
        "context": {
            "enabled": True,
            "model": "gemma3:4b",
            "min_events": 3,
            "lock_timeout_s": 30,
            "summary_max_words": 150,
        },
    }
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
    return cfg


def main():
    # Read hook event from stdin
    try:
        raw_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = raw_data.get("session_id", "unknown")
    transcript_path = raw_data.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    config_dir = _find_config_dir()
    if not config_dir:
        sys.exit(0)

    config = load_config(config_dir)

    # Check if context accumulator is enabled
    if not config.get("context", {}).get("enabled", True):
        sys.exit(0)

    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', session_id)
    # Derive session dir from project root (where .claude/ lives), not config_dir
    project_root = os.path.dirname(os.path.dirname(config_dir))  # .claude/sentinel/ -> project root
    session_dir = os.path.join(project_root, ".sentinel", "sessions", safe_id)
    update_summary(transcript_path, session_dir, config)
    sys.exit(0)


if __name__ == "__main__":
    main()
