# Attachments and voice

[English](../en/voice.md) · [한국어](../ko/voice.md) · [すべてのページ](../README.md)

ターミナルをテキストだけのものでなくする2つのこと。プロンプトに画像を落とすことと、打ち込む代わりにエージェントに話しかけることです。

このページはデーモン側の半分です。何を添付できるのか、どの音声バックエンドがあるのか、それぞれに何が必要なのか。それを行うキー — チップ、`Ctrl+V`、`/voice`、`Ctrl+Space` — は [Terminal UI](tui.md) にあります。

## Attachments

ファイルをプロンプトに貼るかドラッグすると、添付のチップになります。プロンプトを送るとファイルも一緒に行きます。

```text
[📎 screenshot.png 1.2MB]
> what is wrong with this layout?
```

添付できるもの。

| Kind | Types | What the model gets |
|---|---|---|
| 画像 | PNG, JPEG, GIF, WebP | モデルに視覚があれば、画像そのもの |
| テキスト | `.txt`, `.md`, ソースファイル, JSON | ファイルのテキストがターンにインライン展開されます |
| PDF | `application/pdf` | 受け付けるベンダーでは、文書そのもの |
| それ以外 | — | 名前と種類。ツールで読めばよいとエージェントに分かります |

規則は、すべてデーモンが強制します。

- **種類は名前ではなくバイト列から決まります。** `notes.txt` という名前のバイナリはバイナリとして扱われます。
- **1添付あたり 20MB。** これを超えると、上限を示すメッセージとともに拒否されます。貼り付けた（インラインの）添付はさらに WebSocket のフレームサイズ、およそ 3MB に制限されます。それより大きいものはパスとして送る必要があり、ターミナル UI はもともとそうしています。
- **Pillow が入っていれば、画像は長辺 1568px に縮小されます。** インストーラーが追加します。[Install](install.md) を参照してください。なくても画像は原寸で送られ、動くことは動きますが、トークンを余計に使います。
- **指し示したファイルがコピーされることは決してありません。** 保存されるのは貼り付けられたバイト列だけで、`$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>` の下に内容で重複排除されて置かれます。同じスクリーンショットを10回貼っても、ファイルは1つです。

### Models without vision

すべてのモデルが見えるわけではありません。いま動いているモデルが見えないとき、画像はターンを失敗させません。モデルが対処できるマーカーとして届きます。

```text
[image attached: screenshot.png] (this model cannot see images; ask the user to
describe it, or read the file from disk with a tool)
```

Anthropic と Gemini のモデルはすべて視覚ありとして扱われます。OpenAI 互換のベンダーではモデル名で判断し、見覚えのない名前 — ローカルの GGUF、先週出たモデル — はテキスト専用とみなされます。失敗するリクエストより、機能を落とすほうがましだからです。ローカルのモデルが実際には画像を見られるのに snowpea が違うと言う場合は、モデル ID を添えて issue で教えてください。

## Voice

音声は、マシンにそれを行う何かがあるときに動きます。必須のものは何もなく、欠けているものは黙って飛ばされるのではなく報告されます。

```bash
snowpea setup audio
```

このセクションは2つのことを尋ねます — どう聞くか、どう話すか — そして、このマシンに実際に入っているバックエンドを表示します。

### Speech to text

| Provider | Needs | Notes |
|---|---|---|
| `local-whisper` | `PATH` 上の `whisper` または `faster-whisper` | 何もマシンの外に出ません |
| `openai` | すでに設定済みの OpenAI API キー | `whisper-1` または `gpt-4o-transcribe` |
| `command` | `{path}` を含むコマンドテンプレート | 標準出力が書き起こしになります |
| `auto`（デフォルト） | — | ローカルの whisper、次に OpenAI、次にコマンド |
| `off` | — | 決して聞きません |

`auto` がローカルの CLI を優先するのは意図的です。書き起こしは、放っておくとあなたの部屋の音声がマシンの外に出る唯一の経路だからです。

### Text to speech

| Provider | Needs | Notes |
|---|---|---|
| `studio` | snowpea-studio の MCP サーバー | 動かしているなら、最良の声 |
| `openai` | OpenAI API キー | `tts-1` または `gpt-4o-mini-tts` |
| `edge-tts` | `PATH` 上の `edge-tts` | ニューラル音声、ネットワークを使います |
| `piper` | `PATH` 上の `piper` | ローカルのニューラル音声 |
| `say` | macOS | 標準搭載 |
| `espeak-ng` | `PATH` 上の `espeak-ng` | 小さく、機械的で、どこにでもある |
| `powershell` | Windows | SAPI、標準搭載 |
| `command` | `{text}` と `{out}` を含むテンプレート | 音声ファイルを書き出します |
| `auto`（デフォルト） | — | studio、次に OpenAI、次に最初に見つかったローカルのもの |
| `off` | — | 決して話しません |

### Recording

マイクの録音には `rec`（sox）、`arecord`（alsa-utils）、`ffmpeg` のいずれかが必要です。再生には `afplay`、`paplay`、`aplay`、`ffplay`、`mpv` のいずれかが必要です。どちらもないマシンでも、ターミナル UI は自分で再生できるように音声ファイルを送ることはできます。

### The agent can use voice too

エージェント自身がこの作業をできるように、ツールが2つあります。

| Tool | What it does |
|---|---|
| `transcribe_audio` | 音声ファイルを読み、何が話されたかを返します |
| `text_to_speech` | 何かを話して音声ファイルを返します。`play: true` ならここで発話します |

どちらもバックエンドが存在するまでは inactive で、`snowpea tools list` がそう言います。

```bash
snowpea tools list
```

## Settings

上記はすべて `$SNOWPEA_HOME/settings.json` の `audio` の下にあり、デーモンは再起動なしで編集を拾います。

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

`autoSpeak` はすべての返答を読み上げます。デーモンは返答が終わった時点で音声を合成し、このマシンにプレイヤーがあれば再生し、いずれにせよそのファイルを載せた `audio.spoken` セッションイベントを発行します。別のマシンのクライアントが自分で再生できるようにするためです。発話がターンに影響することはありません。バックエンドの欠如やプレイヤーの故障は、失敗した答えではなく、ログの1行と無音の返答になります。`player` と `recorder` は、最初に見つかったものではなく特定のツールを強制します。

## When nothing happens

デーモンに何ができるかを尋ねてください。オフになっている機能にはすべて、オフである理由 — どのパッケージを入れるか、どのキーを設定するか — が添えられます。

```bash
snowpea tools list --json
```

ターミナル UI が起動時に、そして設定が変わるたびに読むのも同じレポートです。`/voice` や `/tts` がバックエンドがないと言うとき、その文はこのレポートのものであって推測ではありません。そしてバックエンドを入れればそれで十分で、再起動は要りません。
