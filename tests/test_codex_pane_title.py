"""Codex 原生标题兜底 hook 的会话隔离测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".codex/bin/codex-pane-title.py"
HOOKS = ROOT / ".codex/hooks.json"
SESSION = "01a0de93-af8d-7021-a757-5aa719d81a51"
OTHER = "01a0e0e9-4196-7e53-b256-2ef46c6b98ae"
CHILD = "01a0e3d4-5684-7d01-84d0-0363bdb88ae9"


class PaneTitleHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.codex_home = Path(self.temp.name) / ".codex"
        self.codex_home.mkdir()
        self.capabilities = self.codex_home / "tui-thread-reference-capabilities"
        self.capabilities.mkdir()
        self.db = self.codex_home / "state_5.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, agent_path TEXT)")
            conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", [
                (SESSION, None, None),
                (OTHER, "原生已有名称", None),
                (CHILD, None, "/root/fix_titles"),
            ])
        for thread_id in (SESSION, OTHER, CHILD):
            (self.capabilities / thread_id).touch()

    def run_hook(self, event: str = "UserPromptSubmit", session_id: str = SESSION,
                 *, pane: str = "%999") -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(self.codex_home), "TMUX_PANE": pane})
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"hook_event_name": event, "session_id": session_id, "prompt": "修复窗口名"}),
            text=True, capture_output=True, check=False, env=env,
        )

    def test_unnamed_tui_gets_semantic_native_tool_instruction(self) -> None:
        before = self.db.stat().st_mtime_ns
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("14 至 24 个字符", context)
        self.assertIn("不能直接截取 prompt", context)
        self.assertIn("venue 或交易所", context)
        self.assertIn("策略类型或下单通道", context)
        self.assertIn("tools.mcp__codex_tui__set_thread_title", context)
        self.assertIn(f'"threadId":"{SESSION}"', context)
        self.assertNotIn("TMUX_PANE", context)
        self.assertEqual(self.db.stat().st_mtime_ns, before)

    def test_existing_native_name_wins_even_with_wrong_pane(self) -> None:
        self.assertEqual(self.run_hook(session_id=OTHER).stdout, "")

    def test_later_native_name_stops_fallback(self) -> None:
        self.assertTrue(self.run_hook().stdout)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE threads SET name = ? WHERE id = ?", ("原生晚到", SESSION))
        self.assertEqual(self.run_hook().stdout, "")

    def test_missing_capability_exec_review_and_subagent_are_silent(self) -> None:
        (self.capabilities / SESSION).unlink()
        self.assertEqual(self.run_hook().stdout, "")
        self.assertEqual(self.run_hook(session_id=CHILD).stdout, "")

    def test_clear_creates_independent_session_and_stale_id_cannot_target_it(self) -> None:
        first = json.loads(self.run_hook().stdout)["hookSpecificOutput"]["additionalContext"]
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE threads SET name = ? WHERE id = ?", ("旧会话标题", SESSION))
            conn.execute("UPDATE threads SET name = NULL WHERE id = ?", (OTHER,))
        second = json.loads(self.run_hook(session_id=OTHER).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f'"threadId":"{SESSION}"', first)
        self.assertIn(f'"threadId":"{OTHER}"', second)
        self.assertNotIn(OTHER, first)
        self.assertEqual(self.run_hook(session_id=SESSION).stdout, "")

    def test_other_events_invalid_ids_and_missing_db_are_silent(self) -> None:
        self.assertEqual(self.run_hook(event="SessionStart").stdout, "")
        self.assertEqual(self.run_hook(session_id="../unsafe").stdout, "")
        self.db.unlink()
        self.assertEqual(self.run_hook().stdout, "")

    def test_hooks_and_install_really_wire_title_helper(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]
        self.assertNotIn("SessionStart", hooks)
        command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("codex-pane-title.py", command)
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('link_file "$repo_root/.codex/bin/codex-pane-title.py"', installer)
        self.assertIn('link_file "$repo_root/.codex/bin/codex-thread-name.py"', installer)


if __name__ == "__main__":
    unittest.main()
