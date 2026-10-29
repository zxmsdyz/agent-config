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


def _find_interactive_agy_pid(start_pid: int | None = None) -> int | None:
    """沿进程祖先查找交互式 agy TUI 进程 PID，过滤 -p/--print 非交互调用。"""
    pid = os.getppid() if start_pid is None else start_pid
    visited: set[int] = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        info = _process_info(pid)
        if info is None:
            return None
        argv, parent_pid = info
        if argv and _is_agy_executable(argv[0]):
            return pid if not _agy_is_noninteractive(argv) else None
        pid = parent_pid
    return None


def _is_interactive_agy_process(start_pid: int | None = None) -> bool:
    """沿进程祖先查找 agy，只放行非 -p/--print 的交互式 TUI。"""
    return _find_interactive_agy_pid(start_pid) is not None


def _pane_map_file(agy_home: Path, pane_id: str) -> Path:
    return agy_home / "titles" / f"pane-{pane_id}.json"


def _write_pane_mapping(
    agy_home: Path,
    conversation_id: str,
    title: str,
    *,
    pane: str | None = None,
    agy_pid: int | None = None,
) -> None:
    """将 tmux pane 与当前 agy 会话 ID、短 ID、标题绑定落盘，供 pane 边框脚本快速读取。"""
    target_pane = (pane if pane is not None else os.environ.get("TMUX_PANE", "")).strip()
    if not target_pane or not target_pane.startswith("%"):
        return
    if not _is_tui_conversation(agy_home, conversation_id):
        return
    resolved_pid = agy_pid if agy_pid is not None else _find_interactive_agy_pid()
    if resolved_pid is None:
        return
    titles_dir = agy_home / "titles"
    payload = {
        "pane_id": target_pane,
        "pid": resolved_pid,
        "conversation_id": conversation_id,
        "short_id": conversation_id[:8],
        "title": title,
    }
    try:
        titles_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = titles_dir / f".pane-{target_pane}.tmp"
        target_path = _pane_map_file(agy_home, target_pane)
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(target_path)
    except OSError:
        pass


def _sync_renamed_annotation_title(agy_home: Path, conversation_id: str, saved: str) -> str:
    """若用户在 agy 内通过 /rename 修改了标题（annotations 比 titles 新且内容不同），同步回 titles。"""
    ann_title = _read_annotation_title(agy_home, conversation_id)
    if not ann_title or ann_title == saved:
        return saved
    ann_path = agy_home / "annotations" / f"{conversation_id}.pbtxt"
    title_path = _title_file(agy_home, conversation_id)
    try:
        if ann_path.stat().st_mtime + 1e-6 < title_path.stat().st_mtime:
            return saved
    except OSError:
        return saved
    try:
        title_path.parent.mkdir(parents=True, exist_ok=True)
        title_path.write_text(ann_title + "\n", encoding="utf-8")
    except OSError:
        pass
    return ann_title


