"""Codex 完成通知的根线程过滤测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / ".codex/bin/codex-notify-root-turn.py"
NOTIFY_SCRIPT = Path(__file__).parents[1] / ".codex/bin/codex-notify-done.sh"
SPEC = importlib.util.spec_from_file_location("codex_notify_root_turn", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


THREAD_ID = "01a080a0-3834-72f1-9838-2302dc6cc98e"


def _notify_payload(**overrides: object) -> str:
    """生成与 Codex notify wire schema 一致的最小事件。"""
    payload: dict[str, object] = {
        "type": "agent-turn-complete",
        "thread-id": THREAD_ID,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _rollout_metadata(**overrides: object) -> dict[str, object]:
    """生成根用户线程的最小 rollout 首行。"""
    metadata: dict[str, object] = {
        "thread_source": "user",
        "parent_thread_id": None,
        "agent_path": None,
    }
    metadata.update(overrides)
    return {"payload": metadata}


class CodexNotifyRootTurnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sessions_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_rollout(self, metadata: object, name: str | None = None) -> Path:
        """写入仅供测试使用的 rollout 首行。"""
        rollout = self.sessions_root / (
            name or f"rollout-2026-09-08T18-46-18-{THREAD_ID}.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(metadata, str):
            rollout.write_text(metadata + "\n", encoding="utf-8")
        else:
            rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        return rollout

    def test_root_user_turn_is_allowed(self) -> None:
        self._write_rollout(_rollout_metadata())
        self.assertTrue(MODULE.should_notify(_notify_payload(), self.sessions_root))

    def test_subagent_turn_is_suppressed(self) -> None:
        self._write_rollout(
            _rollout_metadata(
                thread_source="subagent",
                parent_thread_id="01a07f7a-3de8-7202-a52c-42f01a040c55",
                agent_path="/root/network_access_audit",
            )
        )
        self.assertFalse(MODULE.should_notify(_notify_payload(), self.sessions_root))

    def test_cli_reads_injected_sessions_root(self) -> None:
        self._write_rollout(_rollout_metadata())
        with patch.dict(
            os.environ,
            {"CODEX_NOTIFY_SESSIONS_ROOT": str(self.sessions_root)},
            clear=False,
        ):
            self.assertEqual(MODULE.main(["过滤器", _notify_payload()]), 0)

    def test_shell_silently_stops_before_notification_for_subagent(self) -> None:
        self._write_rollout(
            _rollout_metadata(
                thread_source="subagent",
                parent_thread_id="01a07f7a-3de8-7202-a52c-42f01a040c55",
                agent_path="/root/network_access_audit",
            )
        )
        environment = {
            **os.environ,
            "CODEX_NOTIFY_SESSIONS_ROOT": str(self.sessions_root),
        }
        result = subprocess.run(
            ["bash", str(NOTIFY_SCRIPT), _notify_payload()],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_negative_cases_fail_closed(self) -> None:
        cases: list[tuple[str, str, list[object]]] = [
            ("畸形通知", "{", []),
            ("缺少线程", _notify_payload(**{"thread-id": None}), []),
            ("非法 UUID", _notify_payload(**{"thread-id": "not-a-uuid"}), []),
            ("找不到 rollout", _notify_payload(), []),
            (
                "多个 rollout",
                _notify_payload(),
                [
                    _rollout_metadata(),
                    _rollout_metadata(),
                ],
            ),
            ("坏元数据", _notify_payload(), ["{"]),
            (
                "未知来源",
                _notify_payload(),
                [_rollout_metadata(thread_source="system")],
            ),
        ]
        for name, payload, rollouts in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    sessions_root = Path(directory)
                    for index, metadata in enumerate(rollouts):
                        rollout = sessions_root / str(index) / (
                            f"rollout-{index}-{THREAD_ID}.jsonl"
                        )
                        rollout.parent.mkdir(parents=True, exist_ok=True)
                        text = metadata if isinstance(metadata, str) else json.dumps(metadata)
                        rollout.write_text(text + "\n", encoding="utf-8")
                    self.assertFalse(MODULE.should_notify(payload, sessions_root))


if __name__ == "__main__":
    unittest.main()
