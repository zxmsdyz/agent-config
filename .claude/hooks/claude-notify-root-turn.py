#!/usr/bin/env python3
"""仅在 Claude Code 根（主）agent 的回合真正结束时允许通知。

Claude Code 2.1.273 实测：subagent 完成走的是 `SubagentStop` 事件，我们只注册
`Stop`，所以"subagent 完成也响"在当前版本并不会发生。本过滤器是**第二道防线**，
覆盖三类仍会误响的情形：

1. 有人把 hook 也挂到 `SubagentStop`，或未来版本把 `Stop` 也在 subagent 内触发
   （官方 payload 里 `agent_id` / `agent_type` 注明 "inside a subagent" 时出现）。
2. `claude --agent <name>` 这类整个会话就是 agent 的运行方式。
3. **主回合只是"暂停等后台活"**：主 agent 起了 async subagent / 后台 Bash 就结束
   本回合，等后台完成再被唤醒继续。官方为此在 Stop payload 里给了
   `background_tasks`，其 schema 原文就是 "Lets hooks distinguish 'session is done'
   from 'session is paused waiting for background work to wake it'"。这正是用户听到
   "子任务完成时也响"的真实来源。

取不到 payload 时**放行**（与 Codex 侧的 fail-closed 相反）：Codex 的 notify 对
根线程和 subagent 用同一个入口、payload 是唯一判据，静默才安全；Claude 这边事件
本身（Stop vs SubagentStop）已经分流，没有 payload 也不可能是 subagent，此时静默
只会白白吃掉全部通知。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 未完成的后台活：状态缺失或不认识时一律按"还在飞"处理，宁可少响一次。
_SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "killed"})


def _is_blank(value: object) -> bool:
    """agent_id / agent_type 只有缺失或空串才算"不在 subagent 里"。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_root_transcript(payload: dict) -> bool:
    """主会话 transcript 固定是 <project>/<session_id>.jsonl，subagent 在 subagents/ 下。

    字段缺失时返回 True——拿不到路径就无从证伪，按放行处理，别让 schema 漂移把通知
    全部吃掉；只要路径在就严格判。
    """
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript.strip():
        return True
    path = Path(transcript)
    if "subagents" in path.parts:
        return False
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return path.stem == session_id
    return True


def _has_inflight_background_work(payload: dict) -> bool:
    """回合结束时还有在飞的后台活 = 会话只是被暂停，不是主任务做完。"""
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, dict):
            return True
        status = task.get("status")
        if not isinstance(status, str) or status.strip().lower() not in _SETTLED_STATUSES:
            return True
    return False


def should_notify(raw_payload: str) -> bool:
    """判定一次 Stop hook 调用是否属于"主 agent 任务真的结束了"。"""
    stripped = raw_payload.strip() if isinstance(raw_payload, str) else ""
    if not stripped:
        return True
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True

    event = payload.get("hook_event_name")
    # 字段缺失时不拦（防未来改名把通知全吃掉），但明写着别的事件就一定拦。
    if isinstance(event, str) and event != "Stop":
        return False
    if not (_is_blank(payload.get("agent_id")) and _is_blank(payload.get("agent_type"))):
        return False
    if not _is_root_transcript(payload):
        return False
    if _has_inflight_background_work(payload):
        return False
    return True


def main(argv: list[str]) -> int:
    """返回零表示允许通知，其他返回值都要求 shell 静默退出。"""
    raw = argv[1] if len(argv) > 1 else sys.stdin.read()
    return 0 if should_notify(raw) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
