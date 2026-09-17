"""Claude Code 完成通知的主 agent 过滤测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
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


class _skip_log:
    """把静音日志改道到临时文件，避免测试污染 ~/.claude/notify-skip.log。"""

    def __enter__(self) -> Path:
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(MODULE._SKIP_LOG_ENV)
        self.path = Path(self._dir.name) / "notify-skip.log"
        self.path.touch()
        os.environ[MODULE._SKIP_LOG_ENV] = str(self.path)
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self._prev is None:
            os.environ.pop(MODULE._SKIP_LOG_ENV, None)
        else:
            os.environ[MODULE._SKIP_LOG_ENV] = self._prev
        self._dir.cleanup()


def _run_notify(payload: str) -> list[str]:
    """DRY_RUN 跑一次 notify-done.sh，返回它打印的行（静默时为空）。"""
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            ["bash", str(NOTIFY_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            input=payload,
            env={
                **os.environ,
                "CLAUDE_NOTIFY_DRY_RUN": "1",
                MODULE._SKIP_LOG_ENV: str(Path(tmp) / "skip.log"),
            },
        )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


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
        with _skip_log() as log:
            self.assertEqual(MODULE.main(["过滤器", _stop_payload()]), 0)
            self.assertEqual(
                MODULE.main(["过滤器", _stop_payload(hook_event_name="SubagentStop")]), 1
            )
            self.assertFalse(log.read_text(encoding="utf-8").startswith("\n"))

    def test_cli_accepts_mode_flag(self) -> None:
        payload = _stop_payload(hook_event_name="Notification", message="需要权限")
        with _skip_log():
            self.assertEqual(MODULE.main(["过滤器", "--mode", "attention", payload]), 0)
            self.assertEqual(MODULE.main(["过滤器", "--mode", "stop", payload]), 1)

    def test_skip_is_logged_with_reason_and_tasks(self) -> None:
        """静音本身不可观测——判据误伤时现场必须留下一行，否则只能靠猜。"""
        tasks = [{"id": "beel9v18b", "type": "shell", "status": "running"}]
        with _skip_log() as log:
            MODULE.main(["过滤器", _stop_payload(background_tasks=tasks)])
            written = log.read_text(encoding="utf-8")
        self.assertIn("stop/background-work-inflight", written)
        self.assertIn("beel9v18b", written)
        self.assertIn(SESSION_ID[:8], written)

    def test_skip_log_is_capped(self) -> None:
        with _skip_log() as log:
            for _ in range(MODULE._SKIP_LOG_MAX_LINES + 40):
                MODULE.main(["过滤器", _stop_payload(hook_event_name="SubagentStop")])
            lines = log.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), MODULE._SKIP_LOG_MAX_LINES)

    def test_allowed_turn_writes_nothing(self) -> None:
        with _skip_log() as log:
            MODULE.main(["过滤器", _stop_payload()])
            self.assertEqual(log.read_text(encoding="utf-8"), "")

    def test_attention_mode_allows_waiting_on_user_events(self) -> None:
        for event in ("Notification", "PermissionRequest"):
            with self.subTest(event=event):
                self.assertTrue(
                    MODULE.should_notify(
                        _stop_payload(hook_event_name=event), mode="attention"
                    )
                )

    def test_attention_mode_ignores_background_work(self) -> None:
        """要权限时后台常常正有活在飞——套 stop 的判据就等于最该响时不响。"""
        payload = _stop_payload(
            hook_event_name="Notification",
            background_tasks=[{"id": "x", "status": "running"}],
        )
        self.assertTrue(MODULE.should_notify(payload, mode="attention"))

    def test_attention_mode_still_blocks_subagents(self) -> None:
        for field in ("agent_id", "agent_type"):
            with self.subTest(field=field):
                payload = _stop_payload(hook_event_name="Notification", **{field: "x"})
                self.assertFalse(MODULE.should_notify(payload, mode="attention"))

    def test_stop_mode_rejects_attention_events(self) -> None:
        payload = _stop_payload(hook_event_name="Notification")
        self.assertFalse(MODULE.should_notify(payload))

    def test_shell_dispatches_event_to_notification_kind(self) -> None:
        """notify-done.sh 的事件分发：DRY_RUN 下打印 mode/title/line/speech 四行。"""
        cases = {
            "Stop": ("stop", "任务已完成"),
            "Notification": ("attention", "需要你确认"),
            "PermissionRequest": ("attention", "需要你确认"),
        }
        for event, (mode, status) in cases.items():
            with self.subTest(event=event):
                out = _run_notify(_stop_payload(hook_event_name=event))
                self.assertEqual(out[0], mode)
                self.assertIn(status, out[1])

    def test_idle_prompt_is_suppressed(self) -> None:
        """idle_prompt 在每次回复完约 60s 后触发——放行就等于每个任务响两次。"""
        payload = _stop_payload(
            hook_event_name="Notification", notification_type="idle_prompt"
        )
        self.assertFalse(MODULE.should_notify(payload, mode="attention"))

    def test_noisy_notification_types_are_suppressed(self) -> None:
        for kind in (
            "auth_success",
            "elicitation_complete",
            "elicitation_response",
            "agent_completed",
            "quota_auto_resume_started",
        ):
            with self.subTest(kind=kind):
                payload = _stop_payload(
                    hook_event_name="Notification", notification_type=kind
                )
                self.assertFalse(MODULE.should_notify(payload, mode="attention"))

    def test_blocking_notification_types_are_allowed(self) -> None:
        for kind in ("permission_prompt", "elicitation_dialog", "agent_needs_input"):
            with self.subTest(kind=kind):
                payload = _stop_payload(
                    hook_event_name="Notification", notification_type=kind
                )
                self.assertTrue(MODULE.should_notify(payload, mode="attention"))

    def test_unknown_notification_type_fails_open(self) -> None:
        """拒绝列表而非允许列表：漏掉要你拍板的时刻比多响一次贵得多。"""
        payload = _stop_payload(
            hook_event_name="Notification", notification_type="some_future_type"
        )
        self.assertTrue(MODULE.should_notify(payload, mode="attention"))

    def test_shell_speaks_what_is_being_asked(self) -> None:
        """没有 message 字段时，播报词得靠 notification_type / tool_name 撑起信息量。"""
        cases = [
            ({"notification_type": "permission_prompt"}, "需要你授权"),
            ({"notification_type": "agent_needs_input"}, "后台任务需要你输入"),
            ({"tool_name": "AskUserQuestion"}, "有问题要你选"),
            ({"tool_name": "ExitPlanMode"}, "计划待你批准"),
        ]
        for extra, expected in cases:
            event = "PreToolUse" if "tool_name" in extra else "Notification"
            with self.subTest(expected=expected):
                out = _run_notify(_stop_payload(hook_event_name=event, **extra))
                self.assertIn(expected, out[1])
                self.assertTrue(out[3].endswith(expected))

    def test_shell_banner_prefers_notification_message(self) -> None:
        """英文 message 只进横幅；朗读保持中文短句，否则中文神经音念得含混。"""
        message = "Claude needs your permission to use Bash"
        out = _run_notify(
            _stop_payload(hook_event_name="Notification", message=message)
        )
        self.assertEqual(out[2], message)
        self.assertTrue(out[3].endswith("需要你确认"))
        self.assertNotIn(message, out[3])

    def test_shell_ignores_unrelated_events(self) -> None:
        for event in ("PostToolUse", "UserPromptSubmit", "SessionEnd"):
            with self.subTest(event=event):
                self.assertEqual(_run_notify(_stop_payload(hook_event_name=event)), [])

    def test_pretooluse_on_ordinary_tool_is_suppressed(self) -> None:
        """matcher 漏写时 PreToolUse 会对每个工具触发——脚本自己也要挡住。"""
        for tool in ("Bash", "Edit", "Read"):
            with self.subTest(tool=tool):
                payload = _stop_payload(hook_event_name="PreToolUse", tool_name=tool)
                self.assertFalse(MODULE.should_notify(payload, mode="attention"))
                self.assertEqual(_run_notify(payload), [])

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
