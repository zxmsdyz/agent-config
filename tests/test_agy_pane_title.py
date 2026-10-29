"""Antigravity (agy) 终端标题与 tmux 语义命名 hook 的单元测试。"""

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
SCRIPT = ROOT / ".gemini/bin/agy-pane-title.py"
HOOKS = ROOT / ".gemini/config/hooks.json"
SETTINGS = ROOT / ".gemini/antigravity-cli/settings.json"
RC_SNIPPET = ROOT / "shell/rc.snippet"
SESSION = "b5818028-be8e-415a-9300-3a0c84455545"
OTHER = "147e115d-2230-4827-8886-f17d87aaec7a"
CHILD = "01a0e3d4-5684-7d01-84d0-0363bdb88ae9"


class AgyPaneTitleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agy_home = Path(self.temp.name) / ".gemini/antigravity-cli"
        self.presence = self.agy_home / "presence"
        self.annotations = self.agy_home / "annotations"
        self.titles = self.agy_home / "titles"
        self.presence.mkdir(parents=True)
        self.annotations.mkdir(parents=True)
        self.titles.mkdir(parents=True)
        self.db = self.agy_home / "conversation_summaries.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, "
                "title TEXT, "
                "parent_conversation_id TEXT, "
                "nesting_depth INTEGER)"
            )
            conn.executemany(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                [
                    (SESSION, "Create Agyolo Tmux Integration", "", 0),
                    (OTHER, "已有中文语义标题", "", 0),
                ],
            )
        for cid in (SESSION, OTHER, CHILD):
            (self.presence / f"{cid}.lock").write_text("{}", encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["AGY_HOME"] = str(self.agy_home)
        env.pop("TMUX_PANE", None)
        return env

    def test_title_runner_prioritizes_chinese_title_then_english_then_default(self) -> None:
        # 1. 仅有英文自动标题时，先回退展示英文标题
        res_en = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(
                {
                    "conversation_id": SESSION,
                    "conversation_title": "Create Agyolo Tmux Integration",
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res_en.returncode, 0, res_en.stderr)
        self.assertEqual(res_en.stdout.strip(), "Create Agyolo Tmux Integration")

        # 2. 通过 --set 写入中文语义标题后，优先输出中文标题且同步写回 annotations 和 sqlite
        res_set = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--conversation-id",
                SESSION,
                "--set",
                "配置 agyolo 与 tmux 任务自动命名！",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res_set.returncode, 0, res_set.stderr)
        res_zh = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(
                {
                    "conversation_id": SESSION,
                    "conversation_title": "Create Agyolo Tmux Integration",
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res_zh.stdout.strip(), "配置agyolo与tmux任务自动命名")
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT title FROM conversation_summaries WHERE conversation_id = ?", (SESSION,)
            ).fetchone()
        self.assertEqual(row[0], "配置agyolo与tmux任务自动命名")
        pbtxt = (self.annotations / f"{SESSION}.pbtxt").read_text(encoding="utf-8")
        self.assertIn("配置agyolo与tmux任务自动命名", pbtxt)

        # 3. 用户在 agy 内通过 /rename 更新 annotations 后，优先同步并返回 /rename 后的名字
        ann_file = self.annotations / f"{SESSION}.pbtxt"
        ann_file.write_text('title:"qpilot-ibit-rename"\n', encoding="utf-8")
        res_renamed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(
                {
                    "conversation_id": SESSION,
                    "conversation_title": "qpilot-ibit-rename",
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res_renamed.returncode, 0, res_renamed.stderr)
        self.assertEqual(res_renamed.stdout.strip(), "qpilot-ibit-rename")
        self.assertEqual(
            (self.titles / f"{SESSION}.txt").read_text(encoding="utf-8").strip(),
            "qpilot-ibit-rename",
        )

        # 4. 无会话标题时默认回退为 agy
        res_default = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"conversation_id": CHILD}),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res_default.stdout.strip(), "agy")

    def test_pre_invocation_hook_injects_only_for_unnamed_root_tui(self) -> None:
        # 未设置中文标题的根 TUI 会话（仅有英文自动标题）应注入命名指令
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps(
                {
                    "conversationId": SESSION,
                    "parentConversationId": "",
                    "rootConversationId": SESSION,
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        msg = payload["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("14 至 24 个字符", msg)
        self.assertIn(f"--conversation-id {SESSION} --set", msg)

        # 已有中文标题的会话在每轮新用户输入首步注入「仅切换新任务主题时才更新」指令；同一轮工具调用后续步（相同 lastUserInput 或 invocationNum>1）静默
        res_titled = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps({"conversationId": OTHER, "lastUserInput": "排查sm1线上问题"}),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        titled_msg = json.loads(res_titled.stdout)["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("当前窗口标题为「已有中文语义标题」", titled_msg)
        self.assertIn("仅当用户明确切换到全新任务主题时", titled_msg)

        # 同一轮工具调用后的再次 PreInvocation（lastUserInput 未变化）保持静默
        res_same_input = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps({"conversationId": OTHER, "lastUserInput": "排查sm1线上问题"}),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(json.loads(res_same_input.stdout), {})

        res_titled_midturn = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps({"conversationId": OTHER, "invocationNum": 2, "lastUserInput": "下一轮输入"}),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(json.loads(res_titled_midturn.stdout), {})

        # subagent 会话（有 parentConversationId）保持静默
        res_sub = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps(
                {
                    "conversationId": CHILD,
                    "parentConversationId": SESSION,
                    "rootConversationId": SESSION,
                }
            ),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(json.loads(res_sub.stdout), {})

        # 无 presence lock（如 agy -p 非交互模式）保持静默
        (self.presence / f"{SESSION}.lock").unlink()
        res_headless = subprocess.run(
            [sys.executable, str(SCRIPT), "--hook"],
            input=json.dumps({"conversationId": SESSION}),
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        self.assertEqual(json.loads(res_headless.stdout), {})

    def test_config_and_rc_snippet_wiring(self) -> None:
        rc_text = RC_SNIPPET.read_text(encoding="utf-8")
        self.assertIn('alias agyolo="agy --dangerously-skip-permissions"', rc_text)

        hooks_data = json.loads(HOOKS.read_text(encoding="utf-8"))
        pre_hook = hooks_data["agy-pane-title"]["PreInvocation"][0]
        self.assertEqual(pre_hook["type"], "command")
        self.assertIn("agy-pane-title.py --hook", pre_hook["command"])

        settings_data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual(settings_data["title"]["type"], "command")
        self.assertIn("agy-pane-title.py", settings_data["title"]["command"])

        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("find_native_agy()", installer)
        self.assertIn("/mnt/*) continue ;;", installer)
        self.assertIn('if [ -n "$agy_bin" ]; then', installer)


if __name__ == "__main__":
    unittest.main()
