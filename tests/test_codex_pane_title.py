"""Codex tmux 自动命名模式判定测试。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
