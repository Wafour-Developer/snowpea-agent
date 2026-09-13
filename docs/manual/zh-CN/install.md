# Install

[English](../en/install.md) · [한국어](../ko/install.md) · [全部页面](../README.md)

snowpea 需要两种运行时：Python 3.11+（由 [uv](https://docs.astral.sh/uv/) 管理）和 Node 20+。安装脚本会在缺失时把两者都装好。终端 UI 已预打包进 Python wheel 中，因此你的机器上不需要执行 `npm install`。

## 一行命令安装

**macOS、Linux、WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
```

**Windows（PowerShell）**

```powershell
iwr https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex
```

然后验证一下：

```bash
snowpea --version
```

该脚本是幂等的——再次运行会原地升级。它会通过官方安装器安装 uv，确认 `node --version` 报告的版本不低于 20，把 `snowpea` 命令作为 uv tool 安装，并在 `~/.local/bin` 尚未加入 `PATH` 时把这行追加到你的 shell 配置文件中。在 Windows 上，数据目录是 `%LOCALAPPDATA%\snowpea`，而不是 `~/.snowpea`。

如果想在不做任何改动的情况下看看它会做什么，可以先下载脚本再加上 `--dry-run`：

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh -o install.sh
sh install.sh --dry-run
```

## 手动安装

如果你不想把脚本直接管道到 shell 里执行，或者一行命令在某一步失败了、你想自己处理：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, if missing
uv tool install snowpea-agent
snowpea --version
```

终端 UI 需要 Node 20+ 在 `PATH` 中。无界面运行（`snowpea -c`）以及每个 `snowpea <subcommand>` 都不依赖 Node；只有 TUI 需要它。

## 安装来源覆盖

安装脚本接受一个来源覆盖，CI 和离线安装用的就是它：

```bash
sh install.sh --from-checkout
SNOWPEA_WHEEL_URL=./snowpea_agent-0.1.0-py3-none-any.whl sh install.sh
SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent sh install.sh
```

`SNOWPEA_BIN_DIR` 改变 `snowpea` 可执行文件最终落到哪里，而 `--from-checkout` 安装的是脚本自己所在的那份 checkout。

## 从源码检出安装

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync
npm ci
npm run build
uv run snowpea --version
```

`npm run build` 会生成 `tui/dist/snowpea-tui.js`，`snowpea` 会在预打包的包体之后寻找它。在开发 UI 时，可以把 `SNOWPEA_TUI_ENTRY` 指向你自己的入口文件，以完全跳过打包产物。

## 升级

snowpea 每天在后台检查一次是否有更新的发行版，并把答案缓存在 `$SNOWPEA_HOME/update-check.json` 中。没有你点头，什么都不会被安装。

**在终端 UI 里。** 有更新在等着时，屏幕顶部会出现一条横幅：

```text
⬆ Update available v0.1.2 (current v0.1.1) — press U or type /update
```

按 `U`（或者输入 `/update`）并回答 `y`。升级在后台进行，横幅报告进度，完成后 snowpea 会以新版本自行重启。正在进行的回合绝不会被打断。

**从命令行。**

```bash
snowpea update --check
snowpea update
```

`--check` 报告当前版本和最新版本，不安装任何东西。不加它时，`snowpea update` 会执行升级、停止守护进程，并告诉你再次启动 `snowpea`。`snowpea --version` 也会提到有待处理的更新，但只用缓存里的答案，所以它绝不会等网络。

升级会把输出写进 `$SNOWPEA_HOME/logs/update.log`。它执行的是 `uv tool install --force --reinstall`；如果 `uv` 不在 `PATH` 上，则改用安装脚本记录在 `$SNOWPEA_HOME/install.json` 里的方式；如果什么都不知道，snowpea 会打印出需要你手工执行的命令，而不是瞎猜。

在 `$SNOWPEA_HOME/settings.json` 中有两个设置控制这件事：

```json
{ "updates": { "check": true, "channel": "auto" } }
```

`check: false` 关掉每天的后台检查，`/update` 和 `snowpea update` 仍可按需使用。`channel` 可以是 `auto`（包发布在 PyPI 上时用 PyPI，否则用仓库的 git tag）、`pypi` 或 `git`。

如果想手工升级：

```bash
uv tool upgrade snowpea-agent
snowpea daemon stop
snowpea --version
```

手工升级后要停止守护进程。正在运行的守护进程会把旧代码留在内存里，而下一个连接进来的客户端会按照一个已经与已安装版本不匹配的协议版本进行协商。

## 卸载

```bash
snowpea daemon stop
uv tool uninstall snowpea-agent
```

这样会保留你的数据。如果也要一并删除，请删除 `$SNOWPEA_HOME`（即 `~/.snowpea`，在 Windows 上是 `%LOCALAPPDATA%\snowpea`）。如果你把守护进程注册成了服务，请先注销它：

```bash
snowpea service uninstall
```

## 出问题时

**安装完成后立即出现 `snowpea: command not found`。** 说明当前 shell 中 `~/.local/bin` 还没有加入 `PATH`。打开一个新终端，或者重新 source 你的 shell 配置文件。安装脚本会追加那一行，但无法改变你当前所在的这个 shell。

**Node 缺失或版本过旧。** TUI 无法启动。请通过你的包管理器或 [nodejs.org](https://nodejs.org) 安装 Node 20+，然后再次运行 `snowpea`。在此期间其他一切照常工作：

```bash
snowpea -c "hello" --json
```

**守护进程无法启动。** 查看 `$SNOWPEA_HOME/logs/daemon.log`，并检查是否有陈旧进程占用了记录的端口：

```bash
snowpea daemon status
snowpea daemon stop
snowpea daemon start
```

`daemon status` 会读取 `$SNOWPEA_HOME/daemon.json` 并验证该 pid 是否存活。如果文件陈旧而 pid 已经不存在，删除它是安全的。

**企业代理或离线机器。** `uv tool install` 需要访问 PyPI。请在运行安装脚本前设置 `HTTPS_PROXY`，或者使用你自带的 wheel 文件安装：`uv tool install ./snowpea_agent-0.1.0-py3-none-any.whl`。

## 下一步

[Setup](setup.md) —— 选择一个供应商并配好密钥。
