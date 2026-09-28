"""Codex 终态提醒路由测试。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".codex/bin/codex-notify-done.sh"
HOOKS = ROOT / ".codex/hooks.json"
CONFIG = ROOT / ".codex/config.toml"


def run_notify(payload: dict, *, legacy: bool = False, state_db: str | None = None,
               pane: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TMUX_PANE", None)
    env["CODEX_NOTIFY_DRY_RUN"] = "1"
    if state_db is not None:
        env["CODEX_NOTIFY_STATE_DB"] = state_db
    if pane is not None:
        env["TMUX_PANE"] = pane
    encoded = json.dumps(payload, ensure_ascii=False)
    command = ["bash", str(SCRIPT)]
    if legacy:
        command.append(encoded)
        stdin = None
    else:
        stdin = encoded
    return subprocess.run(command, input=stdin, text=True, capture_output=True, env=env, check=False)


class NotifyRoutingTest(unittest.TestCase):
    def test_session_id_selects_exact_native_name_without_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state_5.sqlite"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)")
                conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", [
                    ("01a0de93-af8d-7021-a757-5aa719d81a51", "修复test5策略运行时间为空", "长篇首条 prompt"),
                    ("01a0e0e9-4196-7e53-b256-2ef46c6b98ae", "检查交易", "另一个 prompt"),
                ])
            result = run_notify({"hook_event_name": "Stop", "session_id": "01a0de93-af8d-7021-a757-5aa719d81a51"}, state_db=str(db))
            self.assertEqual(result.stdout.splitlines()[2:4], ["修复test5策略运行时间为空", "修复test5策略运行时间为空，任务已完成"])
            other = run_notify({"hook_event_name": "PermissionRequest", "session_id": "01a0e0e9-4196-7e53-b256-2ef46c6b98ae"}, state_db=str(db), pane="%999")
            self.assertEqual(other.stdout.splitlines()[2:4], ["检查交易", "检查交易，需要你做决策"])
            unknown = run_notify({"hook_event_name": "Stop", "session_id": "01a00000-0000-7000-8000-000000000000"}, state_db=str(db), pane="%999")
            self.assertEqual(unknown.stdout.splitlines()[2:4], ["任务已完成", "任务已完成"])

    def test_stop_reports_explicit_completion(self) -> None:
        result = run_notify({"hook_event_name": "Stop", "last_assistant_message": "已处理"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            ["complete", "Codex 任务已完成", "任务已完成", "任务已完成", "{}"],
        )

    def test_codex_home_and_relative_state_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state_5.sqlite"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT)")
                conn.execute("INSERT INTO threads VALUES (?, ?)",
                             ("01a0de93-af8d-7021-a757-5aa719d81a51", "原生会话名"))
            payload = '{"session_id":"01a0de93-af8d-7021-a757-5aa719d81a51"}'
            helper = ROOT / ".codex/bin/codex-thread-name.py"
            for extra in ({"CODEX_HOME": temp}, {"CODEX_NOTIFY_STATE_DB": "state_5.sqlite"}):
                with self.subTest(extra=extra):
                    env = os.environ.copy()
                    env.update(extra)
                    result = subprocess.run(["python3", str(helper)], input=payload, text=True,
                                            capture_output=True, check=False, cwd=temp, env=env)
                    self.assertEqual(result.stdout, "原生会话名\n")

    def test_concurrent_notifications_use_distinct_temporary_audio_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake_bin = Path(temp) / "bin"
            fake_bin.mkdir()
            log = Path(temp) / "paths.log"
            (fake_bin / "mktemp").write_text(
                '#!/bin/sh\npath=$(/usr/bin/mktemp "$@") || exit\nprintf "%s\\n" "$path" >> "$MOCK_LOG"\nprintf "%s\\n" "$path"\n',
                encoding="utf-8",
            )
            (fake_bin / "uname").write_text('#!/bin/sh\nprintf "UnknownOS\\n"\n', encoding="utf-8")
            (fake_bin / "mktemp").chmod(0o755)
            (fake_bin / "uname").chmod(0o755)
            env = os.environ.copy()
            env.pop("CODEX_NOTIFY_DRY_RUN", None)
            env.update({"PATH": f"{fake_bin}:{env['PATH']}", "TMPDIR": temp, "MOCK_LOG": str(log),
                        "CODEX_NOTIFY_EDGE_PYTHON": str(Path(temp) / "missing-python")})
            payload = '{"hook_event_name":"Stop"}'
            processes = [subprocess.Popen(["bash", str(SCRIPT)], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, env=env) for _ in range(2)]
            for process in processes:
                stdout, stderr = process.communicate(payload, timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, "{}\n")
            paths = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(not Path(path).exists() for path in paths))

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
        self.assertNotIn("SessionStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("Stop", hooks)
        self.assertIn("PermissionRequest", hooks)
        for event in ("Stop", "PermissionRequest"):
            handler = hooks[event][0]["hooks"][0]
            self.assertTrue(handler["async"])
            self.assertIn("codex-notify-done.sh", handler["command"])

    def test_native_thread_name_configuration(self) -> None:
        import tomllib

        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(config["tui"]["terminal_title"], ["thread-name"])
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('existing.setdefault("tui", {})["terminal_title"]', installer)
        self.assertIn('link_file "$repo_root/.codex/bin/codex-pane-title.py"', installer)

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_tmux_preserves_chinese_first_character_and_claude_spinner(self) -> None:
        config = (ROOT / ".tmux.conf").read_text(encoding="utf-8")
        format_string = next(line.split("'", 2)[1] for line in config.splitlines()
                             if line.startswith("setw -g automatic-rename-format "))
        socket_name = f"codex-title-test-{uuid4().hex}"
        command = ["tmux", "-L", socket_name]
        subprocess.run(command + ["-f", "/dev/null", "new-session", "-d", "-s", "titlecheck"],
                       check=True, capture_output=True)
        try:
            for raw, expected in (("✳ 从 origin 更新", "从 origin 更新"),
                                  ("修 复任务", "修 复任务"),
                                  ("⠂ 修复任务", "修复任务")):
                with self.subTest(raw=raw):
                    subprocess.run(command + ["select-pane", "-t", "titlecheck:0.0", "-T", raw],
                                   check=True, capture_output=True)
                    result = subprocess.run(command + ["display-message", "-p", "-t", "titlecheck:0.0",
                                                       "-F", format_string], check=True, capture_output=True,
                                            text=True)
                    self.assertEqual(result.stdout.strip(), expected)
        finally:
            subprocess.run(command + ["kill-server"], check=False, capture_output=True)

    def test_installer_merge_preserves_desktop_notify_and_other_tui_settings(self) -> None:
        import tomllib

        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        python_code = installer.rsplit('"$python_bin" - <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "config.toml"
            base = '[tui]\nnotifications = true\n\n[projects."/tmp/local"]\ntrust_level = "trusted"\n'
            for notify, retained in (
                ('["python3", "/tmp/computer-use/turn-ended.py"]', True),
                ('["bash", "/tmp/.codex/bin/codex-notify-done.sh"]', False),
            ):
                with self.subTest(notify=notify):
                    target.write_text(f"notify = {notify}\n" + base, encoding="utf-8")
                    env = os.environ.copy()
                    env.update({"AGENT_CONFIG_CODEX_TEMPLATE": str(CONFIG),
                                "AGENT_CONFIG_CODEX_TARGET": str(target),
                                "AGENT_CONFIG_CODEX_EXISTING": str(target),
                                "AGENT_CONFIG_HOME": temp,
                                "AGENT_CONFIG_OPENSPEC_COMMAND": "openspec-mcp",
                                "AGENT_CONFIG_OPENSPEC_ARGS": ""})
                    result = subprocess.run(["python3", "-c", python_code], env=env,
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    merged = tomllib.loads(target.read_text(encoding="utf-8"))
                    self.assertEqual("notify" in merged, retained)
                    self.assertEqual(merged["tui"]["terminal_title"], ["thread-name"])
                    self.assertTrue(merged["tui"]["notifications"])
                    self.assertEqual(merged["projects"]["/tmp/local"]["trust_level"], "trusted")


if __name__ == "__main__":
    unittest.main()
