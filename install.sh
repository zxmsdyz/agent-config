#!/usr/bin/env bash
# 将仓库内的配置以软链接安装到当前用户目录。
set -eu

repo_root=$(cd "$(dirname "$0")" && pwd -P)
system=$(uname -s)

case "$system" in
  Darwin) platform="macOS" ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then platform="WSL"
    else platform="Linux"
    fi
    ;;
  *) echo "不支持的系统: $system" >&2; exit 1 ;;
esac

# Superpowers 在 Claude Code 中是官方插件。已安装时不做网络操作，缺失时自动补齐。
if command -v claude >/dev/null 2>&1; then
  plugin_state=$(claude plugin list 2>/dev/null || true)
  if ! printf '%s\n' "$plugin_state" | grep -q 'superpowers@claude-plugins-official'; then
    claude plugin install superpowers@claude-plugins-official --scope user
    plugin_state=$(claude plugin list 2>/dev/null || true)
  fi
  if printf '%s\n' "$plugin_state" | grep -A4 'superpowers@claude-plugins-official' | grep -q 'Status:.*disabled'; then
    claude plugin enable superpowers@claude-plugins-official >/dev/null
  fi
fi

# 备份一律落到专用目录，不能留在原地。
# Why：~/.claude/skills/ 和 ~/.codex/rules/ 会被工具**按目录扫描**，原地留一个
# `<name>.bak.<ts>` 会被当成另一个 skill / 另一份规则加载出来（2026-08-20 实测：
# 备份目录直接以 `cryptostruct-market-data.bak.20260820154018` 出现在技能列表里）。
backup_root="$HOME/.agent-config-backups"

