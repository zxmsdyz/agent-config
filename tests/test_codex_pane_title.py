"""Codex tmux 自动命名模式判定测试。"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / ".codex/bin/codex-pane-title.py"
SPEC = importlib.util.spec_from_file_location("codex_pane_title", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CodexSubcommandTest(unittest.TestCase):
    def test_interactive_tui_has_no_subcommand(self) -> None:
        self.assertIsNone(MODULE._codex_subcommand(["/opt/codex", "--dangerously-bypass-approvals-and-sandbox"]))

    def test_exec_is_non_interactive(self) -> None:
        self.assertEqual(
            MODULE._codex_subcommand(["/opt/codex", "exec", "-s", "read-only", "审查这个 change"]),
            "exec",
        )

    def test_exec_after_global_options_is_non_interactive(self) -> None:
        self.assertEqual(
            MODULE._codex_subcommand(["/opt/codex", "-c", "model='gpt-5.6-sol'", "exec", "审查"]),
            "exec",
        )

    def test_review_is_non_interactive(self) -> None:
        self.assertEqual(MODULE._codex_subcommand(["/opt/codex", "review", "--uncommitted"]), "review")

    def test_initial_prompt_is_still_interactive(self) -> None:
        self.assertIsNone(MODULE._codex_subcommand(["/opt/codex", "帮我修复 pane 名称"]))


class TitleCleaningTest(unittest.TestCase):
    def test_removes_spaces_and_punctuation(self) -> None:
        self.assertEqual(MODULE._clean_title("优化 Codex 窗格命名！"), "优化Codex窗格命名")

    def test_limits_title_to_twelve_characters(self) -> None:
        self.assertEqual(MODULE._clean_title("一二三四五六七八九十十一十二十三"), "一二三四五六七八九十十一")

    def test_rejects_punctuation_only_title(self) -> None:
        self.assertEqual(MODULE._clean_title("？！……"), "")


class FirstPromptInstructionTest(unittest.TestCase):
    def test_interactive_first_prompt_requests_semantic_title(self) -> None:
        event = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "修复窗口名"})
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=True),
            patch.object(MODULE, "_automatic_rename_enabled", return_value=True),
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._emit_first_prompt_instruction(), 0)

        output = json.loads(stdout.getvalue())
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("6 至 12 个字符", context)
        self.assertIn("不能直接截取用户原句", context)
        self.assertIn("codex-pane-title.py --set", context)

    def test_non_interactive_codex_does_not_request_title(self) -> None:
        event = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "审查"})
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=False),
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._emit_first_prompt_instruction(), 0)

        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
