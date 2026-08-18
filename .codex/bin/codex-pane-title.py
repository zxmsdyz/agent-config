#!/usr/bin/env python3
"""根据 Codex 会话的首条用户提示设置 tmux 窗口名。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


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
        if any(arg.startswith(f"{option}=") for option in _OPTIONS_WITH_VALUE if option.startswith("--")):
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
            parent_pid = int(next(line for line in status_lines if line.startswith("PPid:")).split()[1])
        except (OSError, ValueError, IndexError, StopIteration):
            return False

        if argv and _is_codex_executable(argv[0]):
            return _codex_subcommand(argv) not in _NON_INTERACTIVE_SUBCOMMANDS
        pid = parent_pid
    return False


def _short_title(prompt: str) -> str:
    """把首条提示压成适合 tmux 状态栏的单行标题。"""
    text = re.sub(r"\s+", " ", prompt).strip()
    text = re.sub(r"^[#>*`\-\d.、)（(\s]+", "", text)
    if not text:
        return "Codex"
    limit = 32
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        print("{}")
        return 0

    pane = os.environ.get("TMUX_PANE", "")
    prompt = event.get("prompt", "")
    session_id = event.get("session_id", "")
    if not pane or not isinstance(prompt, str) or not prompt.strip():
        print("{}")
        return 0

    # Claude Code 会在自己的 pane 里启动 codex exec 做审核；子进程会继承 TMUX_PANE。
    # 这里只允许交互式 Codex TUI 改名，避免后台 exec/review 覆盖 Claude 窗口名。
    if not _is_interactive_codex_process():
        print("{}")
        return 0

    # 每个会话在每个 pane 只取首条提示，避免“继续”一类追问覆盖任务名。
    marker_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{session_id}-{pane}")
    marker = Path("/tmp") / f"codex-pane-title-{marker_name}"
    if marker.exists():
        print("{}")
        return 0

    title = _short_title(prompt)
    subprocess.run(
        ["tmux", "rename-window", "-t", pane, title],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        marker.write_text(title, encoding="utf-8")
    except OSError:
        pass

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
