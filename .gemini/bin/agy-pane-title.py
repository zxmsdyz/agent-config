#!/usr/bin/env python3
"""Antigravity CLI (agy) 的 tmux 终端标题渲染器与首次对话中文语义命名 hook。

支持三种运行模式：
1. 无参数（默认）：作为 ~/.gemini/antigravity-cli/settings.json 中 `title.command` 被 agy TUI 调用，
   从 stdin 读取 StatusLineData JSON，向 stdout 输出当前会话应显示的窗口标题（由 agy 通过 OSC 2
   写入 pane_title，再由 tmux automatic-rename 同步为窗口名）。
2. `--hook`：作为 ~/.gemini/config/hooks.json 的 `PreInvocation` hook 运行，仅在交互式根会话且尚无
   中文语义标题时注入一次性命名指令。
3. `--conversation-id <uuid> --set <title>`：清洗并持久化当前会话的中文语义标题，并在交互式
   agy tmux pane 中立即刷新 pane_title。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path
from uuid import UUID


MAX_TITLE_LENGTH = 24
DEFAULT_TITLE = "agy"
_NON_INTERACTIVE_FLAGS = {"-p", "--print", "--prompt"}
_OPTIONS_WITH_VALUE = {
    "--add-dir",
    "--agent",
    "--conversation",
    "--effort",
    "--input-format",
    "--json-schema",
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print-timeout",
    "--project",
}


def _agy_home() -> Path:
    configured = os.environ.get("AGY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".gemini" / "antigravity-cli").expanduser().resolve()


def _valid_conversation_id(value: object) -> str | None:
    """只接受完整 UUID，防止将任意路径拼进标题或能力目录。"""
    if not isinstance(value, str):
        return None
    try:
        return value.lower() if str(UUID(value)) == value.lower() else None
    except ValueError:
        return None


def _has_cjk(text: str) -> bool:
    """判断标题是否已包含中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _clean_title(raw_title: str) -> str:
    """移除空白、标点、符号与控制字符，并按 Unicode 字符数限制长度。"""
    cleaned = "".join(
        char
        for char in raw_title.strip()
        if unicodedata.category(char)[0] not in {"C", "P", "S", "Z"}
    )
    return cleaned[:MAX_TITLE_LENGTH]


def _title_file(agy_home: Path, conversation_id: str) -> Path:
    return agy_home / "titles" / f"{conversation_id}.txt"


def _last_input_file(agy_home: Path, conversation_id: str) -> Path:
    return agy_home / "titles" / f"{conversation_id}.last_input"


def _consume_new_user_input(agy_home: Path, conversation_id: str, last_user_input: object) -> bool:
    """判断 PreInvocation 是否处于新一轮用户输入的首步（避免同一轮工具调用后重复注入）。"""
    if not isinstance(last_user_input, str) or not last_user_input.strip():
        return True
    normalized = last_user_input.strip()
    path = _last_input_file(agy_home, conversation_id)
    try:
        previous = path.read_text(encoding="utf-8")
    except OSError:
        previous = None
    if previous == normalized:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")
    except OSError:
        pass
    return True


def _read_saved_title(agy_home: Path, conversation_id: str) -> str:
    path = _title_file(agy_home, conversation_id)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_annotation_title(agy_home: Path, conversation_id: str) -> str:
    path = agy_home / "annotations" / f"{conversation_id}.pbtxt"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^\s*title\s*:\s*"((?:\\.|[^"\\])*)"', content, flags=re.MULTILINE)
    if not match:
        return ""
    raw = match.group(1)
    return raw.replace('\\"', '"').replace("\\\\", "\\").strip()