# 把 $HOME 下的相对路径展平成文件名，避免不同目录的同名文件互相覆盖。
backup_path() {
  rel=${1#"$HOME"/}
  printf '%s/%s.bak.%s' "$backup_root" "$(printf '%s' "$rel" | tr '/' '_')" "$(date +%Y%m%d%H%M%S)"
}

link_file() {
  source=$1
  target=$2
  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    mkdir -p "$backup_root"
    backup=$(backup_path "$target")
    mv "$target" "$backup"
    echo "已备份: $target -> $backup"
  fi
  ln -sfn "$source" "$target"
}

link_file "$repo_root/.tmux.conf" "$HOME/.tmux.conf"
link_file "$repo_root/.claude/hooks/notify-done.sh" "$HOME/.claude/hooks/notify-done.sh"
link_file "$repo_root/.claude/hooks/notify.ps1" "$HOME/.claude/hooks/notify.ps1"
link_file "$repo_root/.claude/bin/tmux-cc-peer-label.sh" "$HOME/.claude/bin/tmux-cc-peer-label.sh"
link_file "$repo_root/.claude/bin/claude-wrapper.sh" "$HOME/.claude/bin/claude-wrapper.sh"
link_file "$repo_root/.claude/bin/rc-debug.sh" "$HOME/.claude/bin/rc-debug.sh"
link_file "$repo_root/.claude/bin/rc-disconnect-snapshot.sh" "$HOME/.claude/bin/rc-disconnect-snapshot.sh"
link_file "$repo_root/.claude/bin/statusline-agent-name.sh" "$HOME/.claude/bin/statusline-agent-name.sh"
link_file "$repo_root/.claude/settings.local.json" "$HOME/.claude/settings.local.json"
link_file "$repo_root/.claude/skills/cryptostruct-market-data" "$HOME/.claude/skills/cryptostruct-market-data"
link_file "$repo_root/.codex/hooks.json" "$HOME/.codex/hooks.json"
link_file "$repo_root/.codex/bin/codex-pane-title.py" "$HOME/.codex/bin/codex-pane-title.py"
link_file "$repo_root/.codex/bin/codex-notify-done.sh" "$HOME/.codex/bin/codex-notify-done.sh"
link_file "$repo_root/.codex/rules/default.rules" "$HOME/.codex/rules/default.rules"
chmod +x "$repo_root/install.sh" "$repo_root/.claude/hooks/notify-done.sh" \
  "$repo_root/.claude/bin/tmux-cc-peer-label.sh" "$repo_root/.claude/bin/claude-wrapper.sh" \
  "$repo_root/.claude/bin/rc-debug.sh" "$repo_root/.claude/bin/rc-disconnect-snapshot.sh" \
  "$repo_root/.claude/bin/statusline-agent-name.sh" "$repo_root/.codex/bin/"*

# ---------------------------------------------------------------------------
# rc 片段幂等注入：把 shell/rc.snippet 塞进用户 rc 文件的 marker 包裹区间内。
# 已存在 marker 就整体替换区间内容，不重复追加。
# ---------------------------------------------------------------------------
inject_rc_snippet() {
  rc_file=$1
  snippet="$repo_root/shell/rc.snippet"
  begin_marker="# >>> agent-config >>>"
  end_marker="# <<< agent-config <<<"
  python3 - "$rc_file" "$snippet" "$begin_marker" "$end_marker" <<'PY'
import sys
from pathlib import Path

rc_path, snippet_path, begin, end = sys.argv[1:5]
rc = Path(rc_path)
snippet_body = Path(snippet_path).read_text(encoding="utf-8").rstrip("\n")
block = f"{begin}\n{snippet_body}\n{end}\n"

text = rc.read_text(encoding="utf-8") if rc.exists() else ""
if begin in text and end in text:
    start = text.index(begin)
    stop = text.index(end) + len(end)
    if text[stop:stop + 1] == "\n":
        stop += 1
    text = text[:start] + block + text[stop:]
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += block
rc.write_text(text, encoding="utf-8")

# marker 区间之外若还留着旧的手写引导行（本仓库纳管前用户自己加的），重复 source
# 本身无害（函数/alias 只是重定义），但会让 rc 文件难读、也容易和将来的改动打架。
# 不自动删——误删用户 rc 文件的代价远大于收益——只提示。
outside = text[:text.index(begin)] + text[text.index(end) + len(end):]
if "claude-wrapper.sh" in outside:
    print(f"[agent-config] ⚠️  {rc_path} 的 marker 区间之外还有旧的 claude-wrapper 引导行，", file=sys.stderr)
    print("                建议手动删除，避免重复 source。", file=sys.stderr)
PY
  echo "已同步 rc 片段: $rc_file"
}

if [ "$platform" = "macOS" ]; then
  default_rc="$HOME/.zshrc"
  other_rc="$HOME/.bashrc"
else
  default_rc="$HOME/.bashrc"
  other_rc="$HOME/.zshrc"
fi
[ -f "$default_rc" ] || : > "$default_rc"
inject_rc_snippet "$default_rc"
[ -f "$other_rc" ] && inject_rc_snippet "$other_rc"

openspec_command=""
openspec_args=""
openspec_python="${OPENSPEC_MCP_PYTHON:-}"
if [ -n "$openspec_python" ]; then
  if [ ! -x "$openspec_python" ] || ! "$openspec_python" -c 'import openspec_mcp' >/dev/null 2>&1; then
    echo "OPENSPEC_MCP_PYTHON 不可用或缺少 openspec_mcp 模块: $openspec_python" >&2
    exit 1
  fi
  openspec_command=$openspec_python
  openspec_args="-m openspec_mcp"
elif command -v openspec-mcp >/dev/null 2>&1; then
  openspec_command=$(command -v openspec-mcp)
  openspec_real=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$openspec_command")
  openspec_python_candidate="$(dirname "$openspec_real")/python"
  if [ -x "$openspec_python_candidate" ] && \
     ! "$openspec_python_candidate" -c 'import mcp, importlib.metadata; raise SystemExit(int(importlib.metadata.version("mcp").split(".")[0]) >= 2)' >/dev/null 2>&1; then
    if command -v uv >/dev/null 2>&1; then
      uv tool install --force openspec-mcp --with 'mcp<2'
    else
      echo "openspec-mcp 与已安装的 MCP SDK 不兼容，且缺少 uv 无法修复" >&2
      exit 1
    fi
  fi
elif command -v uv >/dev/null 2>&1; then
  # openspec-mcp 0.2.0 仍使用 MCP SDK 1.x API，必须限制 mcp<2。
  uv tool install openspec-mcp --with 'mcp<2'
  openspec_command=$(command -v openspec-mcp)
else
  for candidate in \
    "$HOME/.local/share/mcp-venvs/openspec/bin/python" \
    "$HOME/.local/share/uv/tools/openspec-mcp/bin/python" \
    "$HOME/.venvs/openspec/bin/python"
  do
    if [ -x "$candidate" ] && "$candidate" -c 'import openspec_mcp' >/dev/null 2>&1; then
      openspec_command=$candidate
      openspec_args="-m openspec_mcp"
      break
    fi
  done
fi

# settings.json 含机器级 MCP 路径，必须生成本地副本，不能直接软链接模板。
# 先完成上面的全部探测和校验，再替换现有配置，避免失败时留下空缺。
settings_target="$HOME/.claude/settings.json"
if [ -e "$settings_target" ] && [ ! -L "$settings_target" ]; then
  mkdir -p "$backup_root"
  settings_backup=$(backup_path "$settings_target")
  mv "$settings_target" "$settings_backup"
  echo "已备份: $settings_target -> $settings_backup"
elif [ -L "$settings_target" ]; then
  rm "$settings_target"
fi

export AGENT_CONFIG_TEMPLATE="$repo_root/.claude/settings.json"
export AGENT_CONFIG_TARGET="$settings_target"
export AGENT_CONFIG_SETTINGS_EXISTING="${settings_backup:-}"
python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

template = Path(os.environ["AGENT_CONFIG_TEMPLATE"])
target = Path(os.environ["AGENT_CONFIG_TARGET"])
settings = json.loads(template.read_text(encoding="utf-8"))
settings.setdefault("mcpServers", {})

# 本机可能有模板里没有的 MCP server（例如指向本地构建产物的路径），这些是机器专属的、
# 不该进模板，但也不能每次 install 就被抹掉。取并集：模板同名条目优先，本机独有的保留。
# 其余顶层键一律以模板为准——hooks / statusLine 这些正是要靠 install.sh 拉回统一状态的。
existing_path = os.environ.get("AGENT_CONFIG_SETTINGS_EXISTING", "")
if existing_path and Path(existing_path).exists():
    try:
        previous = json.loads(Path(existing_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        previous = {}
    for name, conf in (previous.get("mcpServers") or {}).items():
        if name not in settings["mcpServers"]:
            settings["mcpServers"][name] = conf
            print(f"[agent-config] 保留本机独有的 MCP server: {name}", file=sys.stderr)

# hooks / statusLine 的 command 交给 Claude Code 执行，`~` 是否展开取决于它用不用 shell。
# 不赌这个：模板里统一写 `~/`（保持跨机器可移植），生成本地副本时递归替换成真实
# $HOME，两种执行方式都能跑。
def expand_home(node):
    if isinstance(node, dict):
        return {k: expand_home(v) for k, v in node.items()}
    if isinstance(node, list):
        return [expand_home(v) for v in node]
    if isinstance(node, str):
        return node.replace("~/", os.environ["HOME"] + "/")
    return node


settings = expand_home(settings)

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# ---------------------------------------------------------------------------
# ~/.codex/config.toml 含机器级 trust_level / hooks 信任哈希，不能直接软链接模板。
# 只用模板覆盖 model / model_reasoning_effort / notify / [mcp_servers.openspec]
# 四段，保留本机其余原有段。已有文件先备份，python3 < 3.11（无 tomllib）时不做
# 破坏性合并，仅在文件不存在时按模板直接生成，存在时保留原样并提示手动核对。
# ---------------------------------------------------------------------------
codex_config_target="$HOME/.codex/config.toml"
codex_config_existing=""
mkdir -p "$HOME/.codex"
if [ -e "$codex_config_target" ] && [ ! -L "$codex_config_target" ]; then
  mkdir -p "$backup_root"
  codex_config_backup=$(backup_path "$codex_config_target")
  cp "$codex_config_target" "$codex_config_backup"
  echo "已备份: $codex_config_target -> $codex_config_backup"
  codex_config_existing="$codex_config_target"
elif [ -L "$codex_config_target" ]; then
  rm "$codex_config_target"
fi

export AGENT_CONFIG_CODEX_TEMPLATE="$repo_root/.codex/config.toml"
export AGENT_CONFIG_CODEX_TARGET="$codex_config_target"
export AGENT_CONFIG_CODEX_EXISTING="$codex_config_existing"
export AGENT_CONFIG_HOME="$HOME"
export AGENT_CONFIG_OPENSPEC_COMMAND="$openspec_command"
export AGENT_CONFIG_OPENSPEC_ARGS="$openspec_args"
python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

template_path = Path(os.environ["AGENT_CONFIG_CODEX_TEMPLATE"])
target_path = Path(os.environ["AGENT_CONFIG_CODEX_TARGET"])
existing_path_str = os.environ.get("AGENT_CONFIG_CODEX_EXISTING", "")
home = os.environ["AGENT_CONFIG_HOME"]
openspec_command = os.environ.get("AGENT_CONFIG_OPENSPEC_COMMAND", "")
openspec_args_raw = os.environ.get("AGENT_CONFIG_OPENSPEC_ARGS", "")
openspec_args = openspec_args_raw.split() if openspec_args_raw else []

raw_template_text = template_path.read_text(encoding="utf-8").replace("__HOME__", home)
placeholder_fill = openspec_command or "openspec-mcp"
parsable_template_text = raw_template_text.replace("__OPENSPEC_PYTHON__", placeholder_fill)

try:
    import tomllib
except ImportError:
    tomllib = None

if tomllib is None:
    print("[agent-config] 当前 python3 没有内置 tomllib（< 3.11），跳过 ~/.codex/config.toml 的安全合并。", file=sys.stderr)
    if existing_path_str:
        print("[agent-config] 已保留现有 ~/.codex/config.toml，未做修改；请手动核对模板 .codex/config.toml 中的", file=sys.stderr)
        print("                model / model_reasoning_effort / notify / [mcp_servers.openspec] 是否需要同步。", file=sys.stderr)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(parsable_template_text, encoding="utf-8")
        print(f"[agent-config] 已按模板直接生成 {target_path}（未做 tomllib 校验）", file=sys.stderr)
    raise SystemExit(0)

template = tomllib.loads(parsable_template_text)


def dump_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_-]+$", key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(dump_scalar(v) for v in value) + "]"
    raise TypeError(f"不支持序列化的类型: {type(value)!r}")


def dump_table(f, table, path):
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    header = ".".join(dump_key(p) for p in path)
    f.write(f"[{header}]\n")
    for k, v in scalars.items():
        f.write(f"{dump_key(k)} = {dump_scalar(v)}\n")
    f.write("\n")
    for k, v in subtables.items():
        dump_table(f, v, path + [k])


def dump_document(data: dict) -> str:
    import io

    f = io.StringIO()
    top_scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    top_tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for k, v in top_scalars.items():
        f.write(f"{dump_key(k)} = {dump_scalar(v)}\n")
    if top_scalars:
        f.write("\n")
    for k, v in top_tables.items():
        dump_table(f, v, [k])
    text = f.getvalue()
    while text.endswith("\n\n"):
        text = text[:-1]
    if not text.endswith("\n"):
        text += "\n"
    return text


mcp_openspec_table = {
    "command": openspec_command or "openspec-mcp",
    "args": openspec_args,
}

if existing_path_str:
    existing_text = Path(existing_path_str).read_text(encoding="utf-8")
    try:
        existing = tomllib.loads(existing_text)
    except Exception as exc:
        print(f"[agent-config] 现有 ~/.codex/config.toml 解析失败，跳过合并以免破坏它: {exc}", file=sys.stderr)
        raise SystemExit(1)
    existing["model"] = template["model"]
    existing["model_reasoning_effort"] = template["model_reasoning_effort"]
    existing["notify"] = template["notify"]
    mcp_servers = existing.setdefault("mcp_servers", {})
    mcp_servers["openspec"] = mcp_openspec_table
    result = existing
    action = "合并"
else:
    result = dict(template)
    result["mcp_servers"] = {"openspec": mcp_openspec_table}
    action = "生成"

document = dump_document(result)
# 落盘前自检：确保生成的内容能被 tomllib 正常解析回来，避免写出坏文件
tomllib.loads(document)
target_path.parent.mkdir(parents=True, exist_ok=True)
target_path.write_text(document, encoding="utf-8")
print(f"[agent-config] 已{action} {target_path}", file=sys.stderr)
PY

echo "已确保 Claude 官方 superpowers 插件启用"
if [ -n "$openspec_command" ]; then
  echo "已识别 openspec MCP: $openspec_command $openspec_args"
  if command -v claude >/dev/null 2>&1; then
    current_openspec=$(claude mcp get openspec 2>/dev/null || true)
    if ! printf '%s\n' "$current_openspec" | grep -q "Command: $openspec_command"; then
      claude mcp remove openspec -s user >/dev/null 2>&1 || true
      if [ -n "$openspec_args" ]; then
        # shellcheck disable=SC2086
        claude mcp add --scope user openspec -- "$openspec_command" $openspec_args
      else
        claude mcp add --scope user openspec -- "$openspec_command"
      fi
    fi
  fi
else
  echo "未发现 openspec-mcp，且系统没有 uv 可用于自动安装" >&2
  exit 1
fi

echo "已安装 $platform 配置。"
if [ "$platform" = "macOS" ]; then
  echo "通知使用 osascript，语音优先 afplay，失败时使用 say。"
elif [ "$platform" = "WSL" ]; then
  echo "通知继续使用 Windows PowerShell + BurntToast。"
fi
