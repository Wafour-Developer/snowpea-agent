# 첨부와 음성

터미널이 텍스트 전용에서 벗어나는 두 가지: 프롬프트에 이미지를 떨어뜨리는 것, 그리고 타이핑 대신 말로 시키는 것.

다른 언어: [English](../en/voice.md) · [전체 목록](../README.md)

이 문서는 데몬 쪽 절반입니다 — 무엇을 붙일 수 있는지, 어떤 음성 백엔드가 있는지, 각각 무엇이 필요한지. 실제로 그 일을 하는 키 — 칩, `Ctrl+V`, `/voice`, `Ctrl+Space` — 는 [터미널 UI](tui.md)에 있습니다.

## 첨부

프롬프트에 파일을 붙여넣거나 끌어다 놓으면 첨부 칩이 생깁니다. 프롬프트를 보내면 파일도 함께 갑니다.

```text
[📎 screenshot.png 1.2MB]
> 이 레이아웃 뭐가 문제야?
```

붙일 수 있는 것:

| 종류 | 형식 | 모델이 받는 것 |
|---|---|---|
| 이미지 | PNG, JPEG, GIF, WebP | 비전이 되는 모델이면 그림 자체 |
| 텍스트 | `.txt`, `.md`, 소스 파일, JSON | 파일 내용이 턴 안에 인라인으로 |
| PDF | `application/pdf` | 문서를 받는 벤더에서는 문서 그대로 |
| 그 외 | — | 이름과 형식 — 툴로 읽어야 한다는 것을 에이전트가 알 수 있게 |

규칙은 전부 데몬이 강제합니다.

- **형식은 이름이 아니라 바이트가 정합니다.** `notes.txt`라는 이름의 바이너리는 바이너리로 취급됩니다.
- **첨부 하나당 20MB.** 넘으면 한도를 밝히며 거절합니다. 붙여넣은(인라인) 첨부는 websocket 프레임 크기 때문에 약 3MB로 더 제한되며, 그보다 크면 경로로 보내야 합니다 — 터미널 UI가 원래 그렇게 합니다.
- **이미지는 긴 변 1568px로 축소됩니다** — Pillow가 설치되어 있을 때. 설치 스크립트가 같이 깔아 줍니다. [설치](install.md)를 보세요. 없으면 원본 크기로 보내지고, 동작은 하되 토큰을 더 씁니다.
- **경로로 가리킨 파일은 복사하지 않습니다.** 붙여넣은 바이트만 `$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>`에 내용 해시로 저장되며 중복은 하나로 합쳐집니다 — 같은 스크린샷을 열 번 붙여넣어도 파일은 하나입니다.

### 비전이 없는 모델

모든 모델이 보는 것은 아닙니다. 지금 모델이 못 볼 때도 턴이 실패하지는 않고, 모델이 대응할 수 있는 표시로 도착합니다.

```text
[image attached: screenshot.png] (this model cannot see images; ask the user to
describe it, or read the file from disk with a tool)
```

Anthropic과 Gemini 모델은 전부 비전 가능으로 봅니다. OpenAI 호환 벤더는 모델 이름으로 판단하며, 모르는 이름 — 로컬 GGUF, 지난주에 나온 모델 — 은 텍스트 전용으로 가정합니다. 요청이 실패하는 것보다 기능이 한 단계 낮아지는 편이 낫기 때문입니다. 내 로컬 모델은 이미지를 보는데 snowpea가 아니라고 한다면, 모델 id와 함께 이슈로 알려 주세요.

## 음성

음성은 그 일을 해 줄 무언가가 기계에 있을 때 동작합니다. 필수인 것은 하나도 없고, 없는 것은 조용히 넘어가지 않고 이유와 함께 보고됩니다.

```bash
snowpea setup audio
```

이 섹션은 두 가지를 묻습니다 — 어떻게 들을지, 어떻게 말할지 — 그리고 이 기계에 실제로 설치된 백엔드가 무엇인지 보여 줍니다.

### 여러 화면에서 동시에 설치할 때

`audio.install`은 대상 키별(single-flight)로 한 번만 실행됩니다. 대상 키는 엔진 설치면 `engine`,
보이스 설치면 `engine+voice`입니다. 다른 창이나 CLI가 같은 대상을 다시 요청하면 데몬은 두 번째
요청을 시작하지 않고 RPC 코드 `install_running`으로 거절하며, 현재 실행 중인 설치 메타데이터
(engine, 선택적 voice, 최신 progress 스냅샷)를 함께 보냅니다. 클라이언트는 새 설치를 시작하지
말고 이미 실행 중인 작업에 붙어야 합니다.

