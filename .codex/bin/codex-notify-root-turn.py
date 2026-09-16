#!/usr/bin/env python3
"""仅在 Codex 根用户线程完成时允许通知。"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from uuid import UUID


_THREAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _valid_thread_id(thread_id: object) -> str | None:
    """校验 notify 的线程 ID，避免它进入文件匹配模式。"""
    if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
        return None
    try:
        UUID(thread_id)
    except ValueError:
        return None
    return thread_id


def _notify_thread_id(raw_payload: str) -> str | None:
    """从合法的完成事件中提取线程 ID。"""
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
        return None
    return _valid_thread_id(payload.get("thread-id"))


def _find_rollout(sessions_root: Path, thread_id: str) -> Path | None:
    """唯一匹配到线程 rollout 时返回文件；歧义时拒绝通知。"""
    candidates: list[Path] = []
    try:
        for candidate in sessions_root.rglob(f"rollout-*-{thread_id}.jsonl"):
            if candidate.is_file():
                candidates.append(candidate)
                if len(candidates) == 2:
                    return None
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _is_root_user_rollout(rollout: Path) -> bool:
    """只接受元数据明确标记为无父级的用户根线程。"""
    try:
        with rollout.open(encoding="utf-8") as file:
            event = json.loads(file.readline())
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(event, dict):
        return False
    metadata = event.get("payload")
    if not isinstance(metadata, dict) or metadata.get("thread_source") != "user":
        return False
    return (
        metadata.get("parent_thread_id") in (None, "")
        and metadata.get("agent_path") in (None, "")
    )


def should_notify(raw_payload: str, sessions_root: Path) -> bool:
    """判定一次 Codex notify 是否属于可通知的根用户线程。"""
    thread_id = _notify_thread_id(raw_payload)
    if thread_id is None:
        return False
    rollout = _find_rollout(sessions_root, thread_id)
    return rollout is not None and _is_root_user_rollout(rollout)


def _sessions_root() -> Path:
    """允许测试注入会话根目录，生产环境默认使用 Codex 会话目录。"""
    configured = os.environ.get("CODEX_NOTIFY_SESSIONS_ROOT")
    return Path(configured) if configured else Path.home() / ".codex" / "sessions"


def main(argv: list[str]) -> int:
    """返回零表示允许通知，其他返回值都要求 shell 静默退出。"""
    if len(argv) != 2:
        return 1
    return 0 if should_notify(argv[1], _sessions_root()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
