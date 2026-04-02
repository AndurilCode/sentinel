"""sentinel_backends.py — Multi-backend LLM adapter for Sentinel.

Provides:
- resolve_backend(config, override_backend, override_model) -> (backend, model)
- call_llm(prompt, system_prompt, model, backend, config, **kwargs) -> str
- _call_ollama(prompt, system_prompt, model, config, *, think, json_format, ...) -> str
- init_ollama_semaphore(concurrency) — initialize module-level semaphore

Currently supports the "ollama" backend. Claude and Copilot backends will be
added in subsequent tasks.
"""
import json
import threading
import urllib.request
from typing import Optional

# ── Semaphore ──────────────────────────────────────────────────────────────

# Gates Ollama HTTP calls to avoid GPU contention.
# Initialized via init_ollama_semaphore() (called from main config setup).
_ollama_semaphore: Optional[threading.Semaphore] = None


def init_ollama_semaphore(concurrency: int) -> None:
    """Set the module-level Ollama concurrency semaphore."""
    global _ollama_semaphore
    _ollama_semaphore = threading.Semaphore(concurrency)


# ── Backend resolution ─────────────────────────────────────────────────────

def resolve_backend(
    config: dict,
    override_backend: Optional[str] = None,
    override_model: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve which backend and model to use.

    Priority (highest to lowest):
    1. override_backend / override_model (per-rule overrides)
    2. config["backend"] / config["backends"][backend]["model"]
    3. config["model"] (backward-compat, pre-backends key)
    4. Defaults: backend="ollama", model="gemma3:4b"

    Returns:
        (backend_name, model_name)
    """
    backend = override_backend or config.get("backend", "ollama")
    backends_cfg = config.get("backends", {})
    backend_cfg = backends_cfg.get(backend, {})
    model = override_model or backend_cfg.get("model") or config.get("model", "gemma3:4b")
    return backend, model


# ── Backend dispatch ───────────────────────────────────────────────────────

def call_llm(
    prompt: str,
    system_prompt: str,
    model: str,
    backend: str,
    config: dict,
    **kwargs,
) -> str:
    """Dispatch to the appropriate LLM backend.

    Args:
        prompt:        User message content.
        system_prompt: System message content.
        model:         Model identifier (backend-specific).
        backend:       Backend name — currently only "ollama" is supported.
        config:        Full Sentinel config dict.
        **kwargs:      Passed through to the backend implementation.

    Returns:
        Raw string content from the LLM response.

    Raises:
        ValueError: If backend is not recognised.
    """
    if backend == "ollama":
        return _call_ollama(prompt, system_prompt, model, config, **kwargs)
    raise ValueError(f"Unknown backend: {backend!r}. Supported: 'ollama'")


# ── Ollama backend ─────────────────────────────────────────────────────────

def _call_ollama(
    prompt: str,
    system_prompt: str,
    model: str,
    config: dict,
    *,
    think: bool = False,
    json_format: bool = True,
    timeout_ms: Optional[int] = None,
    num_predict: Optional[int] = None,
) -> str:
    """Send a chat request to the local Ollama server and return the content.

    Handles semaphore gating, payload construction, and HTTP transport.
    Raises on network/timeout errors — caller decides how to handle.

    Args:
        prompt:        User message.
        system_prompt: System message.
        model:         Ollama model tag (e.g. "gemma3:4b").
        config:        Sentinel config dict.
        think:         Enable chain-of-thought / think mode.
        json_format:   Instruct Ollama to constrain output to JSON.
        timeout_ms:    Override config timeout (milliseconds).
        num_predict:   Max tokens to generate (overrides default heuristic).
    """
    ollama_cfg = config.get("backends", {}).get("ollama", {})
    url_base = ollama_cfg.get("url") or config.get("ollama_url", "http://localhost:11434")
    url = f"{url_base}/api/chat"

    effective_timeout_ms = timeout_ms or config.get("timeout_ms", 5000)
    timeout_s = effective_timeout_ms / 1000

    default_num_predict = num_predict or (1000 if (not json_format or think) else 300)

    payload_dict: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ],
        "stream": False,
        "think":  think,
        "options": {
            "num_predict": default_num_predict,
            "temperature": 0.1,
        },
    }
    if json_format:
        payload_dict["format"] = "json"

    payload = json.dumps(payload_dict).encode()

    sem = _ollama_semaphore
    if sem:
        sem.acquire()
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())
    finally:
        if sem:
            sem.release()

    return body.get("message", {}).get("content", "")
