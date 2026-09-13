# Execution backends

[English](../en/backends.md) · [한국어](../ko/backends.md) · [全部页面](../README.md)

每一个会碰文件系统或者会执行命令的工具，都要经过一个执行 backend。backend 决定这件事发生在*哪里*。工具集本身不变：`read_file`、`shell`、`grep`、`git_commit` 以及其余工具，不管跑在你的笔记本上、容器里，还是网络另一头的主机上，行为都完全一致。

## 切换

```
/backend
/backend local
/backend docker {"image": "python:3.11-slim"}
/backend ssh {"host": "10.0.0.5", "user": "build", "key": "~/.ssh/id_ed25519"}
```

不带参数的 `/backend` 会打印当前使用的 backend。改动会立即对会话生效，包括已经在进行中的工作，从下一次工具调用开始。项目可以在 `<project>/.snowpea/settings.json` 的 `backend` 项下设定自己的默认值。

## local

默认项。命令以你的用户身份、在会话的工作目录中、带着你的环境执行。什么都没有被隔离——这是你处理自己代码时想要的 backend，同时也是你不该把 auto 模式的任务指过去的那一个。

## docker

```
/backend docker {"image": "python:3.11-slim"}
```

| 键 | 默认值 | 含义 |
|---|---|---|
| `image` | `python:3.11-slim` | 用来创建容器的镜像 |
| `workdir` | 会话的 workdir | 在容器内的同一绝对路径上做 bind mount |

容器以会话命名，在第一次工具调用时惰性创建，并在会话关闭时移除。工作目录被 bind mount 到同一个绝对路径上，因此模型推理时用的路径在容器内外是一样的。其余的一切——装好的软件包、网络、环境——都属于那个容器。

这正是适合 auto 模式的 backend。agent 想装什么就装什么，想弄坏什么就弄坏什么，容器扔掉即可。

它需要一个正在运行的 Docker 守护进程。没有的话，这个 backend 会如实报告失败，而不是悄悄回落到 local。

## ssh

```
/backend ssh {"host": "10.0.0.5", "port": 22, "user": "build", "key": "~/.ssh/id_ed25519"}
```

| 键 | 含义 |
|---|---|
| `host` | 主机名或地址 |
| `port` | 默认 22 |
| `user` | 远程用户 |
| `key` | 私钥路径 |
| `password` | `key` 的替代方案 |
| `cwd` | 远程工作目录，默认是远程主目录 |

命令通过 SSH 执行，文件读写走 SFTP。agent 写出来的文件只存在于远程主机上——本地不会镜像任何东西，在 ssh backend 下的一次 `write_file` 不会动你自己的磁盘。强烈建议使用密钥认证；写进会话配置里的密码，说到底就是配置文件里的一个密码。

## 确认自己此刻在哪里

诚实的测试方法是直接问：

```bash
snowpea -c "run hostname and tell me what it printed"
```

在 `local` 下是你的机器，在 `docker` 下是容器 id，在 `ssh` 下是远程主机。既然 agent 对文件系统的全部视野都要经过 backend，这件事它不可能弄错。

## 不会跟着走的东西

backend 管的是工具执行，不是守护进程。无论某个会话用的是哪个 backend，会话、记忆、审批队列、调度器和 gateway 都留在你机器上的守护进程里。网络类工具——`web_search`、`web_extract`、浏览器工具、媒体工具和 MCP server——是从守护进程运行的，而不是从 backend 主机。

## 下一步

[Headless](headless.md) —— 在脚本里用上这一切。
