#!/usr/bin/env python3
"""按 hook 会话 ID 只读查询 Codex 原生短会话名。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID


def thread_name(raw_payload: str, db_path: Path) -> str:
    """只有 ID 精确匹配且存在短名称时才返回标题。"""
    try:
        payload = json.loads(raw_payload)
        session_id = payload.get("session_id") or payload.get("thread-id")
        if not isinstance(session_id, str) or str(UUID(session_id)) != session_id.lower():
            return ""
        # mode=ro 避免 hook 误写 Codex 状态库；不读 title（它是完整首条 prompt）。
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.5) as conn:
            row = conn.execute("SELECT name FROM threads WHERE id = ?", (session_id,)).fetchone()
        return row[0].strip() if row and isinstance(row[0], str) else ""
    except (AttributeError, ValueError, OSError, sqlite3.Error, json.JSONDecodeError):
        return ""


def main() -> None:
    """安静失败；未知标题交由通知脚本退回状态文案。"""
    configured = os.environ.get("CODEX_NOTIFY_STATE_DB")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    db_path = Path(configured) if configured else codex_home / "state_5.sqlite"
    db_path = db_path.expanduser().resolve()
    print(thread_name(sys.stdin.read(), db_path))


if __name__ == "__main__":
    main()
