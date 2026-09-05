"""Codex 终态提醒路由测试。"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".codex/bin/codex-notify-done.sh"
HOOKS = ROOT / ".codex/hooks.json"
CONFIG = ROOT / ".codex/config.toml"


def run_notify(payload: dict, *, legacy: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TMUX_PANE", None)
    env["CODEX_NOTIFY_DRY_RUN"] = "1"
    encoded = json.dumps(payload, ensure_ascii=False)
    command = ["bash", str(SCRIPT)]
    if legacy:
        command.append(encoded)
        stdin = None
    else:
        stdin = encoded
    return subprocess.run(command, input=stdin, text=True, capture_output=True, env=env, check=False)


class NotifyRoutingTest(unittest.TestCase):
    def test_stop_reports_explicit_completion(self) -> None:
        result = run_notify({"hook_event_name": "Stop", "last_assistant_message": "已处理"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            ["complete", "Codex 任务已完成", "任务已完成", "任务已完成", "{}"],
        )

    def test_permission_request_reports_decision(self) -> None:
        result = run_notify({"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            ["decision", "Codex 需要你做决策", "需要你做决策", "需要你做决策", "{}"],
        )

    def test_legacy_boundary_without_assistant_message_is_silent(self) -> None:
        result = run_notify(
            {"type": "agent-turn-complete", "last-assistant-message": ""},
            legacy=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unknown_hook_is_silent_and_returns_valid_json(self) -> None:
        result = run_notify({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "{}\n")


class NotifyConfigTest(unittest.TestCase):
    def test_legacy_notify_is_removed_from_template(self) -> None:
        self.assertNotIn("notify =", CONFIG.read_text(encoding="utf-8"))

    def test_terminal_hooks_are_registered(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]
        self.assertIn("Stop", hooks)
        self.assertIn("PermissionRequest", hooks)
        for event in ("Stop", "PermissionRequest"):
            handler = hooks[event][0]["hooks"][0]
            self.assertTrue(handler["async"])
            self.assertIn("codex-notify-done.sh", handler["command"])


if __name__ == "__main__":
    unittest.main()
