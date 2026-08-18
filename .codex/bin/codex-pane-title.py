#!/usr/bin/env python3
"""让 Codex 在首次对话后为当前 tmux 窗口生成简短任务标题。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path


MAX_TITLE_LENGTH = 12
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


def _automatic_rename_enabled(pane: str) -> bool:
    result = _tmux(
        "show-window-options",
        "-v",
        "-t",
        pane,
        "automatic-rename",
    )
    return result.returncode == 0 and result.stdout.strip() == "on"


def _clean_title(raw_title: str) -> str:
    """移除空白、标点和符号，并按 Unicode 字符数限制长度。"""
    cleaned = "".join(
        char
        for char in raw_title.strip()
        if unicodedata.category(char)[0] not in {"C", "P", "S", "Z"}
    )
    return cleaned[:MAX_TITLE_LENGTH]


def _set_title(raw_title: str) -> int:
    pane = _current_pane()
    if pane is None:
        return 0

    title = _clean_title(raw_title)
    if not title:
        print("Codex tmux 标题为空，已跳过命名", file=sys.stderr)
        return 1

    renamed = _tmux("rename-window", "-t", pane, title)
    if renamed.returncode != 0:
        print(renamed.stderr.strip() or "tmux rename-window 失败", file=sys.stderr)
        return renamed.returncode

    # 锁定首次生成的语义标题，避免 Codex 后续更新 pane_title 时覆盖窗口名。
    locked = _tmux("set-window-option", "-t", pane, "automatic-rename", "off")
    if locked.returncode != 0:
        print(locked.stderr.strip() or "关闭 automatic-rename 失败", file=sys.stderr)
        return locked.returncode
    return 0


def _emit_first_prompt_instruction() -> int:
    # Codex hook 输入从 stdin 传入。读取并校验 JSON，避免非 hook 误调用时注入指令。
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if payload.get("hook_event_name") != "UserPromptSubmit":
        return 0

    pane = _current_pane()
    if (
        pane is None
        or not _is_interactive_codex_process()
        or not _automatic_rename_enabled(pane)
    ):
        return 0

    instruction = (
        "这是当前 Codex 会话的首次用户对话。开始分析、回复或调用其他工具前，"
        "必须先概括用户本次要完成的核心任务，生成一个 6 至 12 个字符、约 10 个汉字的中文短标题。"
        "标题要做语义总结，不能直接截取用户原句；不得包含空格、引号、句号、冒号等标点。"
        "随后立即调用 shell 命令：python3 /home/kalami/.codex/bin/codex-pane-title.py --set <短标题>。"
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
    args = parser.parse_args()
    if args.title is not None:
        return _set_title(args.title)
    return _emit_first_prompt_instruction()


if __name__ == "__main__":
    raise SystemExit(main())
