"""One OpenRouter completion. No network and no second request."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engines import openrouter_worker as worker  # noqa: E402


class SingleRequestTests(unittest.TestCase):
    def test_body_has_one_max_tokens_and_no_tools(self) -> None:
        body = worker.completion_body("openrouter/z-ai/glm-5.3-flash", "hello", 64)
        self.assertEqual("z-ai/glm-5.3-flash", body["model"])
        self.assertEqual(64, body["max_tokens"])
        self.assertEqual([{"role": "user", "content": "hello"}], body["messages"])
        self.assertNotIn("tools", body)

    def test_oversize_prompt_does_not_call_the_provider(self) -> None:
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            raise AssertionError("provider called")

        code, line = worker.execute(
            ["--max-input-tokens", "3", "--max-tokens", "8", "-m", "openrouter/z-ai/glm-5.3-flash", "hello"],
            env={},
            opener=opener,
        )
        self.assertEqual(1, code)
        self.assertIn("input_bound", line)
        self.assertEqual([], calls)

    def test_provider_failure_is_not_retried(self) -> None:
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            raise TimeoutError("once")

        with mock.patch.object(worker, "load_openrouter_key", return_value="test-key"):
            code, line = worker.execute(
                ["--max-input-tokens", "100", "--max-tokens", "8", "-m", "openrouter/z-ai/glm-5.3-flash", "hi"],
                env={},
                opener=opener,
            )
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertNotIn("test-key", line)

    def test_one_response_writes_usage_and_the_text(self) -> None:
        payload = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout):
            self.assertEqual(120, timeout)
            self.assertNotIn(b"tools", request.data)
            self.assertIn(b'"max_tokens": 8', request.data)
            return Response(json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(worker, "load_openrouter_key", return_value="test-key"):
            cwd = Path(tmp)
            code, line = worker.execute(
                ["--cwd", str(cwd), "--max-input-tokens", "100", "--max-tokens", "8",
                 "-m", "openrouter/z-ai/glm-5.3-flash", "hi"],
                env={},
                opener=opener,
            )
            text = (cwd / "openrouter-output.txt").read_text(encoding="utf-8")
        self.assertEqual(0, code)
        self.assertEqual("ok", text)
        row = json.loads(line)
        self.assertEqual("turn.completed", row["type"])
        self.assertEqual(2, row["usage"]["input_tokens"])
        self.assertEqual(1, row["usage"]["output_tokens"])


if __name__ == "__main__":
    unittest.main()
