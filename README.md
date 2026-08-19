# agent-config

个人 Claude Code、Codex CLI + tmux 配置备份，支持 WSL、macOS 和原生 Linux。

## 文件映射

仓库内路径镜像 `$HOME`，安装时拷回（或 symlink）到对应位置：

| 仓库内 | 部署到 | 作用 |
|---|---|---|
| `.tmux.conf` | `~/.tmux.conf` | tmux 配置；开启 `automatic-rename`，窗口名自动跟随 Claude Code 当前任务摘要（取 `pane_title` 剥掉 spinner），新 tab 不再是 `bash`、无需手动改名；pane 边框右端显示该 pane 的 CC peer 地址（见下一行脚本） |
| `.claude/bin/tmux-cc-peer-label.sh` | `~/.claude/bin/tmux-cc-peer-label.sh` | 被 `pane-border-format` 的 `#()` 调用，查 `~/.claude/sessions/<pid>.json` 反查该 pane 的 Claude Code peer 地址：`⇄ 名字 sid` = 已注册 peer messaging（`SendMessage({to:"名字"})` 可直达）；`· 名字 sid (no-msg)` = 有会话但没开 messaging socket，`ListAgents` 看不到它 |
| `.claude/settings.json` | `~/.claude/settings.json` | Claude Code 设置；`Stop` hook 调用下面的脚本弹系统通知 |
| `.claude/hooks/notify-done.sh` | `~/.claude/hooks/notify-done.sh` | Stop hook 入口：自动判断 macOS / WSL / Linux，选择对应的通知和语音实现 |
| `.claude/hooks/notify.ps1` | 随 `notify-done.sh` 同目录 | 弹 Windows toast + 用 WinRT OneCore 嗓音 **Yaoyao（女声）** 朗读任务名；找不到该嗓音则退回默认 |
| `.codex/hooks.json` | `~/.codex/hooks.json` | Codex `SessionStart` + `UserPromptSubmit` hook；仅交互式 TUI 按 chat 的 `session_id` 重置并生成任务标题，`codex exec/review` 不会改名 |
| `.codex/bin/codex-pane-title.py` | `~/.codex/bin/codex-pane-title.py` | 新 chat（含 `/clear`）先清旧标题，首轮再注入“约 20 字，优先包含 venue、策略/通道与动作”的语义命名指令；同 chat 的后续追问不覆盖，旧轮次也不能越过 `/clear` 回写 |
| `.codex/bin/codex-notify-done.sh` | `~/.codex/bin/codex-notify-done.sh` | Codex `notify` 入口；按系统选择通知和语音，在每轮完成时朗读 tmux 任务名 |

## 安装（软链接方式）

```bash
cd ~/agent-config
./install.sh
tmux source-file ~/.tmux.conf   # 让运行中的 tmux 立即生效
```

`install.sh` 会识别 macOS / WSL / Linux，大部分配置以软链接安装。若目标位置已有普通文件，
脚本会先生成带时间戳的 `.bak.*` 备份，不会直接覆盖。`~/.claude/settings.json`
例外：它会由通用模板加上本机自动探测到的 MCP 路径生成，因此是本地文件而非软链接。

> 注：仓库里是**副本**，之后改了本机 `~` 下的文件记得同步回仓库再 commit。
> 想让仓库成为唯一真源、编辑自动同步，可改用 symlink（如 GNU stow）。

## 依赖

- macOS 通知使用系统自带的 `osascript`，语音优先播放 edge-tts 产物，否则回退到 `say`。
- WSL 的 Windows toast 需 PowerShell 模块 `BurntToast`（`Install-Module BurntToast`），原有逻辑保持不变。
- 原生 Linux 通知优先使用 `notify-send`，MP3 播放使用 `mpv`（存在时）。
- `~/.codex/config.toml` 的 `notify` 需使用当前用户的绝对路径，例如 macOS 上
  `notify = ["bash", "/Users/<用户名>/.codex/bin/codex-notify-done.sh"]`。
- Codex 首次发现或脚本变更后会要求审核 hook；在 Codex 内运行 `/hooks` 并信任该用户级 hook。

`install.sh` 会确保 Claude 官方 `superpowers` 插件已安装并启用，同时自动探测
`openspec-mcp` 可执行文件或包含 `openspec_mcp` 模块的 Python 虚拟环境。未安装且
系统存在 `uv` 时，会自动安装 `openspec-mcp` 并固定兼容的 `mcp<2`，然后通过
`claude mcp add --scope user` 注册，不依赖 Claude 不读取的 `settings.json.mcpServers` 路径。
非标准安装位置可在执行时显式传入：

```bash
OPENSPEC_MCP_PYTHON=/path/to/venv/bin/python \
./install.sh
```

## 不包含

不含任何凭证 / token / cookie / 会话数据（如 `~/.claude/.credentials.json`、`~/.claude/projects/` 等），仅纯配置。
