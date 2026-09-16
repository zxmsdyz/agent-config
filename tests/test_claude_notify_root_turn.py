"""Claude Code 完成通知的主 agent 过滤测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".claude/hooks/claude-notify-root-turn.py"
NOTIFY_SCRIPT = Path(__file__).parents[1] / ".claude/hooks/notify-done.sh"
SPEC = importlib.util.spec_from_file_location("claude_notify_root_turn", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


SESSION_ID = "93f3d8bd-4603-4686-b80a-464cac8a5808"
PROJECT_DIR = "/Users/simon/.claude/projects/-Users-simon-workspace-qflow"


def _stop_payload(**overrides: object) -> str:
    """生成与 Claude Code 2.1.273 实测一致的最小 Stop 事件。"""
    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "transcript_path": f"{PROJECT_DIR}/{SESSION_ID}.jsonl",
        "cwd": "/Users/simon/workspace/qflow",
        "prompt_id": "1fa41a7c-5883-46ce-8703-788c8cd19008",
        "permission_mode": "bypassPermissions",
        "effort": {"level": "high"},
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "ok",
        "background_tasks": [],
        "session_crons": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


class ClaudeNotifyRootTurnTest(unittest.TestCase):
    def test_root_turn_is_allowed(self) -> None:
        self.assertTrue(MODULE.should_notify(_stop_payload()))

    def test_subagent_stop_event_is_suppressed(self) -> None:
        self.assertFalse(
            MODULE.should_notify(_stop_payload(hook_event_name="SubagentStop"))
        )

    def test_stop_inside_subagent_is_suppressed(self) -> None:
        """未来版本若在 subagent 内也触发 Stop，payload 会带 agent_id / agent_type。"""
        for field in ("agent_id", "agent_type"):
            with self.subTest(field=field):
                self.assertFalse(
                    MODULE.should_notify(_stop_payload(**{field: "log-collector"}))
                )

    def test_subagent_transcript_is_suppressed(self) -> None:
        transcript = f"{PROJECT_DIR}/{SESSION_ID}/subagents/agent-a0f0fbd2ddaa874d9.jsonl"
        self.assertFalse(MODULE.should_notify(_stop_payload(transcript_path=transcript)))

    def test_foreign_transcript_is_suppressed(self) -> None:
        """transcript 文件名与 session_id 对不上 = 不是这个会话的主线。"""
        other = f"{PROJECT_DIR}/b8c7837e-74ea-4c89-8632-4bf1c001d1de.jsonl"
        self.assertFalse(MODULE.should_notify(_stop_payload(transcript_path=other)))

    def test_pause_waiting_on_background_work_is_suppressed(self) -> None:
        """起了 async subagent / 后台 Bash 就结束本回合 —— 这不是主任务做完。"""
        cases: list[list[object]] = [
            [{"id": "beel9v18b", "type": "shell", "status": "running"}],
            [{"id": "a9d454af2", "type": "agent", "status": "pending"}],
            [{"id": "x", "status": "completed"}, {"id": "y", "status": "running"}],
            [{"id": "z"}],
            ["形状不认识"],
        ]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                self.assertFalse(MODULE.should_notify(_stop_payload(background_tasks=tasks)))

    def test_settled_background_work_still_notifies(self) -> None:
        tasks = [
            {"id": "x", "type": "shell", "status": "completed"},
            {"id": "y", "type": "agent", "status": "failed"},
        ]
        self.assertTrue(MODULE.should_notify(_stop_payload(background_tasks=tasks)))

    def test_unusable_payload_fails_open(self) -> None:
        """Stop 与 SubagentStop 是两个事件、只注册了 Stop：没有 payload 也不可能是 subagent。

        这里与 Codex 侧的 fail-closed 相反，是刻意的——那边 payload 是唯一判据。
        """
        for name, raw in (("空", ""), ("空白", "   \n"), ("畸形", "{"), ("非对象", "[]")):
            with self.subTest(name=name):
                self.assertTrue(MODULE.should_notify(raw))

    def test_missing_optional_fields_fail_open(self) -> None:
        payload = json.dumps({"stop_hook_active": False})
        self.assertTrue(MODULE.should_notify(payload))

    def test_cli_reads_stdin_and_argv(self) -> None:
        self.assertEqual(MODULE.main(["过滤器", _stop_payload()]), 0)
        self.assertEqual(
            MODULE.main(["过滤器", _stop_payload(hook_event_name="SubagentStop")]), 1
        )

    def test_shell_silently_stops_before_notification_for_subagent(self) -> None:
        result = subprocess.run(
            ["bash", str(NOTIFY_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            input=_stop_payload(hook_event_name="SubagentStop"),
            env={**os.environ},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