def _resolve_conversation_title(
    agy_home: Path,
    conversation_id: str,
    conv_title: str = "",
) -> str:
    """解析当前会话标题（优先采用已保存标题或 /rename 更新后的标题）。"""
    saved = _read_saved_title(agy_home, conversation_id)
    if saved:
        return _sync_renamed_annotation_title(agy_home, conversation_id, saved)

    ann_title = _read_annotation_title(agy_home, conversation_id)
    is_root, db_title = _read_db_summary(agy_home, conversation_id)

    if conv_title and _has_cjk(conv_title):
        return conv_title
    if ann_title and _has_cjk(ann_title):
        return ann_title
    if is_root and db_title and _has_cjk(db_title):
        return db_title

    if conv_title:
        return conv_title
    if ann_title:
        return ann_title
    if is_root and db_title:
        return db_title
    return DEFAULT_TITLE


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

    raw_conv_title = payload.get("conversation_title") or payload.get("conversationTitle")
    conv_title = raw_conv_title.strip() if isinstance(raw_conv_title, str) else ""
    title = _resolve_conversation_title(agy_home, conversation_id, conv_title)
    _write_pane_mapping(agy_home, conversation_id, title)
    return title


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
        existing_title = _sync_renamed_annotation_title(agy_home, conversation_id, existing_title)
        _write_pane_mapping(agy_home, conversation_id, existing_title)
        inv_num = payload.get("invocationNum")
        if (isinstance(inv_num, int) and inv_num > 1) or not is_new_turn:
            return {}
        instruction = (
            f"当前窗口标题为「{existing_title}」（会话 ID：{conversation_id}，短 ID：{conversation_id[:8]}）。"
            "若用户本轮仍在继续同一任务（追问、确认、补充或推进），"
            "请保持当前标题，不要调用命名脚本；仅当用户明确切换到全新任务主题时，才先调用 run_command 执行："
            f"python3 {quoted_script} --conversation-id {conversation_id} --set '<14至24字中文新标题>'。"
            f"若需与其他 agent 会话对话，可先执行 python3 {quoted_script} --peers 按短 ID、重命名窗口名或标题反查完整 conversation_id 后调用 send_message。"
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
            f"若需与其他 agent 会话对话，可执行 python3 {quoted_script} --peers 按短 ID、重命名窗口名或标题反查完整 conversation_id 后调用 send_message。"
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
    _write_pane_mapping(agy_home, conversation_id, title)

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


def _pid_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _find_descendant_agy_pid(root_pid: int) -> int | None:
    """在 tmux pane 的根 shell 进程树下查找交互式 agy TUI 进程 PID。"""
    if root_pid <= 1:
        return None
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "ppid=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    children: dict[int, list[tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, parts[2]))

    queue = [root_pid]
    visited: set[int] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for child_pid, cmd in children.get(current, []):
            try:
                argv = shlex.split(cmd)
            except ValueError:
                argv = cmd.split()
            if argv and _is_agy_executable(argv[0]) and not _agy_is_noninteractive(argv):
                return child_pid
            queue.append(child_pid)
    return None


def _find_conversation_id_for_pid(agy_pid: int, agy_home: Path) -> str | None:
    """通过进程打开的 presence/<uuid>.lock 文件描述符定位当前会话 ID。"""
    presence_prefix = str((agy_home / "presence").resolve()) + os.sep

    fd_dir = Path("/proc") / str(agy_pid) / "fd"
    if fd_dir.is_dir():
        try:
            for entry in fd_dir.iterdir():
                try:
                    target = str(entry.resolve())
                except OSError:
                    continue
                if target.startswith(presence_prefix) and target.endswith(".lock"):
                    stem = Path(target).stem
                    cid = _valid_conversation_id(stem)
                    if cid:
                        return cid
        except OSError:
            pass

    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(agy_pid), "-Fn"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path_str = line[1:].strip()
        if path_str.endswith(".lock") and "/presence/" in path_str:
            cid = _valid_conversation_id(Path(path_str).stem)
            if cid and _is_tui_conversation(agy_home, cid):
                return cid
    return None


def _read_pane_mapping(agy_home: Path, pane_id: str) -> dict[str, object] | None:
    path = _pane_map_file(agy_home, pane_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _query_tmux_panes(target_pane: str | None = None) -> list[dict[str, str]]:
    fmt = (
        "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t"
        "#{automatic-rename}\t#{window_name}\t#{pane_title}\t#{pane_current_path}"
    )
    cmd = ["tmux", "list-panes", "-a", "-F", fmt]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    rows: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        pane_id = parts[2].strip()
        if target_pane and pane_id != target_pane:
            continue
        rows.append(
            {
                "session_name": parts[0].strip(),
                "window_id": parts[1].strip(),
                "pane_id": pane_id,
                "pane_pid": parts[3].strip(),
                "automatic_rename": parts[4].strip(),
                "window_name": parts[5].strip(),
                "pane_title": parts[6].strip(),
                "cwd": parts[7].strip(),
            }
        )
    return rows


def _inspect_pane_peer(meta: dict[str, str], agy_home: Path) -> dict[str, object] | None:
    pane_id = meta["pane_id"]
    try:
        pane_pid = int(meta["pane_pid"])
    except (KeyError, ValueError):
        return None

    cached = _read_pane_mapping(agy_home, pane_id)
    agy_pid: int | None = None
    conversation_id: str | None = None
    if cached:
        raw_pid = cached.get("pid")
        raw_cid = _valid_conversation_id(cached.get("conversation_id"))
        if isinstance(raw_pid, int) and _pid_is_alive(raw_pid) and raw_cid and _is_tui_conversation(agy_home, raw_cid):
            agy_pid = raw_pid
            conversation_id = raw_cid

    if agy_pid is None:
        agy_pid = _find_descendant_agy_pid(pane_pid)
    if agy_pid is None:
        _pane_map_file(agy_home, pane_id).unlink(missing_ok=True)
        return None

    if conversation_id is None:
        conversation_id = _find_conversation_id_for_pid(agy_pid, agy_home)
    if conversation_id is None:
        return None

    title = _resolve_conversation_title(agy_home, conversation_id)
    _write_pane_mapping(agy_home, conversation_id, title, pane=pane_id, agy_pid=agy_pid)

    manual_window_name = meta["window_name"] if meta.get("automatic_rename") in {"0", "off"} and meta.get("window_name") else None
    display_name = manual_window_name or (
        title if title and title != DEFAULT_TITLE else (meta.get("pane_title") or meta.get("window_name") or DEFAULT_TITLE)
    )
    return {
        "pane_id": pane_id,
        "session_name": meta.get("session_name", ""),
        "window_id": meta.get("window_id", ""),
        "window_name": meta.get("window_name", ""),
        "manual_window_name": manual_window_name,
        "title": title,
        "display_name": display_name,
        "short_id": conversation_id[:8],
        "conversation_id": conversation_id,
        "pid": agy_pid,
        "cwd": meta.get("cwd", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--set", dest="title")
    parser.add_argument("--conversation-id")
    parser.add_argument("--pane-label")
    parser.add_argument("--peers", action="store_true")
    args, _ = parser.parse_known_args()

    agy_home = _agy_home()

    if args.pane_label:
        rows = _query_tmux_panes(args.pane_label.strip())
        if not rows:
            return 0
        peer = _inspect_pane_peer(rows[0], agy_home)
        if peer:
            sys.stdout.write(f"⇄ {peer['display_name']} {peer['short_id']}")
        return 0

    if args.peers:
        peers = []
        for row in _query_tmux_panes():
            peer = _inspect_pane_peer(row, agy_home)
            if peer:
                peers.append(peer)
        print(json.dumps(peers, ensure_ascii=False, indent=2))
        return 0

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
