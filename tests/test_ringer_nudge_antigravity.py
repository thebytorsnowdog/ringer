#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "ringer_nudge_antigravity.py"


def tool_call(name: str, **args: str) -> dict[str, object]:
    return {"name": name, "args": {key: json.dumps(value) for key, value in args.items()}}


def planner(*calls: dict[str, object]) -> dict[str, object]:
    return {"type": "PLANNER_RESPONSE", "tool_calls": list(calls)}


class AntigravityNudgeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state" / "nested" / "nudge-state.json"
        self.repo = self.root / "repo"
        self.artifacts = self.root / "brain" / "conv-1"
        self.transcript = self.root / "transcript.jsonl"

    def write_transcript(self, entries: list[object]) -> None:
        with self.transcript.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(entry if isinstance(entry, str) else json.dumps(entry))
                fh.write("\n")

    def payload(self, conversation_id: str = "conv-1") -> dict[str, str]:
        return {
            "conversationId": conversation_id,
            "transcriptPath": str(self.transcript),
            "artifactDirectoryPath": str(self.artifacts),
        }

    def run_hook(self, payload: object | str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["RINGER_NUDGE_STATE"] = str(self.state)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        output = json.loads(lines[0])
        self.assertIsInstance(output, dict)
        self.assertNotIn("decision", output)
        self.assertNotIn("hookSpecificOutput", output)
        return result, output

    def repo_edit(self, name: str, tool: str = "replace_file_content") -> dict[str, object]:
        return tool_call(tool, TargetFile=str(self.repo / name), Instruction="edit")

    def assert_nudge(self, output: dict[str, object]) -> str:
        self.assertEqual(set(output), {"injectSteps"})
        steps = output["injectSteps"]
        self.assertIsInstance(steps, list)
        self.assertEqual(len(steps), 1)
        message = steps[0]["ephemeralMessage"]
        self.assertIn("Ringer", message)
        self.assertIn("a single task is a one-task manifest", message)
        self.assertIn("explicitly asked for inline work, that request wins", message)
        self.assertIn("never authorises a model, paid spend or broader access", message)
        return message

    def edit_loop_transcript(self) -> list[object]:
        return [
            {"type": "USER_INPUT", "content": "fix it"},
            planner(self.repo_edit("a.py")),
            planner(self.repo_edit("b.py", "multi_replace_file_content")),
            planner(self.repo_edit("c.py", "write_to_file")),
        ]

    def test_edit_loop_of_three_repo_edits_nudges(self) -> None:
        self.write_transcript(self.edit_loop_transcript())
        _, output = self.run_hook(self.payload())
        message = self.assert_nudge(output)
        self.assertIn("edit loop", message)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["nudged"]["conv-1"]["kind"], "edit-loop")

    def test_two_repo_edits_are_silent(self) -> None:
        self.write_transcript([planner(self.repo_edit("a.py")), planner(self.repo_edit("b.py"))])
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})
        self.assertFalse(self.state.exists())

    def test_second_call_for_same_conversation_is_silent(self) -> None:
        self.write_transcript(self.edit_loop_transcript())
        _, first = self.run_hook(self.payload())
        self.assert_nudge(first)
        self.write_transcript(
            self.edit_loop_transcript() + [planner(tool_call("run_command", CommandLine="claude -p hi"))]
        )
        _, second = self.run_hook(self.payload())
        self.assertEqual(second, {})
        _, other = self.run_hook(self.payload("conv-2"))
        self.assert_nudge(other)

    def test_edits_under_artifact_dir_are_silent(self) -> None:
        self.write_transcript(
            [
                planner(tool_call("write_to_file", TargetFile=str(self.artifacts / "task.md"))),
                planner(tool_call("replace_file_content", TargetFile=str(self.artifacts / "plan.md"))),
                planner(tool_call("multi_replace_file_content", TargetFile=str(self.artifacts / "sub" / "x.md"))),
                planner(tool_call("write_to_file", TargetFile=str(self.artifacts / "walkthrough.md"))),
            ]
        )
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_sibling_of_artifact_dir_is_not_exempt(self) -> None:
        sibling = str(self.artifacts) + "-other"
        self.write_transcript(
            [planner(tool_call("write_to_file", TargetFile=f"{sibling}/{name}")) for name in ("a", "b", "c")]
        )
        _, output = self.run_hook(self.payload())
        self.assert_nudge(output)

    def test_live_ringer_run_after_edits_resets(self) -> None:
        self.write_transcript(
            [
                planner(self.repo_edit("a.py")),
                planner(self.repo_edit("b.py")),
                planner(self.repo_edit("c.py")),
                planner(tool_call("run_command", CommandLine="python3 ringer.py run swarm.yaml")),
                planner(self.repo_edit("d.py")),
            ]
        )
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_dry_run_and_baseline_do_not_reset(self) -> None:
        for flag in ("--dry-run", "--baseline"):
            with self.subTest(flag=flag):
                self.write_transcript(
                    [
                        planner(self.repo_edit("a.py")),
                        planner(self.repo_edit("b.py")),
                        planner(tool_call("run_command", CommandLine=f"python3 ringer.py run swarm.yaml {flag}")),
                        planner(self.repo_edit("c.py")),
                    ]
                )
                _, output = self.run_hook(self.payload(f"conv{flag}"))
                self.assert_nudge(output)

    def test_direct_claude_print_call_nudges(self) -> None:
        self.write_transcript(
            [planner(tool_call("run_command", CommandLine="claude -p 'implement the parser'", Cwd=str(self.repo)))]
        )
        _, output = self.run_hook(self.payload())
        message = self.assert_nudge(output)
        self.assertIn("model call", message)

    def test_other_worker_clis_and_provider_calls_nudge(self) -> None:
        commands = [
            "cd /repo && codex exec 'fix tests'",
            "grok --single 'hello'",
            "opencode run 'do it'",
            "curl https://api.anthropic.com/v1/messages -d @body.json",
            "node scripts/persona-smoke.mjs",
        ]
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                self.write_transcript([planner(tool_call("run_command", CommandLine=command))])
                _, output = self.run_hook(self.payload(f"conv-model-{index}"))
                self.assert_nudge(output)

    def test_live_ringer_ask_is_not_a_model_call(self) -> None:
        self.write_transcript(
            [planner(tool_call("run_command", CommandLine="python3 ringer.py ask --engine claude 'what is X'"))]
        )
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_plain_shell_commands_never_trigger(self) -> None:
        self.write_transcript(
            [
                planner(
                    tool_call("run_command", CommandLine="ls -la"),
                    tool_call("run_command", CommandLine="git status && git log -p"),
                    tool_call("run_command", CommandLine="cat claude.md"),
                    tool_call("run_command", CommandLine="grep -r claude src"),
                )
            ]
        )
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_malformed_lines_and_raw_args_are_tolerated(self) -> None:
        self.write_transcript(
            [
                "{not json",
                "",
                {"type": "PLANNER_RESPONSE", "tool_calls": "oops"},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "claude -p raw"}}]},
            ]
        )
        _, output = self.run_hook(self.payload())
        self.assert_nudge(output)

    def test_only_last_400_lines_are_read(self) -> None:
        entries: list[object] = [planner(tool_call("run_command", CommandLine="claude -p early"))]
        entries.extend({"type": "USER_INPUT", "content": str(index)} for index in range(400))
        self.write_transcript(entries)
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_malformed_stdin_prints_empty_object(self) -> None:
        for stdin in ("", "not json", "[1, 2]", "{\"conversationId\": 5}"):
            with self.subTest(stdin=stdin):
                _, output = self.run_hook(stdin)
                self.assertEqual(output, {})

    def test_missing_transcript_prints_empty_object(self) -> None:
        _, output = self.run_hook(self.payload())
        self.assertEqual(output, {})

    def test_corrupt_state_file_is_recovered(self) -> None:
        self.state.parent.mkdir(parents=True)
        self.state.write_text("not json", encoding="utf-8")
        self.write_transcript(self.edit_loop_transcript())
        _, output = self.run_hook(self.payload())
        self.assert_nudge(output)
        _, again = self.run_hook(self.payload())
        self.assertEqual(again, {})

    def test_runtime_is_well_under_one_second(self) -> None:
        entries: list[object] = [planner(self.repo_edit(f"f{index}.py")) for index in range(2000)]
        self.write_transcript(entries)
        started = time.monotonic()
        _, output = self.run_hook(self.payload())
        elapsed = time.monotonic() - started
        self.assert_nudge(output)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
