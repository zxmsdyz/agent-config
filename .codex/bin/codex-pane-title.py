#!/usr/bin/env python3
"""仅在交互式 Codex 会话缺少原生名称时请求语义命名。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from uuid import UUID


def _valid_session_id(value: object) -> str | None:
    """只接受完整 UUID，避免将任意路径拼进能力目录。"""
    if not isinstance(value, str):
        return None
    try:
        return value if str(UUID(value)) == value.lower() else None
    except ValueError:
        return None


def _is_tui_thread(session_id: str, capability_dir: Path) -> bool:
    """能力文件由 Codex TUI 创建；daemon 继承的 TMUX_PANE 不参与判定。"""
    try:
        return stat.S_ISREG((capability_dir / session_id).lstat().st_mode)
    except OSError:
        return False


def _native_name(db_path: Path, session_id: str) -> tuple[bool, str]:
    """只读查询精确会话；未知 schema 或 subagent 均安静跳过。"""
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.5) as conn:
            row = conn.execute(
                "SELECT name, agent_path FROM threads WHERE id = ?", (session_id,)
            ).fetchone()
    except (OSError, ValueError, sqlite3.Error):
        return False, ""
    if row is None or row[1] not in (None, "", "/root"):
        return False, ""
    return True, row[0].strip() if isinstance(row[0], str) else ""


def _hook_context(payload: object, codex_home: Path, db_path: Path | None = None,
                  capability_dir: Path | None = None) -> dict[str, object] | None:
    """已有原生短名优先；只有尚无名称的 TUI 根会话才注入指令。"""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return None
    session_id = _valid_session_id(payload.get("session_id"))
    if session_id is None:
        return None
    capabilities = capability_dir or codex_home / "tui-thread-reference-capabilities"
    if not _is_tui_thread(session_id, capabilities):
        return None
    state_db = db_path or codex_home / "state_5.sqlite"
    known, name = _native_name(state_db, session_id)
    if not known or name:
        return None

    instruction = (
        "当前 Codex TUI 会话尚无原生短名。请概括本次用户任务，生成一个 14 至 24 个字符、"
        "约 20 个汉字的中文标题；标题必须是语义总结，不能直接截取 prompt。若上下文已提供，"
        "优先保留 venue 或交易所、策略类型或下单通道、任务动作；不得臆造未知信息，"
        "不得包含空格、引号、句号、冒号等标点。开始分析、回复或调用其他业务工具前，"
        "先调用 tools.mcp__codex_tui__set_thread_title，参数为 "
        f'{{"threadId":"{session_id}","title":"<短标题>"}}。'
        "显式 threadId 只会命名这条会话；若已观察到该会话有原生名称则跳过。"
        "若工具未直接显示，可在 ALL_TOOLS 中查找 set_thread_title 并通过 functions.exec 调用；"
        "确实不可用时跳过命名，不执行 shell 或 tmux 改名。"
        "这是静默的界面初始化动作，不要向用户解释或展示命名过程。"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": instruction,
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    result = _hook_context(payload, codex_home)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
