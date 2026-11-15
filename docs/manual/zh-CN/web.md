# snowpea web

`snowpea web` 在浏览器中打开智能体。它与 snowpea IDE 是同一个界面——同样的侧边栏、对话、轨迹(Trajectory)和停靠区(dock)——由本地守护进程提供，所以无需安装其他东西，也没有任何数据离开你的机器。

```bash
snowpea web            # start the daemon if needed, print the address, open it
snowpea web --no-open  # print the address only
```

地址形如 `http://127.0.0.1:<port>/web/?token=…`。token 是守护进程自己的(即 `$SNOWPEA_HOME/daemon.json` 中的那个)；页面会把它保存在该浏览器标签页中，并从地址栏里移除。任何拿到 token 的人都能操控智能体，所以不要把这个地址贴到聊天或工单里。这个地址只能在运行守护进程的机器上打开。

## 从另一台机器访问

最简单的办法仍然是 ssh 隧道：不会多监听任何端口，连接也是加密的。

```bash
ssh -L <port>:127.0.0.1:<port> host
```

要让同一网络里的笔记本或手机直接打开，请开启远程访问。它是另一个用用户名和密码保护的监听器，回环监听器照旧工作。

1. 在 `settings.json` 里设置用户名(需要的话也设置地址)：

   ```json
   { "web": { "remote": { "enabled": true, "username": "me" } } }
   ```

2. 设置密码。会询问两次，只以加盐的 scrypt 哈希保存在 `credentials.json`(权限 0600)中，绝不写入 `settings.json`。`--stdin` 供脚本使用，读取一行；`--clear` 删除密码。

   ```bash
   snowpea web password
   snowpea web password --stdin
   snowpea web password --clear
   ```

3. 重启守护进程(`snowpea daemon stop`，然后 `snowpea web`)。设置在守护进程启动时读取，运行中的守护进程不会重新绑定。

之后 `snowpea web` 还会打印给其他机器用的地址 `http://<本机局域网地址>:8787/web/`，打开后是登录页面。`snowpea web status` 显示远程访问是否开启、监听地址、用户名、是否已设置密码以及已登录的会话数。

| 设置 (`web.remote.…`) | 默认值 | 含义 |
|---|---|---|
| `enabled` | `false` | 启动第二个监听器。 |
| `host` | `0.0.0.0` | 监听地址(`0.0.0.0` 表示所有网卡)。 |
| `port` | `8787` | 监听端口。 |
| `username` | `""` | 唯一可以登录的账号。 |
| `sessionHours` | `12` | 一次登录的有效时长。 |
| `allowedOrigins` | `[]` | WebSocket 额外接受的页面来源，例如代理后面的 `https://snowpea.example.com`。 |
| `trustProxy` | `false` | 信任你自己运行的反向代理发来的 `X-Forwarded-Proto` / `X-Forwarded-For`。 |

如果开启了远程访问却没有用户名或密码，守护进程只在回环地址上启动，把原因写进日志，`snowpea web` 也会说明原因。

安全提示：

- **这是明文 HTTP。** 密码和所有操作都不加密传输。在可信网络之外，请把它放在 TLS 后面(Caddy、nginx 等反向代理，并设置 `trustProxy: true`)，或通过 VPN 或上面的 ssh 隧道访问。
- 登录的人可以操控智能体，也就能操作这台机器的 shell 和文件。请使用足够长的密码。
- 同一地址 5 分钟内登录失败 5 次，或全局失败 20 次，就会锁定登录直到这 5 分钟结束。登录成功和失败都会连同客户端地址写入日志，但绝不记录密码。
- 登录凭据是一个 cookie(`HttpOnly`、`SameSite=Strict`，HTTPS 下带 `Secure`)。会话只保存在守护进程内存中，重启守护进程会让所有人退出登录。更改或清除密码(`snowpea web password`)会让所有人立即退出登录；其他会话持续到过期或退出登录。
- 远程监听器只提供页面、登录和 WebSocket(没有 `/health`、`/version` 或协议架构)，WebSocket 会拒绝来自其他来源的页面。

## 界面

布局参照 DeepSeek Harness 的 Web 客户端：三列，对话在中间。

