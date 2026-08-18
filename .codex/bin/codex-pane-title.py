#!/usr/bin/env python3
"""根据 Codex 会话的首条用户提示设置 tmux 窗口名。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


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