### 음성 → 텍스트

| 제공자 | 필요한 것 | 비고 |
|---|---|---|
| `local-whisper` | `PATH` 위의 `whisper` 또는 `faster-whisper` | 기계 밖으로 아무것도 나가지 않습니다 |
| `openai` | 이미 설정한 OpenAI API 키 | `whisper-1` 또는 `gpt-4o-transcribe` |
| `command` | `{path}`가 들어간 명령 템플릿 | stdout이 전사 결과 |
| `auto` (기본) | — | 로컬 whisper → OpenAI → 명령 순 |
| `off` | — | 듣지 않음 |

`auto`가 로컬 CLI를 먼저 고르는 것은 의도한 것입니다. 전사는 내 방의 소리가 기계 밖으로 나갈 수 있는 유일한 경로입니다.

### 텍스트 → 음성

| 제공자 | 필요한 것 | 비고 |
|---|---|---|
| `studio` | snowpea-studio MCP 서버 | 운영 중이라면 가장 좋은 목소리 |
| `openai` | OpenAI API 키 | `tts-1` 또는 `gpt-4o-mini-tts` |
| `edge-tts` | `PATH` 위의 `edge-tts` | 뉴럴 보이스, 네트워크 사용 |
| `piper` | `PATH` 위의 `piper` | 로컬 뉴럴 보이스 |
| `say` | macOS | 기본 탑재 |
| `espeak-ng` | `PATH` 위의 `espeak-ng` | 작고, 기계음이고, 어디에나 있음 |
| `powershell` | Windows | SAPI, 기본 탑재 |
| `command` | `{text}`와 `{out}`이 들어간 템플릿 | 오디오 파일을 씁니다 |
| `auto` (기본) | — | studio → OpenAI → 설치된 로컬 백엔드 순 |
| `off` | — | 말하지 않음 |

### 녹음

마이크 녹음에는 `rec`(sox), `arecord`(alsa-utils), `ffmpeg` 중 하나가 필요합니다. 재생에는 `afplay`, `paplay`, `aplay`, `ffplay`, `mpv` 중 하나가 필요합니다. 둘 다 없는 기계에서도 터미널 UI는 오디오 파일을 건네줄 수 있고, 재생은 직접 하면 됩니다.

### 에이전트도 음성을 씁니다

툴 두 개가 있어서 에이전트가 스스로 할 수 있습니다.

| 툴 | 하는 일 |
|---|---|
| `transcribe_audio` | 오디오 파일을 읽어 무슨 말인지 돌려줍니다 |
| `text_to_speech` | 말을 만들어 오디오 파일로 돌려줍니다. `play: true`면 이 기계에서 소리를 냅니다 |

백엔드가 없으면 둘 다 비활성이고, `snowpea tools list`가 그렇게 보여 줍니다.

```bash
snowpea tools list
```

## 설정

위의 모든 것은 `$SNOWPEA_HOME/settings.json`의 `audio` 아래에 있고, 데몬은 재시작 없이 수정을 반영합니다.

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

`autoSpeak`은 모든 답변을 소리 내어 읽습니다. 답변이 끝나는 순간 데몬이 음성을 만들고, 이 기계에 플레이어가 있으면 재생하며, 어느 쪽이든 파일 경로를 담은 `audio.spoken` 세션 이벤트를 보냅니다 — 다른 기계에 있는 클라이언트가 직접 재생할 수 있도록. 말하기가 턴에 영향을 주는 일은 없습니다. 백엔드가 없거나 플레이어가 망가져 있으면 로그 한 줄과 조용한 답변이지, 실패한 답변이 아닙니다. `player`와 `recorder`는 먼저 발견된 것 대신 특정 도구를 강제합니다.

## 아무 일도 일어나지 않을 때

데몬에게 무엇을 할 수 있는지 물어보세요. 꺼져 있는 기능에는 전부 꺼진 이유가 붙어 옵니다 — 어떤 패키지를 깔아야 하는지, 어떤 키를 넣어야 하는지.

```bash
snowpea tools list --json
```

터미널 UI가 시작할 때, 그리고 설정이 바뀔 때마다 다시 읽는 것도 같은 보고서입니다. `/voice` 나 `/tts` 가 백엔드가 없다고 말한다면 그 문장은 추측이 아니라 이 보고서의 것이고, 백엔드를 설치하는 것만으로 충분합니다 — 재시작은 필요 없습니다.
