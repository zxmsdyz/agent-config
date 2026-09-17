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

本过滤器有两种 mode，对应两类"值得打扰用户"的时刻：

- `stop`（默认）= 主 agent 回合真正结束，判据如上四条。
- `attention` = 主 agent **卡住等人**（要权限 / 要决策 / 空闲）。此时 `background_tasks`
  里有活在飞是**常态**而非噪声（起了后台活又撞上要授权），故该判据必须跳过；
  `hook_event_name` 也不再要求是 `Stop`。挡 subagent 的两条判据两种 mode 都保留——
  subagent 要权限时打扰用户同样没意义。

⚠ 静音是**不可观测**的：判据一旦误伤，现象就是"通知莫名其妙没了"，而现场什么都
不留。所以 `main()` 每静音一次就往 `~/.claude/notify-skip.log` 记一行（含判据名与
`background_tasks` 原文），供事后复盘到底是哪条拦的。日志只在 CLI 入口写，
`should_notify()` 保持纯函数——测试与复用都不该有副作用。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 未完成的后台活：状态缺失或不认识时一律按"还在飞"处理，宁可少响一次。
_SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "killed"})

# 静音原因日志。默认落在 ~/.claude/（机器本地、不进仓库）；测试用环境变量改道。
_SKIP_LOG_ENV = "CLAUDE_NOTIFY_SKIP_LOG"
_SKIP_LOG_DEFAULT = "~/.claude/notify-skip.log"
_SKIP_LOG_MAX_LINES = 500


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


# `PreToolUse` 只有在 settings 的 matcher 把它限定到 AskUserQuestion / ExitPlanMode
# 时才该挂这个 hook——否则每个工具调用都会响。之所以不只靠 `Notification`：本机开着
# bypassPermissions + skipDangerousModePermissionPrompt，权限弹窗几乎不出现，真正
# 卡住等人的是那两个工具本身。
ATTENTION_EVENTS = frozenset({"Notification", "PermissionRequest", "PreToolUse"})

# `Notification` 是个大杂烩事件，用 notification_type 分流。官方列出的类型里只有一部分
# 是"卡住等人"，其余会把通知变成噪声——尤其 `idle_prompt`：它在**每次**回复结束约 60s
# 后触发，注册上去等于每个任务响两次（Stop 一次、60s 后再一次）。
#
# 用**拒绝列表**而非允许列表：本功能的全部价值就是别漏掉要你拍板的时刻，未来版本新增
# 的类型宁可多响一次，也不该被默默吃掉。
_IGNORED_NOTIFICATION_TYPES = frozenset(
    {
        "idle_prompt",  # 回复完 60s 没输入——Stop 已经响过了
        "auth_success",
        "elicitation_complete",
        "elicitation_response",
        "agent_completed",  # 后台 agent 完成会把主回合唤醒，届时走 Stop
    }
)
_IGNORED_NOTIFICATION_PREFIXES = ("quota_auto_resume",)

# `PreToolUse` 本身对每个工具调用都触发，靠 settings 的 matcher 限定到这两个。matcher
# 写错或漏写就会变成"每调一次工具响一次"，所以这里再挡一道：判据便宜，噪声很贵。
_BLOCKING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})


def skip_reason(raw_payload: str, mode: str = "stop") -> str | None:
    """返回静音原因（判据名），允许通知时返回 None。

    原因串直接进日志，改名会影响历史日志可比性。mode 语义见模块文档。
    """
    attention = mode == "attention"
    stripped = raw_payload.strip() if isinstance(raw_payload, str) else ""
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    event = payload.get("hook_event_name")
    # 字段缺失时不拦（防未来改名把通知全吃掉），但明写着别的事件就一定拦。
    if isinstance(event, str):
        allowed = ATTENTION_EVENTS if attention else {"Stop"}
        if event not in allowed:
            return f"unexpected-event:{event}"
    if not _is_blank(payload.get("agent_id")):
        return "inside-subagent:agent_id"
    if not _is_blank(payload.get("agent_type")):
        return "inside-subagent:agent_type"
    if attention and event == "PreToolUse":
        tool = payload.get("tool_name")
        if isinstance(tool, str) and tool.strip() and tool.strip() not in _BLOCKING_TOOLS:
            return f"tool-not-blocking:{tool.strip()}"
    if attention and isinstance(event, str) and event == "Notification":
        kind = payload.get("notification_type")
        if isinstance(kind, str) and kind.strip():
            kind = kind.strip()
            if kind in _IGNORED_NOTIFICATION_TYPES or kind.startswith(
                _IGNORED_NOTIFICATION_PREFIXES
            ):
                return f"notification-type-ignored:{kind}"
    if not _is_root_transcript(payload):
        return "not-root-transcript"
    # 等人确认时后台有活在飞是常态，不是"任务没做完"，该判据只对 stop mode 成立。
    if not attention and _has_inflight_background_work(payload):
        return "background-work-inflight"
    return None


def should_notify(raw_payload: str, mode: str = "stop") -> bool:
    """判定一次 hook 调用是否值得打扰用户。"""
    return skip_reason(raw_payload, mode) is None


def _log_skip(reason: str, raw_payload: str) -> None:
    """记录一次静音。任何异常都吞掉——诊断日志绝不能反过来弄坏通知链路。"""
    try:
        target = Path(os.path.expanduser(os.environ.get(_SKIP_LOG_ENV) or _SKIP_LOG_DEFAULT))
        detail = ""
        try:
            payload = json.loads(raw_payload)
            if isinstance(payload, dict):
                # background_tasks 是最可能误伤的判据，原文照记，便于看清卡在哪个 status。
                detail = " tasks=" + json.dumps(
                    payload.get("background_tasks"), ensure_ascii=False
                )
                session = payload.get("session_id")
                if isinstance(session, str) and session:
                    detail = f" session={session[:8]}" + detail
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        line = f"{datetime.now().isoformat(timespec='seconds')} {reason}{detail}\n"

        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)

        # 无人清理的日志迟早变成几十 MB 的垃圾：超限就只留后半截。
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > _SKIP_LOG_MAX_LINES:
            target.write_text("".join(lines[-_SKIP_LOG_MAX_LINES:]), encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    """返回零表示允许通知，其他返回值都要求 shell 静默退出。

    用法：`... [--mode stop|attention] [payload]`，省略 payload 时从 stdin 读。
    """
    args = list(argv[1:])
    mode = "stop"
    if len(args) >= 2 and args[0] == "--mode":
        mode = args[1]
        args = args[2:]
    raw = args[0] if args else sys.stdin.read()
    reason = skip_reason(raw, mode)
    if reason is None:
        return 0
    _log_skip(f"{mode}/{reason}", raw)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