def _read_db_summary(agy_home: Path, conversation_id: str) -> tuple[bool, str]:
    """只读查询 conversation_summaries.db；返回 (是否允许命名的根会话, 标题)。"""
    db_path = agy_home / "conversation_summaries.db"
    if not db_path.exists():
        return True, ""
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.5) as conn:
            row = conn.execute(
                "SELECT title, parent_conversation_id, nesting_depth "
                "FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
    except (OSError, ValueError, sqlite3.Error):
        return True, ""
    if row is None:
        return True, ""
    title = row[0].strip() if isinstance(row[0], str) else ""
    parent_id = row[1].strip() if isinstance(row[1], str) else ""
    nesting = int(row[2] or 0)
    if parent_id or nesting > 0:
        return False, ""
    return True, title


def _is_tui_conversation(agy_home: Path, conversation_id: str) -> bool:
    """presence/<id>.lock 由交互式 agy TUI 的 store.Manager 创建。"""
    lock_path = agy_home / "presence" / f"{conversation_id}.lock"
    try:
        return stat.S_ISREG(lock_path.lstat().st_mode)
    except OSError:
        return False


def _is_agy_executable(executable: str) -> bool:
    return Path(executable).name in {"agy", "agy.exe"}


def _agy_is_noninteractive(argv: list[str]) -> bool:
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return False
        if arg in _NON_INTERACTIVE_FLAGS:
            return True
        if any(arg.startswith(f"{flag}=") for flag in _NON_INTERACTIVE_FLAGS if flag.startswith("--")):
            return True
        if arg in _OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(arg.startswith(f"{opt}=") for opt in _OPTIONS_WITH_VALUE):
            index += 1
            continue
        index += 1
    return False


def _process_info(pid: int) -> tuple[list[str], int] | None:
    proc_dir = Path("/proc") / str(pid)
    if proc_dir.is_dir():
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
            return argv, parent_pid
        except (OSError, ValueError, IndexError, StopIteration):
            return None

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(None, 1)
    if len(parts) != 2:
        return None
    try:
        return shlex.split(parts[1]), int(parts[0])
    except ValueError:
        return None


def _is_interactive_agy_process(start_pid: int | None = None) -> bool:
    """沿进程祖先查找 agy，只放行非 -p/--print 的交互式 TUI。"""
    pid = os.getppid() if start_pid is None else start_pid
    visited: set[int] = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        info = _process_info(pid)
        if info is None:
            return False
        argv, parent_pid = info
        if argv and _is_agy_executable(argv[0]):
            return not _agy_is_noninteractive(argv)
        pid = parent_pid
    return False


def _render_title_from_statusline(raw_stdin: str, agy_home: Path) -> str:
    """供 agy `title.command` 调用：输出单行窗口标题。"""
    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except json.JSONDecodeError:
        return DEFAULT_TITLE
    if not isinstance(payload, dict):
        return DEFAULT_TITLE

    conversation_id = _valid_conversation_id(
        payload.get("conversation_id") or payload.get("conversationId")
    )
    if conversation_id is None:
        return DEFAULT_TITLE

    saved = _read_saved_title(agy_home, conversation_id)
    if saved:
        return saved

    conv_title = payload.get("conversation_title") or payload.get("conversationTitle")
    if isinstance(conv_title, str) and conv_title.strip() and _has_cjk(conv_title):
        return conv_title.strip()

    ann_title = _read_annotation_title(agy_home, conversation_id)
    if ann_title and _has_cjk(ann_title):
        return ann_title

    is_root, db_title = _read_db_summary(agy_home, conversation_id)
    if is_root and db_title and _has_cjk(db_title):
        return db_title

    if isinstance(conv_title, str) and conv_title.strip():
        return conv_title.strip()
    if ann_title:
        return ann_title
    if is_root and db_title:
        return db_title
    return DEFAULT_TITLE


def _hook_response(
    payload: object,
    agy_home: Path,
    *,
    script_path: Path | None = None,
) -> dict[str, object]:
    """供 PreInvocation hook 调用：仅在交互式根会话缺少中文标题时注入命名指令。"""
    if not isinstance(payload, dict):
        return {}

    conversation_id = _valid_conversation_id(
        payload.get("conversationId") or payload.get("conversation_id")
    )
    if conversation_id is None:
        return {}

    parent_id = payload.get("parentConversationId") or payload.get("parent_conversation_id")
    if isinstance(parent_id, str) and parent_id.strip():
        return {}

    root_id = payload.get("rootConversationId") or payload.get("root_conversation_id")
    if isinstance(root_id, str) and root_id.strip() and root_id.strip().lower() != conversation_id:
        return {}

    if not _is_tui_conversation(agy_home, conversation_id):
        return {}

    is_root, db_title = _read_db_summary(agy_home, conversation_id)
    if not is_root:
        return {}

    existing_title = _read_saved_title(agy_home, conversation_id)
    if not existing_title:
        ann_title = _read_annotation_title(agy_home, conversation_id)
        if _has_cjk(ann_title):
            existing_title = ann_title
        elif _has_cjk(db_title):
            existing_title = db_title

    target_script = script_path or (Path.home() / ".gemini" / "bin" / "agy-pane-title.py")
    quoted_script = shlex.quote(str(target_script))
    is_new_turn = _consume_new_user_input(
        agy_home, conversation_id, payload.get("lastUserInput")
    )
    if existing_title:
        inv_num = payload.get("invocationNum")
        if (isinstance(inv_num, int) and inv_num > 1) or not is_new_turn:
            return {}
        instruction = (
            f"当前窗口标题为「{existing_title}」。若用户本轮仍在继续同一任务（追问、确认、补充或推进），"
            "请保持当前标题，不要调用命名脚本；仅当用户明确切换到全新任务主题时，才先调用 run_command 执行："
            f"python3 {quoted_script} --conversation-id {conversation_id} --set '<14至24字中文新标题>'。"
            "切勿向用户提及命名过程。"
        )
    else:
        instruction = (
            "当前 Antigravity CLI 会话尚无中文短名。请概括本次用户任务，生成一个 14 至 24 个字符、"
            "约 20 个汉字的中文标题；标题必须是语义总结，不能直接截取 prompt。若上下文已提供，"
            "优先保留 venue 或交易所、策略类型或下单通道、任务动作；不得臆造未知信息，"
            "不得包含空格、引号、句号、冒号等标点。开始分析、回复或调用其他业务工具前，"
            f"先调用 run_command 执行：python3 {quoted_script} "
            f"--conversation-id {conversation_id} --set '<短标题>'。"
            "这是静默的界面初始化动作，不要向用户解释或展示命名过程。"
        )
    return {
        "injectSteps": [
            {
                "ephemeralMessage": instruction,
            }
        ]
    }


def _update_annotation_and_db(agy_home: Path, conversation_id: str, title: str) -> None:
    """尽力同步 annotations 与 conversation_summaries.db，使 /resume 列表也显示中文短名。"""
    ann_dir = agy_home / "annotations"
    ann_path = ann_dir / f"{conversation_id}.pbtxt"
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'title:"{escaped}"'
    try:
        ann_dir.mkdir(parents=True, exist_ok=True)
        if ann_path.exists():
            existing = ann_path.read_text(encoding="utf-8")
            if re.search(r'^\s*title\s*:', existing, flags=re.MULTILINE):
                updated = re.sub(
                    r'^\s*title\s*:\s*"(?:\\.|[^"\\])*"',
                    new_line,
                    existing,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                updated = existing.rstrip("\n") + "\n" + new_line + "\n"
        else:
            updated = new_line + "\n"
        ann_path.write_text(updated, encoding="utf-8")
    except OSError:
        pass

    db_path = agy_home / "conversation_summaries.db"
    if db_path.exists():
        try:
            with sqlite3.connect(db_path, timeout=0.5) as conn:
                conn.execute(
                    "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                    (title, conversation_id),
                )
        except sqlite3.Error:
            pass


def _set_title(raw_title: str, raw_conversation_id: str | None, agy_home: Path) -> int:
    conversation_id = _valid_conversation_id(raw_conversation_id)
    if conversation_id is None:
        print("无效的 conversation_id，已跳过命名", file=sys.stderr)
        return 1

    title = _clean_title(raw_title)
    if not title:
        print("agy tmux 标题为空，已跳过命名", file=sys.stderr)
        return 1

    titles_dir = agy_home / "titles"
    try:
        titles_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = titles_dir / f".{conversation_id}.tmp"
        target_path = _title_file(agy_home, conversation_id)
        tmp_path.write_text(title + "\n", encoding="utf-8")
        tmp_path.replace(target_path)
    except OSError as exc:
        print(f"写入 agy 标题失败: {exc}", file=sys.stderr)
        return 1

    _update_annotation_and_db(agy_home, conversation_id, title)

    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane and (_is_tui_conversation(agy_home, conversation_id) or _is_interactive_agy_process()):
        try:
            subprocess.run(
                ["tmux", "select-pane", "-t", pane, "-T", title],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--set", dest="title")
    parser.add_argument("--conversation-id")
    args, _ = parser.parse_known_args()

    agy_home = _agy_home()

    if args.title is not None:
        return _set_title(args.title, args.conversation_id, agy_home)

    if args.hook:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, OSError):
            print("{}")
            return 0
        print(json.dumps(_hook_response(payload, agy_home), ensure_ascii=False))
        return 0

    raw_stdin = sys.stdin.read()
    print(_render_title_from_statusline(raw_stdin, agy_home))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
