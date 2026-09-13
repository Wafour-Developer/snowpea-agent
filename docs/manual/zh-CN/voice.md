# Attachments and voice

[English](../en/voice.md) · [한국어](../ko/voice.md) · [全部页面](../README.md)

有两件事让终端不再只是纯文本：把一张图片丢进 prompt 里，以及用说的跟 agent 交流而不是敲字。

本页讲的是守护进程那一半：可以附上什么、有哪些语音后端、每个后端需要什么。做这些事的按键——chip、`Ctrl+V`、`/voice`、`Ctrl+Space`——在 [Terminal UI](tui.md) 里。

## 附件

把一个文件粘贴或拖进输入行，它就成为一个附件 chip。发送 prompt 时文件跟着一起走：

```text
[📎 screenshot.png 1.2MB]
> what is wrong with this layout?
```

可以附上什么：

| 类别 | 类型 | 模型拿到什么 |
|---|---|---|
| 图片 | PNG、JPEG、GIF、WebP | 图片本身，前提是模型有视觉能力 |
| 文本 | `.txt`、`.md`、源码文件、JSON | 文件的文本，内联进这一回合 |
| PDF | `application/pdf` | 文档本身，在接受 PDF 的供应商上 |
| 其他任何东西 | — | 名字和类型，好让 agent 知道要用工具去读它 |

规则，全部由守护进程强制执行：

- **类型由字节决定，而不是名字。** 一个叫 `notes.txt` 的二进制文件会被当作二进制处理。
- **每个附件 20MB。** 更大的会被拒绝，并给出写明上限的消息。粘贴（内联）的附件还会进一步受 websocket 帧大小限制，大约 3MB——再大就只能以路径形式发送，而这本来就是终端 UI 的做法。
- **图片在长边上被缩放到 1568px**，前提是装了 Pillow。安装器会带上它；见 [Install](install.md)。没有它的话图片按原尺寸发送，仍然可用，只是更费 token。
- **你指向的文件永远不会被复制。** 只有粘贴进来的字节会被保存，位置是 `$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>`，并按内容去重——同一张截图粘十次也只是一个文件。

### 没有视觉能力的模型

不是每个模型都看得见。当前模型看不见时，一张图片不会让这一回合失败：它会以一个模型可以据此行动的标记形式到达。

```text
[image attached: screenshot.png] (this model cannot see images; ask the user to
describe it, or read the file from disk with a tool)
```

Anthropic 和 Gemini 的模型一律被视为具备视觉能力。对于 OpenAI 兼容的供应商，由模型名字决定；认不出来的名字——一个本地 GGUF、一个上周才发布的模型——会被假定为只有文本能力，因为降级总比请求失败好。如果你的本地模型确实能看图片而 snowpea 不这么认为，请带上模型 id 开一个 issue 说明。

## 语音

只有当这台机器上有东西能干这个活的时候，语音才工作。什么都不是必需的，缺什么都会被报告出来，而不是悄悄跳过。

```bash
snowpea setup audio
```

这一节会问两个问题——怎么听，以及怎么说——并显示这里实际装了哪些后端。

### 语音转文字

| 供应商 | 需要 | 说明 |
|---|---|---|
| `local-whisper` | `PATH` 上有 `whisper` 或 `faster-whisper` | 没有任何东西离开这台机器 |
| `openai` | 你已经配好的 OpenAI API 密钥 | `whisper-1` 或 `gpt-4o-transcribe` |
| `command` | 一个包含 `{path}` 的命令模板 | stdout 就是转写结果 |
| `auto`（默认） | — | 先本地 whisper，然后 OpenAI，然后那个命令 |
| `off` | — | 永不收听 |

`auto` 刻意优先选本地 CLI：转写是唯一一条否则会让你房间里的声音离开这台机器的路径。

### 文字转语音

| 供应商 | 需要 | 说明 |
|---|---|---|
| `studio` | snowpea-studio MCP server | 最好的音色，如果你跑了一个的话 |
| `openai` | OpenAI API 密钥 | `tts-1` 或 `gpt-4o-mini-tts` |
| `edge-tts` | `PATH` 上有 `edge-tts` | 神经网络音色，走网络 |
| `piper` | `PATH` 上有 `piper` | 本地神经网络音色 |
| `say` | macOS | 系统自带 |
| `espeak-ng` | `PATH` 上有 `espeak-ng` | 小巧、机械、到处都有 |
| `powershell` | Windows | SAPI，系统自带 |
| `command` | 一个带 `{text}` 和 `{out}` 的模板 | 写出一个音频文件 |
| `auto`（默认） | — | 先 studio，然后 OpenAI，然后第一个本地的 |
| `off` | — | 永不朗读 |

### 录音

录麦克风需要 `rec`（sox）、`arecord`（alsa-utils）或 `ffmpeg` 中的一个。播放需要 `afplay`、`paplay`、`aplay`、`ffplay` 或 `mpv` 中的一个。在两者都没有的机器上，终端 UI 仍然可以把音频文件发给你，让你自己播放。

### agent 也能用语音

两个工具，好让 agent 自己干这些活：

| 工具 | 它做什么 |
|---|---|
| `transcribe_audio` | 读一个音频文件并返回其中说了什么 |
| `text_to_speech` | 说一段话并返回音频文件；`play: true` 会在这里播出来 |

在后端存在之前这两个都处于未激活状态，`snowpea tools list` 会这么告诉你：

```bash
snowpea tools list
```

## 设置

以上所有内容都位于 `$SNOWPEA_HOME/settings.json` 的 `audio` 之下，守护进程无需重启就会拾取修改：

```json
{
  "audio": {
    "stt": { "provider": "auto", "command": null, "model": null },
    "tts": {
      "enabled": true,
      "provider": "auto",
      "voice": null,
      "autoSpeak": false
    },
    "player": null,
    "recorder": null
  }
}
```

`autoSpeak` 会把每一条回复都朗读出来：守护进程在回复结束时合成语音，如果这台机器有播放器就播放它，并且无论如何都会发出一个带着音频文件的 `audio.spoken` 会话事件——于是另一台机器上的客户端可以自己播放。朗读永远不会影响回合：缺少后端或播放器坏掉只会留下一行日志和一条无声的回复，而不是一个失败的答案。`player` 和 `recorder` 用来强制指定某个工具，而不是用第一个找到的。

## 什么都没发生的时候

去问守护进程它能做什么。每一项关着的能力都会附上它关着的原因——该装哪个包，该设哪个密钥：

```bash
snowpea tools list --json
```

终端 UI 在启动时、以及每当某个设置变化时读的就是同一份报告。当 `/voice` 或 `/tts` 说某个后端不存在时，那句话来自这份报告，而不是猜的——并且装上那个后端就够了，不需要重启。
