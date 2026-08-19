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


class ProcessInfoTest(unittest.TestCase):
    def test_macos_ps_fallback_parses_parent_and_argv(self) -> None:
        completed = MODULE.subprocess.CompletedProcess(
            [], 0, "  42 /opt/homebrew/bin/codex --model gpt-5.6-sol\n", ""
        )
        with (
            patch.object(MODULE.Path, "is_dir", return_value=False),
            patch.object(MODULE.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(
                MODULE._process_info(99),
                (["/opt/homebrew/bin/codex", "--model", "gpt-5.6-sol"], 42),
            )
        run.assert_called_once_with(
            ["ps", "-p", "99", "-o", "ppid=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_interactive_detection_can_walk_macos_processes(self) -> None:
        with patch.object(
            MODULE,
            "_process_info",
            side_effect=[(["python3", "hook.py"], 42), (["/opt/homebrew/bin/codex"], 1)],
        ):
            self.assertTrue(MODULE._is_interactive_codex_process(start_pid=99))


class TitleCleaningTest(unittest.TestCase):
    def test_removes_spaces_and_punctuation(self) -> None:
        self.assertEqual(MODULE._clean_title("优化 Codex 窗格命名！"), "优化Codex窗格命名")

    def test_limits_title_to_twenty_four_characters(self) -> None:
        self.assertEqual(
            MODULE._clean_title("一二三四五六七八九十甲乙丙丁戊己庚辛壬癸天地玄黄宇"),
            "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸天地玄黄",
        )

    def test_rejects_punctuation_only_title(self) -> None:
        self.assertEqual(MODULE._clean_title("？！……"), "")


class FirstPromptInstructionTest(unittest.TestCase):
    def test_interactive_first_prompt_requests_semantic_title(self) -> None:
        event = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-new",
                "prompt": "修复窗口名",
            }
        )
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=True),
            patch.object(MODULE, "_ensure_session", return_value="pending"),
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._handle_hook(), 0)

        output = json.loads(stdout.getvalue())
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("14 至 24 个字符", context)
        self.assertIn("venue 或交易所", context)
        self.assertIn("策略类型或下单通道", context)
        self.assertIn("任务动作", context)
        self.assertIn("不能直接截取用户原句", context)
        self.assertIn("codex-pane-title.py --session-id", context)
        self.assertIn("--set '<短标题>'", context)
        self.assertIn("--session-id session-new", context)

    def test_same_titled_session_does_not_rename_again(self) -> None:
        event = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-current",
                "prompt": "继续",
            }
        )
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=True),
            patch.object(MODULE, "_ensure_session", return_value="titled"),
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._handle_hook(), 0)

        self.assertEqual(stdout.getvalue(), "")

    def test_session_start_resets_but_does_not_inject_prompt_context(self) -> None:
        event = json.dumps(
            {"hook_event_name": "SessionStart", "session_id": "session-after-clear"}
        )
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=True),
            patch.object(MODULE, "_ensure_session", return_value="pending") as ensure,
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._handle_hook(), 0)

        ensure.assert_called_once_with("%9", "session-after-clear")
        self.assertEqual(stdout.getvalue(), "")

    def test_non_interactive_codex_does_not_request_title(self) -> None:
        event = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "review-session",
                "prompt": "审查",
            }
        )
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_is_interactive_codex_process", return_value=False),
            patch.object(MODULE.sys, "stdin", io.StringIO(event)),
            redirect_stdout(stdout),
        ):
            self.assertEqual(MODULE._handle_hook(), 0)

        self.assertEqual(stdout.getvalue(), "")


class SessionStateTest(unittest.TestCase):
    def test_partial_state_is_repaired_to_pending(self) -> None:
        with (
            patch.object(
                MODULE,
                "_pane_option",
                side_effect=["session-current", None],
            ),
            patch.object(MODULE, "_set_pane_option", return_value=True) as set_option,
        ):
            self.assertEqual(MODULE._ensure_session("%9", "session-current"), "pending")

        set_option.assert_called_once_with("%9", MODULE._PANE_STATUS_OPTION, "pending")

    def test_new_session_resets_window_and_records_pending_state(self) -> None:
        completed = MODULE.subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(MODULE, "_pane_option", return_value="session-old"),
            patch.object(MODULE, "_tmux", return_value=completed) as tmux,
            patch.object(MODULE, "_lock_window_title", return_value=True),
            patch.object(MODULE, "_set_pane_option", return_value=True) as set_option,
        ):
            self.assertEqual(MODULE._ensure_session("%9", "session-new"), "pending")

        tmux.assert_called_once_with("rename-window", "-t", "%9", "Codex")
        set_option.assert_any_call("%9", MODULE._PANE_SESSION_OPTION, "session-new")
        set_option.assert_any_call("%9", MODULE._PANE_STATUS_OPTION, "pending")

    def test_stale_turn_cannot_overwrite_new_session_title(self) -> None:
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_pane_option", return_value="session-after-clear"),
            patch.object(MODULE, "_tmux") as tmux,
        ):
            self.assertEqual(MODULE._set_title("旧会话标题", "session-before-clear"), 0)

        tmux.assert_not_called()

    def test_successful_title_marks_current_session_titled(self) -> None:
        completed = MODULE.subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%9"}, clear=False),
            patch.object(MODULE, "_pane_option", return_value="session-current"),
            patch.object(MODULE, "_tmux", return_value=completed),
            patch.object(MODULE, "_lock_window_title", return_value=True),
            patch.object(MODULE, "_set_pane_option", return_value=True) as set_option,
        ):
            self.assertEqual(MODULE._set_title("修复 Codex 窗格命名", "session-current"), 0)

        set_option.assert_any_call("%9", MODULE._PANE_TITLE_OPTION, "修复Codex窗格命名")
        set_option.assert_any_call("%9", MODULE._PANE_STATUS_OPTION, "titled")


if __name__ == "__main__":
    unittest.main()
