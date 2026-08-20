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
| `.codex/rules/default.rules` | `~/.codex/rules/default.rules` | Codex 用户级规则；允许 `git merge` 免二次确认 |
| `.claude/bin/claude-wrapper.sh` | `~/.claude/bin/claude-wrapper.sh` | 定义 `claude()` shell 函数，默认加 `--dangerously-skip-permissions` 并拒绝 `/mnt/*` 下的 Windows 版 claude 接管 WSL 仓库；bash / zsh 双兼容（纯 POSIX 循环遍历 `$PATH`，不用 `type -aP` / process substitution） |
| `.claude/bin/rc-debug.sh` | `~/.claude/bin/rc-debug.sh` | remote-control 断连排障：带 `--debug-file` 启动 + 后台采样 TCP 连接状态 |
| `.claude/bin/rc-disconnect-snapshot.sh` | `~/.claude/bin/rc-disconnect-snapshot.sh` | 手机端报 disconnected 时一键采证（bridge 进程活性 / poll 时间 / environment_id 等） |
| `.claude/bin/statusline-agent-name.sh` | `~/.claude/bin/statusline-agent-name.sh` | Claude Code `statusLine`：显示本 session 的跨 agent 通信名（查 `~/.claude/sessions/<pid>.json`）+ 当前 git 分支 + model |
| `.claude/settings.local.json` | `~/.claude/settings.local.json` | 个人 `permissions.allow` 白名单（仅命令前缀级授权，无凭据）。⚠️ 该文件名默认被本机全局 `~/.config/git/ignore` 忽略，本仓库 `.gitignore` 用 `!.claude/settings.local.json` 显式取消忽略 |
| `.claude/skills/cryptostruct-market-data/` | `~/.claude/skills/cryptostruct-market-data/` | 第三方行情数据技能（整目录 symlink），无凭据，仅文档 + 拉取脚本 |
| `shell/rc.snippet` | 注入 `~/.bashrc` / `~/.zshrc` | `ccyolo` alias + `claude-wrapper.sh` 的 source 行；用 `# >>> agent-config >>>` / `# <<< agent-config <<<` marker 包裹，幂等注入（重复执行 `install.sh` 不会重复追加） |
| `.codex/config.toml` | 与 `~/.codex/config.toml` 合并 | 见下方「codex config.toml 合并策略」 |

## 安装（软链接方式）

```bash
cd ~/agent-config
./install.sh
tmux source-file ~/.tmux.conf   # 让运行中的 tmux 立即生效
```

`install.sh` 会识别 macOS / WSL / Linux，大部分配置以软链接安装。若目标位置已有普通文件，
脚本会先生成带时间戳的 `.bak.*` 备份，不会直接覆盖。以下两个例外**不是** symlink，而是
每次执行 `install.sh` 时按本机情况**生成 / 合并**出来的本地文件：

- `~/.claude/settings.json`：由通用模板加上本机自动探测到的 MCP 路径生成。
- `~/.codex/config.toml`：见下方「codex config.toml 合并策略」。

`~/.bashrc` / `~/.zshrc` 里的 `ccyolo` alias + `claude-wrapper.sh` source 行通过
marker 包裹区间幂等注入（见上表 `shell/rc.snippet`），不是 symlink（rc 文件里其余
个人内容不能被覆盖，只能注入/替换 marker 内的这一段）。macOS 默认目标是
`~/.zshrc`，Linux/WSL 默认目标是 `~/.bashrc`；若两个 rc 文件在本机都存在，会同时注入两份，
保证换 shell 也生效。

### codex config.toml 合并策略

`~/.codex/config.toml` 除了 `model` / `model_reasoning_effort` / `notify` /
`[mcp_servers.openspec]` 这几个跨机器通用字段外，还含机器专属的
`[projects."<本机路径>"]` trust_level、`[hooks.state...]` 信任哈希、
`[tui.model_availability_nux]`、`[notice.model_migrations]`，不能直接整体覆盖或
symlink 模板。`install.sh` 的处理方式：

1. 若 `python3` ≥ 3.11（内置 `tomllib`）：解析本机已有的 `config.toml`（若存在），
   只覆盖 `model` / `model_reasoning_effort` / `notify` / `[mcp_servers.openspec]`
   四段（`notify` 里的路径和 `mcp_servers.openspec.command` 会替换成本机 `$HOME`
   与实际探测到的 openspec 可执行文件），其余段原样保留后写回；写回前会用
   `tomllib.loads()` 自检一遍，解析失败才落盘。原文件会先备份成
   `config.toml.bak.<timestamp>`。
2. 若本机没有该文件：直接按模板生成。
3. 若 `python3` < 3.11（没有 `tomllib`）：**不做合并**——已有文件保持原样不动，
   只在终端提示需要手动核对 `model` / `model_reasoning_effort` / `notify` /
   `[mcp_servers.openspec]` 是否要同步；没有旧文件时才按模板直接生成（不做
   TOML 解析校验）。这是已知限制，遇到旧版 python3 时按提示手动处理。

> 注：`.claude/settings.json` / `.codex/config.toml` 仓库里放的是**通用模板**，不是某台
> 机器的直接副本，改本机那两个文件不会回流仓库——要让改动跨机器生效必须改模板。
> **其余文件装完都是 symlink，仓库即唯一真源**：编辑 `~/.claude/bin/xxx.sh` 就是在编辑
> 仓库里的那份，`git status` 会直接看到，不需要"同步回仓库"这一步。

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
