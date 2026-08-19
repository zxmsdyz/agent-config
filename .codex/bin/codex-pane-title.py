#!/usr/bin/env python3
"""让 Codex 在首次对话后为当前 tmux 窗口生成简短任务标题。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path


MAX_TITLE_LENGTH = 24
DEFAULT_TITLE = "Codex"
_PANE_SESSION_OPTION = "@codex_title_session_id"
_PANE_STATUS_OPTION = "@codex_title_status"
_PANE_TITLE_OPTION = "@codex_title"
_NON_INTERACTIVE_SUBCOMMANDS = {"e", "exec", "review"}
_OPTIONS_WITH_VALUE = {
    "-a",
    "--add-dir",
    "--ask-for-approval",
    "-C",
    "--cd",
    "-c",
    "--config",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}


def _codex_subcommand(argv: list[str]) -> str | None:
    """从 Codex argv 中解析子命令；普通交互提示不视为子命令。"""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return None
        if arg in _OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(
            arg.startswith(f"{option}=")
            for option in _OPTIONS_WITH_VALUE
            if option.startswith("--")
        ):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg if arg in _NON_INTERACTIVE_SUBCOMMANDS else None
    return None


def _is_codex_executable(executable: str) -> bool:
    """识别 Node 启动器或原生 Codex 二进制。"""
    return Path(executable).name in {"codex", "codex.exe"}


def _is_interactive_codex_process(start_pid: int | None = None) -> bool:
    """沿进程祖先查找 Codex，并只放行无子命令的交互 TUI。"""
    pid = os.getppid() if start_pid is None else start_pid
    visited: set[int] = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        proc_dir = Path("/proc") / str(pid)
        try:
            argv = [
                part.decode("utf-8", errors="replace")
                for part in proc_dir.joinpath("cmdline").read_bytes().split(b"\0")
                if part
            ]
            status_lines = proc_dir.joinpath("status").read_text(encoding="utf-8").splitlines()
            parent_pid = int(
                next(line for line in status_lines if line.startswith("PPid:")).split()[1]
            )
        except (OSError, ValueError, IndexError, StopIteration):
            return False

        if argv and _is_codex_executable(argv[0]):
            return _codex_subcommand(argv) not in _NON_INTERACTIVE_SUBCOMMANDS
        pid = parent_pid
    return False


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _current_pane() -> str | None:
    pane = os.environ.get("TMUX_PANE", "").strip()
    return pane or None


def _pane_option(pane: str, name: str) -> str | None:
    """读取 pane 级用户 option；未设置时返回 ``None``。"""
    result = _tmux("show-options", "-p", "-v", "-t", pane, name)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _set_pane_option(pane: str, name: str, value: str) -> bool:
    result = _tmux("set-option", "-p", "-t", pane, name, value)
    return result.returncode == 0


def _lock_window_title(pane: str) -> bool:
    """禁止 Codex 的 spinner/cwd OSC 标题覆盖语义窗口名。"""
    result = _tmux("set-window-option", "-t", pane, "automatic-rename", "off")
    return result.returncode == 0


def _ensure_session(pane: str, session_id: str) -> str | None:
    """发现新 chat 时立即清旧标题，并进入等待语义标题的 pending 状态。"""
    current_session = _pane_option(pane, _PANE_SESSION_OPTION)
    if current_session == session_id:
        status = _pane_option(pane, _PANE_STATUS_OPTION)
        if status in {"pending", "titled"}:
            return status
        # 防止上次 tmux option 只写入一半后永久失去重试机会。
        if _set_pane_option(pane, _PANE_STATUS_OPTION, "pending"):
            return "pending"
        return None

    renamed = _tmux("rename-window", "-t", pane, DEFAULT_TITLE)
    if renamed.returncode != 0 or not _lock_window_title(pane):
        return None
    _set_pane_option(pane, _PANE_TITLE_OPTION, DEFAULT_TITLE)
    if not _set_pane_option(pane, _PANE_STATUS_OPTION, "pending"):
        return None
    # session id 最后提交；前面的任一步失败，下次 hook 仍会完整重试。
    if not _set_pane_option(pane, _PANE_SESSION_OPTION, session_id):
        return None
    return "pending"


def _clean_title(raw_title: str) -> str:
    """移除空白、标点和符号，并按 Unicode 字符数限制长度。"""
    cleaned = "".join(
        char
        for char in raw_title.strip()
        if unicodedata.category(char)[0] not in {"C", "P", "S", "Z"}
    )
    return cleaned[:MAX_TITLE_LENGTH]


def _set_title(raw_title: str, session_id: str | None = None) -> int:
    pane = _current_pane()
    if pane is None:
        return 0

    title = _clean_title(raw_title)
    if not title:
        print("Codex tmux 标题为空，已跳过命名", file=sys.stderr)
        return 1

    # `/clear` 可能在上一轮模型生成标题期间发生。旧轮次不得覆盖新 chat 的标题。
    if session_id is not None:
        current_session = _pane_option(pane, _PANE_SESSION_OPTION)
        if current_session != session_id:
            return 0

    renamed = _tmux("rename-window", "-t", pane, title)
    if renamed.returncode != 0:
        print(renamed.stderr.strip() or "tmux rename-window 失败", file=sys.stderr)
        return renamed.returncode

    if not _lock_window_title(pane):
        print("关闭 automatic-rename 失败", file=sys.stderr)
        return 1
    if session_id is not None:
        if not _set_pane_option(pane, _PANE_TITLE_OPTION, title):
            return 1
        if not _set_pane_option(pane, _PANE_STATUS_OPTION, "titled"):
            return 1
    return 0


def _handle_hook() -> int:
    # Codex hook 输入从 stdin 传入。读取并校验 JSON，避免非 hook 误调用时注入指令。
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    event_name = payload.get("hook_event_name")
    if event_name not in {"SessionStart", "UserPromptSubmit"}:
        return 0

    pane = _current_pane()
    session_id = payload.get("session_id")
    if pane is None or not isinstance(session_id, str) or not session_id.strip():
        return 0
    if not _is_interactive_codex_process():
        return 0

    status = _ensure_session(pane, session_id)
    if event_name == "SessionStart" or status != "pending":
        return 0

    quoted_session_id = shlex.quote(session_id)
    instruction = (
        "这是当前 Codex 会话的首次用户对话。开始分析、回复或调用其他工具前，"
        "必须先概括用户本次要完成的核心任务，生成一个 14 至 24 个字符、约 20 个汉字的中文标题。"
        "标题要做语义总结，不能直接截取用户原句；若上下文已提供，必须优先保留 venue 或交易所、"
        "策略类型或下单通道、任务动作等关键信息，不得臆造未知信息；"
        "不得包含空格、引号、句号、冒号等标点。"
        "随后立即调用 shell 命令："
        "python3 /home/kalami/.codex/bin/codex-pane-title.py "
        f"--session-id {quoted_session_id} --set '<短标题>'。"
        "这是静默的界面初始化动作，不要向用户解释或展示标题生成过程。"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": instruction,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", dest="title")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    if args.title is not None:
        return _set_title(args.title, args.session_id)
    return _handle_hook()


if __name__ == "__main__":
    raise SystemExit(main())
