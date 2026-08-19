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

link_file() {
  source=$1
  target=$2
  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    backup="$target.bak.$(date +%Y%m%d%H%M%S)"
    mv "$target" "$backup"
    echo "已备份: $target -> $backup"
  fi
  ln -sfn "$source" "$target"
}

link_file "$repo_root/.tmux.conf" "$HOME/.tmux.conf"
link_file "$repo_root/.claude/hooks/notify-done.sh" "$HOME/.claude/hooks/notify-done.sh"
link_file "$repo_root/.claude/hooks/notify.ps1" "$HOME/.claude/hooks/notify.ps1"
link_file "$repo_root/.claude/bin/tmux-cc-peer-label.sh" "$HOME/.claude/bin/tmux-cc-peer-label.sh"
link_file "$repo_root/.codex/hooks.json" "$HOME/.codex/hooks.json"
link_file "$repo_root/.codex/bin/codex-pane-title.py" "$HOME/.codex/bin/codex-pane-title.py"
link_file "$repo_root/.codex/bin/codex-notify-done.sh" "$HOME/.codex/bin/codex-notify-done.sh"
chmod +x "$repo_root/install.sh" "$repo_root/.claude/hooks/notify-done.sh" \
  "$repo_root/.claude/bin/tmux-cc-peer-label.sh" "$repo_root/.codex/bin/"*

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
  settings_backup="$settings_target.bak.$(date +%Y%m%d%H%M%S)"
  mv "$settings_target" "$settings_backup"
  echo "已备份: $settings_target -> $settings_backup"
elif [ -L "$settings_target" ]; then
  rm "$settings_target"
fi

export AGENT_CONFIG_TEMPLATE="$repo_root/.claude/settings.json"
export AGENT_CONFIG_TARGET="$settings_target"
python3 - <<'PY'
import json
import os
from pathlib import Path

template = Path(os.environ["AGENT_CONFIG_TEMPLATE"])
target = Path(os.environ["AGENT_CONFIG_TARGET"])
settings = json.loads(template.read_text(encoding="utf-8"))
servers = settings.setdefault("mcpServers", {})

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
