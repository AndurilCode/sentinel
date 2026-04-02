"""Tests for sentinel_backends module.

Tests follow TDD: written before implementation passes.
"""
import io
import json
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

import pytest

import sentinel_backends
from sentinel_backends import call_llm, resolve_backend


class TestResolveBackend(unittest.TestCase):
    """Tests for resolve_backend()."""

    def test_global_default_claude(self):
        """Global default: config has backend=claude with backends.claude.model=haiku."""
        config = {
            "backend": "claude",
            "backends": {
                "claude": {"model": "haiku"},
            },
        }
        backend, model = resolve_backend(config)
        self.assertEqual(backend, "claude")
        self.assertEqual(model, "haiku")

    def test_per_rule_override(self):
        """Per-rule override: config has backend=ollama but override_backend/model passed."""
        config = {
            "backend": "ollama",
            "backends": {
                "ollama": {"model": "gemma3:4b"},
            },
        }
        backend, model = resolve_backend(config, override_backend="claude", override_model="opus")
        self.assertEqual(backend, "claude")
        self.assertEqual(model, "opus")

    def test_backward_compat_model_only(self):
        """Backward compat: config has only model key (no backends key)."""
        config = {"model": "gemma3:4b"}
        backend, model = resolve_backend(config)
        self.assertEqual(backend, "ollama")
        self.assertEqual(model, "gemma3:4b")

    def test_model_only_override(self):
        """Model-only override: config has ollama backend, but override_model is passed."""
        config = {
            "backend": "ollama",
            "backends": {
                "ollama": {"model": "gemma3:4b"},
            },
        }
        backend, model = resolve_backend(config, override_model="gemma3:12b")
        self.assertEqual(backend, "ollama")
        self.assertEqual(model, "gemma3:12b")


class TestCallLlmOllama(unittest.TestCase):
    """Tests for call_llm dispatching to _call_ollama."""

    def test_call_llm_ollama_returns_content(self):
        """call_llm with ollama backend calls Ollama and returns content."""
        fake_response_body = json.dumps({
            "message": {"content": '{"violation": false}'}
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        config = {
            "backends": {
                "ollama": {"url": "http://localhost:11434"},
            },
            "timeout_ms": 5000,
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = call_llm(
                "test prompt",
                "system prompt",
                "gemma3:4b",
                "ollama",
                config,
            )

        self.assertIn("violation", result)


class TestCallLlmUnknownBackend(unittest.TestCase):
    """Tests for call_llm raising on unknown backend."""

    def test_unknown_backend_raises_value_error(self):
        """call_llm with an unknown backend raises ValueError."""
        with self.assertRaises(ValueError):
            call_llm("prompt", "system", "model", "unknown_backend", {})


class TestInitOllamaSemaphore(unittest.TestCase):
    """Tests for init_ollama_semaphore."""

    def test_sets_module_semaphore(self):
        """init_ollama_semaphore sets module-level _ollama_semaphore."""
        sentinel_backends.init_ollama_semaphore(3)
        self.assertIsNotNone(sentinel_backends._ollama_semaphore)
        # Semaphore with concurrency 3: should allow 3 acquires without blocking
        sem = sentinel_backends._ollama_semaphore
        sem.acquire()
        sem.acquire()
        sem.acquire()
        # 4th acquire should block — check it's not immediately available
        acquired = sem.acquire(blocking=False)
        self.assertFalse(acquired)
        # Clean up
        sem.release()
        sem.release()
        sem.release()


class TestCallLlmClaude(unittest.TestCase):
    """Tests for call_llm dispatching to _call_claude."""

    def test_call_llm_claude(self):
        """Claude backend runs claude CLI with correct args."""
        mock_result = MagicMock()
        mock_result.stdout = '{"violation": false, "confidence": 0.8, "reason": "ok"}'
        mock_result.returncode = 0

        config = {
            "timeout_ms": 10000,
            "backends": {"claude": {"model": "haiku"}},
        }

        with patch("sentinel_backends.subprocess.run", return_value=mock_result) as mock_run:
            result = call_llm("test prompt", "system prompt", "haiku", "claude", config)

        self.assertIn("violation", result)
        args = mock_run.call_args
        cmd = args[0][0]
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("--print", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("haiku", cmd)
        self.assertIn("--system-prompt", cmd)
        self.assertIn("--no-session-persistence", cmd)

    def test_call_llm_claude_timeout(self):
        """Claude backend raises on subprocess timeout."""
        config = {"timeout_ms": 5000, "backends": {}}

        with patch("sentinel_backends.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                call_llm("prompt", "system", "haiku", "claude", config)

    def test_call_llm_claude_not_found(self):
        """Claude backend raises when binary not on PATH."""
        config = {"timeout_ms": 5000, "backends": {}}

        with patch("sentinel_backends.subprocess.run",
                   side_effect=FileNotFoundError("claude not found")):
            with self.assertRaises(FileNotFoundError):
                call_llm("prompt", "system", "haiku", "claude", config)


# Pytest-style versions of the same tests (as specified in task)

def test_call_llm_claude():
    """Claude backend runs claude CLI with correct args."""
    mock_result = MagicMock()
    mock_result.stdout = '{"violation": false, "confidence": 0.8, "reason": "ok"}'
    mock_result.returncode = 0

    config = {
        "timeout_ms": 10000,
        "backends": {"claude": {"model": "haiku"}},
    }

    with patch("sentinel_backends.subprocess.run", return_value=mock_result) as mock_run:
        result = call_llm("test prompt", "system prompt", "haiku", "claude", config)

    assert "violation" in result
    args = mock_run.call_args
    cmd = args[0][0]
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--print" in cmd
    assert "--model" in cmd
    assert "haiku" in cmd
    assert "--system-prompt" in cmd
    assert "--no-session-persistence" in cmd


def test_call_llm_claude_timeout():
    """Claude backend raises on subprocess timeout."""
    config = {"timeout_ms": 5000, "backends": {}}

    with patch("sentinel_backends.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)):
        with pytest.raises(subprocess.TimeoutExpired):
            call_llm("prompt", "system", "haiku", "claude", config)


def test_call_llm_claude_not_found():
    """Claude backend raises when binary not on PATH."""
    config = {"timeout_ms": 5000, "backends": {}}

    with patch("sentinel_backends.subprocess.run",
               side_effect=FileNotFoundError("claude not found")):
        with pytest.raises(FileNotFoundError):
            call_llm("prompt", "system", "haiku", "claude", config)


if __name__ == "__main__":
    unittest.main()
