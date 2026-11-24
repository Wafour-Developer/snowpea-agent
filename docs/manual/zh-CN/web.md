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

2. 设置密码(至少 8 个字符)。会询问两次，只以加盐的 scrypt 哈希保存在 `credentials.json`(权限 0600)中，绝不写入 `settings.json`。`--stdin` 供脚本使用，读取一行；`--clear` 删除密码。

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
- 会话结束的页面(过期、密码更改，或在别处退出登录)会返回登录界面。
- 远程监听器只提供页面、登录和 WebSocket(没有 `/health`、`/version` 或协议架构)，WebSocket 会拒绝来自其他来源的页面。

### 用隧道获得公网地址

`snowpea web` 可以通过隧道程序为你打开一个公网 HTTPS 地址，不必开放端口或自己架设代理，就能从手机或其他网络访问这台电脑。共有三种提供方:

| 提供方 | 需要什么 | 地址 |
|---|---|---|
| `cloudflare-quick` | `cloudflared`;无需账户 | 随机的 `https://….trycloudflare.com`，每次启动都会变 |
| `cloudflare` | `cloudflared`、Cloudflare 隧道令牌及其主机名 | 你自己的 `https://<主机名>` |
| `ngrok` | `ngrok` 及其 authtoken | ngrok 给你的 `https://….ngrok-free.app` 地址 |

只有 **远程访问已开启并且已设置网页密码** 时，隧道才会启动(`snowpea web --host … --port …`、`snowpea web password`)：只有访问链接而没有密码，不允许开放公网地址。监听器必须能从本机的回环地址访问(`0.0.0.0` 或 `127.0.0.1`)，因为隧道程序从这里连接它。

```bash
snowpea web tunnel install                      # 缺少隧道程序时下载
printf '%s\n' "$TOKEN" | snowpea web tunnel token --provider cloudflare   # 或 ngrok；从标准输入读取
snowpea web tunnel start --provider cloudflare-quick --save   # --save 让下次启动仍使用该提供方
snowpea web tunnel status                       # 状态、地址、程序、是否已设置令牌
snowpea web tunnel stop
```

设置项(`web.remote.tunnel`，只能在这台电脑上更改)：`provider`(`off`、`cloudflare-quick`、`cloudflare`、`ngrok`)、`autoStart`(设置了提供方且条件满足时随守护进程启动，默认开启)、`hostname`(`cloudflare` 隧道的公网主机名；该提供方没有它就不会启动)、`binary`(优先于 `PATH` 和 `~/.snowpea/bin` 使用的隧道程序)。

- **程序。** snowpea 依次在 `binary`、`PATH`、`~/.snowpea/bin` 中查找。`snowpea web tunnel install` 会从 cloudflared 官方 GitHub 发布页下载固定版本，并且只有 sha256 与固定值一致时才安装，不一致则什么都不写入。没有固定校验值的情况(macOS，以及所有平台的 ngrok)，它会拒绝安装，并给出你自己运行的命令(`brew install cloudflared`、`brew install ngrok`、`snap install ngrok`、`winget install …`)。
- **令牌。** ngrok 的 authtoken 和 Cloudflare 隧道令牌保存在 `credentials.json`(权限 600)中，绝不写入 `settings.json`。用 `snowpea web tunnel token --provider …`(标准输入，一行)设置，或在客户端的设置中填写。令牌通过环境变量而不是命令行传给隧道程序，并且会从 snowpea 记录或显示的任何内容中去除。
- **重启。** 隧道程序停止后，snowpea 会在 2 秒、4 秒、8 秒……后重新启动，间隔最长 60 秒。如果连续五次启动都没有显示地址，它就放弃，原因可以在 `snowpea web tunnel status` 中看到。快速隧道(`cloudflare-quick`)每次重启都会得到新地址，客户端通过 `web.tunnel.changed` 事件得知。
- **来源与代理头。** 隧道运行期间，其地址是页面 WebSocket 允许的来源；访客的地址和 `https` 方案取自隧道的头部(`CF-Connecting-IP`、`X-Forwarded-*`)，但只信任来自本机的连接(即隧道程序所连接的那一端)。`allowedOrigins` 不会被写入任何内容。
- **经隧道使用终端。** 通过隧道打开的页面在登录后可以查看终端。但启动终端、输入内容或使用危险选项需要 **终端解锁**：页面会再次要求输入网页密码(`web.tunnel.unlock`)，解锁在该次登录期间有效。若 `maxMode` 不是 `auto`，经隧道的危险选项会被拒绝。

隧道会让任何能猜到地址的人访问页面，因此本页上方关于远程登录的所有注意事项都适用：使用足够长的密码，不需要时就停止隧道。

## 界面

布局参照 DeepSeek Harness 的 Web 客户端：三列，对话在中间。

| 列 | 内容 |
|---|---|
| 左 — 侧边栏 | **New Session**(新会话)、Plugins(插件)、Automation tasks(自动化任务)、More(其他界面：skill、调度、设置……)，然后是 **Workspaces**(工作区)：每个项目文件夹一组，列出其中的对话，置顶的排在前面。行上的圆点表示有一个回合正在运行、有审批在等待，或有未读内容。行菜单可以置顶、重命名、分叉、复制 id 或归档。 |
| 中 — 对话 | 标题栏显示对话标题、它由哪些父对话委派或分叉而来，以及两个标签页：**Chat**(聊天：消息、工具卡片和输入框)和 **Trajectory**(轨迹，见下文)。 |
| 右 — 停靠区 | 在对话旁边打开的标签页：文件、工作区文件树、计划、委派的工作、变更，以及在 IDE 中的终端。窗格可以拆分，标签页可以在窗格之间移动；布局按对话保存在此浏览器中。 |

侧边栏宽 280 px(拖动边缘可在 264 到 420 之间调整)，窗口小于 1024 px 时折叠为窄栏，小于 768 px 时变成抽屉；`Ctrl/⌘ B` 切换显示。停靠区以窗口的 45 % 打开，会记住你设置的宽度，最多占 70 %，如果会让对话窄于 400 px 就直接关闭；`Ctrl/⌘ J` 切换显示。

停靠区的空窗格会显示 **Start** 标签页，其中有工作区文件、变更和计划的卡片(终端卡片仅在 IDE 中可用)。状态栏显示连接状态，打开的标签页会让守护进程不因空闲而关闭。

## 在停靠区中打开文件

对话中提到的文件会在停靠区里以标签页打开，而不是新窗口：

- 指向文件的 Markdown 链接(`[math.ts]` + `(math.ts)`)、相对或绝对路径、Windows 路径；
- 行号或范围：`#L24`、`#L24-L30`、`:24`、`:24-30`；
- 行内代码，例如 `` `src/app.ts` ``，但仅限该回合读取或写入过的文件；
- 回复文本中的文件名(如 `notes.md`)，且该回合读取或写入过该文件时。其他文件名保持为普通文本；普通文本只有在是明确的 http(s)、mailto: 或 www. 地址时才会变成链接。
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
- 终端、文件和工作树界面、连接管理器和应用自更新(这些地址会返回到对话)；
- 原生的文件夹选择器——请输入路径来添加工作区；

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
