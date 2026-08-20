#!/usr/bin/env bash
# QFlow statusline: 常驻显示本 session 的跨 agent 通信名（ListAgents / SendMessage 用的 name）
# 名字真源 = ~/.claude/sessions/<pid>.json 的 .name 字段（Claude Code 自己写的注册表）
# 依赖：仅 bash + coreutils（无 jq）

payload=$(cat)

field() { printf '%s' "$payload" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -1; }

sid=$(field session_id)
cwd=$(field current_dir); [ -n "$cwd" ] || cwd=$(field cwd)
model=$(printf '%s' "$payload" | sed -n 's/.*"display_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)

# 用 session_id 在注册表里反查 agent name
name=""
if [ -n "$sid" ]; then
  reg=$(grep -l "\"sessionId\":\"$sid\"" "$HOME"/.claude/sessions/*.json 2>/dev/null | head -1)
  [ -n "$reg" ] && name=$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$reg" | head -1)
fi
[ -n "$name" ] || name="?"

branch=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
  branch=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null)
fi

out="\033[1;36m@${name}\033[0m"
[ -n "$branch" ] && out="${out}  \033[2m${branch}\033[0m"
[ -n "$model" ] && out="${out}  \033[2m${model}\033[0m"
printf "%b" "$out"
