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

# ~/.codex/config.toml 的安全合并需要 tomllib（Python 3.11+）。PATH 里排在最前的 python3
# 未必是机器上最新的（macOS 自带的仍是 3.9.6），只看它就降级成「不合并」，会白白浪费本机
# 其实装了新版 Python 的情况。按版本号从高到低探测，取第一个带 tomllib 的。
pick_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    candidate_bin=$(command -v "$candidate" 2>/dev/null) || continue
    if "$candidate_bin" -c 'import tomllib' >/dev/null 2>&1; then
      printf '%s' "$candidate_bin"
      return 0
    fi
  done
  command -v python3 2>/dev/null || true
}
python_bin=$(pick_python)
if [ -z "$python_bin" ]; then
  echo "未找到 python3，install.sh 无法继续。" >&2
  exit 1
fi
if ! "$python_bin" -c 'import tomllib' >/dev/null 2>&1; then
  echo "[agent-config] ⚠️  $python_bin 无 tomllib（< 3.11），~/.codex/config.toml 会跳过安全合并；" >&2
  echo "                装一个 3.11+ 的 python3 即可恢复，不必改 PATH，本脚本会自动探测。" >&2
fi

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
  openspec_real=$("$python_bin" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$openspec_command")
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

# openspec 探测失败必须在**动任何配置之前**退出。这个校验原本排在脚本末尾，等到
# settings.json 和 config.toml 都已改写才 exit 1，会在机器上留下一个指向不存在可执行文件
# 的 openspec 条目——正是下面这行注释想避免的「失败时留下空缺」。
if [ -z "$openspec_command" ]; then
  echo "未发现 openspec-mcp，且系统没有 uv 可用于自动安装。未改动任何配置。" >&2
  echo "先装 uv（curl -LsSf https://astral.sh/uv/install.sh | sh）后重跑，" >&2
  echo "或用 OPENSPEC_MCP_PYTHON=/path/to/venv/bin/python ./install.sh 指定现成环境。" >&2
  exit 1
fi

# settings.json 模板里 enabledPlugins 标为 true 的插件必须真的装上：只写 enable 不 install
# 会留下「已启用但不存在」的插件。已安装时不做网络操作，缺失时自动补齐。
if command -v claude >/dev/null 2>&1; then
  wanted_plugins=$("$python_bin" - "$repo_root/.claude/settings.json" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for name, enabled in (data.get("enabledPlugins") or {}).items():
    if enabled:
        print(name)
PY
)
  plugin_state=$(claude plugin list 2>/dev/null || true)
  for plugin in $wanted_plugins; do
    if ! printf '%s\n' "$plugin_state" | grep -qF "$plugin"; then
      if claude plugin install "$plugin" --scope user; then
        plugin_state=$(claude plugin list 2>/dev/null || true)
      else
        echo "[agent-config] ⚠️  插件安装失败，已跳过: $plugin" >&2
        continue
      fi
    fi
    if printf '%s\n' "$plugin_state" | grep -F -A4 "$plugin" | grep -q 'Status:.*disabled'; then
      claude plugin enable "$plugin" >/dev/null
    fi
  done
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

# .tmux.conf 末尾的 `run '~/.tmux/plugins/tpm/tpm'` 依赖 tpm。tpm 缺失时 tmux **不报错**，
# 插件（resurrect / continuum）只是静默不加载——比报错更难发现，所以这里主动补齐。
if grep -q "plugins/tpm/tpm" "$repo_root/.tmux.conf" 2>/dev/null; then
  tpm_dir="$HOME/.tmux/plugins/tpm"
  if [ ! -d "$tpm_dir" ]; then
    if command -v git >/dev/null 2>&1; then
      git clone --depth 1 https://github.com/tmux-plugins/tpm "$tpm_dir" >/dev/null 2>&1
      echo "已安装 tpm: $tpm_dir"
    else
      echo "[agent-config] ⚠️  缺少 git，无法安装 tpm，.tmux.conf 里的插件不会生效。" >&2
    fi
  fi
  if [ -x "$tpm_dir/bin/install_plugins" ] && command -v tmux >/dev/null 2>&1; then
    # install_plugins 从**运行中的** tmux server 读 @plugin 声明。没有 server 就先起一个
    # 临时会话（新 server 会自动读取刚软链好的 ~/.tmux.conf），装完销毁；已有 server 则先
    # source-file 一次，否则它读到的还是本次安装之前的旧配置。
    tmux_bootstrap=0
    if tmux list-sessions >/dev/null 2>&1; then
      tmux source-file "$HOME/.tmux.conf" >/dev/null 2>&1 || true
    else
      tmux new-session -d -s agent-config-bootstrap
      tmux_bootstrap=1
    fi
    "$tpm_dir/bin/install_plugins" >/dev/null 2>&1 && echo "已同步 tmux 插件"
    if [ "$tmux_bootstrap" = 1 ]; then
      tmux kill-session -t agent-config-bootstrap >/dev/null 2>&1 || true
    fi
  elif [ -d "$tpm_dir" ]; then
    echo "[agent-config] ⚠️  未安装 tmux，已跳过插件同步；装好 tmux 后重跑 install.sh 即可。" >&2
  fi
fi

# ---------------------------------------------------------------------------
# rc 片段幂等注入：把 shell/rc.snippet 塞进用户 rc 文件的 marker 包裹区间内。
# 已存在 marker 就整体替换区间内容，不重复追加。
# ---------------------------------------------------------------------------
inject_rc_snippet() {
  rc_file=$1
  snippet="$repo_root/shell/rc.snippet"
  begin_marker="# >>> agent-config >>>"
  end_marker="# <<< agent-config <<<"
  "$python_bin" - "$rc_file" "$snippet" "$begin_marker" "$end_marker" <<'PY'
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
"$python_bin" - <<'PY'
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
# 只用模板覆盖 model / model_reasoning_effort / [mcp_servers.openspec] 三段，并移除
# 旧式 notify（提醒已迁到 Stop / PermissionRequest hooks），保留本机其余原有段。
# 已有文件先备份，python3 < 3.11（无 tomllib）时不做
# 破坏性合并，仅在文件不存在时按模板直接生成，存在时保留原样并提示手动核对（脚本开头的
# pick_python 已尽力找过 3.11+，走到这里说明整台机器上都没有）。
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
"$python_bin" - <<'PY'
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
        print("                model / model_reasoning_effort / [mcp_servers.openspec] 是否需要同步，并手动移除旧式 notify。", file=sys.stderr)
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
    existing.pop("notify", None)
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

echo "已确保 settings.json 声明的 Claude 插件已安装并启用"
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

echo "已安装 $platform 配置。"
if [ "$platform" = "macOS" ]; then
  echo "通知使用 osascript，语音优先 afplay，失败时使用 say。"
elif [ "$platform" = "WSL" ]; then
  echo "通知继续使用 Windows PowerShell + BurntToast。"
fi