| 列 | 内容 |
|---|---|
| 左 — 侧边栏 | **New Session**(新会话)、Plugins(插件)、Automation tasks(自动化任务)、More(其他界面：skill、调度、设置……)，然后是 **Workspaces**(工作区)：每个项目文件夹一组，列出其中的对话，置顶的排在前面。行上的圆点表示有一个回合正在运行、有审批在等待，或有未读内容。行菜单可以置顶、重命名、分叉、复制 id 或归档。 |
| 中 — 对话 | 标题栏显示对话标题、它由哪些父对话委派或分叉而来，以及两个标签页：**Chat**(聊天：消息、工具卡片和输入框)和 **Trajectory**(轨迹，见下文)。 |
| 右 — 停靠区 | 在对话旁边打开的标签页：文件、工作区文件树、计划、委派的工作、变更，以及在 IDE 中的终端。窗格可以拆分，标签页可以在窗格之间移动；布局按对话保存在此浏览器中。 |

侧边栏宽 280 px(拖动边缘可在 264 到 420 之间调整)，窗口小于 1024 px 时折叠为窄栏，小于 768 px 时变成抽屉；`Ctrl/⌘ B` 切换显示。停靠区以窗口的 45 % 打开，会记住你设置的宽度，最多占 70 %，如果会让对话窄于 400 px 就直接关闭；`Ctrl/⌘ J` 切换显示。

## 在停靠区中打开文件

对话中提到的文件会在停靠区里以标签页打开，而不是新窗口：

- 指向文件的 Markdown 链接(`[math.ts]` + `(math.ts)`)、相对或绝对路径、Windows 路径；
- 行号或范围：`#L24`、`#L24-L30`、`:24`、`:24-30`；
- 行内代码，例如 `` `src/app.ts` ``，但仅限该回合读取或写入过的文件；
- 回复下方的文件标签和 artifact 卡片。

再次打开同一个文件会聚焦到它的标签页。预览与 IDE 相同：带高亮的代码、Markdown、图片、PDF、电子表格、Office 文档和 notebook。HTML 文件默认以**静态**方式显示(已清理、无脚本、严格的内容安全策略)；切换为**运行脚本**后会在隔离的框架中加载，它可以运行自己的脚本，但无法访问页面、你的 cookie 或守护进程。

在浏览器中，文件通过守护进程(`fs.*`)读取，并且只限于对话的文件夹：它的工作目录、工作区和附件。此范围之外的路径会被拒绝，就像智能体自己的工具会拒绝一样。

## 轨迹

**Trajectory** 标签页以步骤的形式展示智能体做了什么，而不是聊天形式：每个回合是一条带，其中每次模型调用是一个步骤，包含文本、推理和工具调用，每个工具调用显示参数、结果和耗时。委派的工作出现在它被启动的位置；点击它会在停靠区打开子对话，工具调用中的文件同样会按上文方式在停靠区打开。它由会话的事件日志(`session.trajectory`)生成，因此无论是正在运行的回合、已结束的回合，还是明天恢复的对话，看到的都一样。

## 分叉、置顶和归档

在一个已结束的回合上，你可以**分叉**对话：新对话带着直到该回合的历史开始，原对话保持不变。新对话的标题栏会显示它来自哪里。把文件恢复到某个回合之前(检查点)仍和以前一样；分叉用于尝试另一个方向而不丢掉当前这个。

**置顶**让对话固定在其工作区的顶部；**归档**把它从侧边栏隐藏但不删除(侧边栏的视图选项可以重新显示已归档的对话)。

## 浏览器做不到的事

页面无法访问你的操作系统，所以下面这些仍然只能在 IDE 和终端界面中使用：

- 终端(在终端中打开、新建终端)；
- 在本地运行的 git 操作，以及在文件夹中显示 / 用系统应用打开；
- 原生的文件夹选择器——请输入路径来添加工作区；
- 朗读回复。

这些功能的按钮在浏览器中会被隐藏，而不是点了失败。

## 页面从哪里来

Web 构建随 snowpea 包一起发布，由守护进程在 `/web/` 下提供。`index.html` 从不缓存，其他每个文件的名字里都带内容哈希，所以升级后重新加载总能拿到新版本。开发时可以用 `SNOWPEA_WEB_DIST` 让守护进程指向另一个构建：

```bash
# in snowpea-ide
npm run build:web
# then, with a separate home so your own daemon is not touched
SNOWPEA_WEB_DIST=$PWD/out/web snowpea --home /tmp/snowpea-dev web
```

如果构建缺失，`snowpea web` 会这样告诉你，而该地址会返回 `404`。

## 接下来

- [Terminal UI](tui.md) — 终端中的同一个智能体
- [Protocol](protocol.md) — 页面使用的 RPC(`workspace.*`、`session.trajectory`、`session.fork`、`session.lineage`、`fs.*`)
- [远程会话](../en/remote.md) — 在另一台机器上运行守护进程
