# 설정

`snowpea setup`은 `$SNOWPEA_HOME/settings.json`을 씁니다. 형태는 세 가지입니다.

```bash
snowpea setup            # quick: configure LLM models; defaults for other sections
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

처음이라면 quick이 정답입니다. 뭘 바꾸고 싶은지 알게 되면 full을 한 번 훑어볼 가치가 있습니다. blank는 스크립트 설치와 CI를 위한 것입니다.

## 화면들

`--full`은 다섯 화면과 요약 화면을 거칩니다. 모든 화면은 선택이 있으면 **Done — keep X**, 없으면 **Skip — decide later**로 끝나고, 모든 화면에는 명령줄 플래그가 있어서 대화형으로 진행할 필요가 전혀 없습니다.

| 화면 | 선택 | 플래그 |
|---|---|---|
| Providers | LLM 벤더, 키, 모델 | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | 웹 검색 제공자 하나 | `--search-provider` |
| Browser | 브라우저 제공자 하나 | `--browser-provider` |
| Tools | 어떤 툴 카테고리를 켤지 | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token` |
| Done | 무엇이 쓰였는지 요약 | — |

목록은 키가 필요 없는 무료 항목이 먼저, 그다음 키가 필요하거나 자체 호스팅해야 하는 무료 항목, 마지막이 유료입니다. 각 목록의 기본값에는 별표가 붙어 있습니다. LLM 벤더를 빼면 기본 설정 어디에도 유료 계정이 필요한 곳은 없습니다 — 웹 검색과 브라우저 모두 키 없이 동작합니다.

## 모델 목록은 어디서 오는가

모델을 보여주는 모든 곳 — 설정 마법사, `snowpea provider models`, `provider.models` RPC, TUI `/model` 선택창 — 은 하나의 함수를 씁니다. 이 함수는 네 단계를 순서대로 시도하고, 어느 단계가 답했는지도 함께 알려줍니다.

| 단계 | 출처 | 언제 답하나 |
| --- | --- | --- |
| 1 | **live** — 벤더의 실제 엔드포인트 | 자격 증명으로 벤더에 닿을 수 있을 때 |
| 2 | **settings** — `providers.<벤더>.models`, OAuth 계정이면 `.oauth_models` | 실시간 조회가 실패했거나 목록을 직접 고정해 둔 경우 |
| 3 | **cache** — 마지막으로 성공한 목록, `$SNOWPEA_HOME/cache/models-<벤더>-<인증>.json` | 벤더에 닿을 수 없고 고정해 둔 목록도 없을 때 |
| 4 | **curated** — 이 빌드의 목록에 공개 [models.dev](https://models.dev) 카탈로그를 합친 것 | 마지막 수단이며, 목록을 공개하지 않는 백엔드에서는 이것이 정상 응답 |

실시간 엔드포인트는 벤더뿐 아니라 **로그인 방식**에 따라서도 다릅니다. OpenAI 호환 벤더는 `/v1/models`, Gemini API 키는 `/v1beta/models`, Anthropic은 `/v1/models`, OpenAI 호환 목록이 없는 로컬 서버는 Ollama의 `/api/tags`를 씁니다. ChatGPT 구독 계정은 Codex 백엔드의 계정별 카탈로그(`chatgpt.com/backend-api/codex/models`)를 조회하며, 이는 Codex CLI가 보여 주는 목록과 같습니다. 반면 Google 로그인은 Code Assist 백엔드로 연결되는데 이 백엔드는 모델 목록을 전혀 공개하지 않으므로 항상 선언된(curated) 목록을 보여 줍니다 — 기능이 떨어져서가 아니라 그것이 정확한 답이기 때문입니다.

목록을 직접 고정하려면 — 아직 어디에도 공개되지 않은 모델을 미리 쓸 수 있는 계정이거나, 목록을 엉터리로 내려주는 서버인 경우 — 다음처럼 적습니다.

```json
{
  "providers": {
    "local": {"base_url": "http://localhost:8000/v1", "models": ["Qwen/Qwen3-32B"]},
    "openai": {"auth_method": "chatgpt", "oauth_models": ["gpt-5.1-codex"]}
  }
}
```

`models`는 모든 계정에 적용되고, `oauth_models`는 해당 제공자를 OAuth로 로그인했을 때만 적용됩니다. 덕분에 한 블록 안에서 Codex 카탈로그만 고정하고 API 키로 볼 목록은 건드리지 않을 수 있습니다. 둘 다 다음 조회부터 바로 반영되며 재시작이 필요 없습니다. `SNOWPEA_MODELS_DEV=0`으로 두면 curated 단계에서도 네트워크를 쓰지 않습니다.

## 벤더 기본 모델은 어디서 오는가

`snowpea setup`의 벤더 목록은 각 벤더가 실제로 세션을 시작할 모델을 `default: glm-5.3 (from models.dev)` 형태로 보여 줍니다. 이 id는 화면을 그릴 때 결정되며 빌드에 박혀 있지 않습니다. 몇 달 전에 설치한 릴리스라도 벤더가 지금 제공하는 모델을 안내한다는 뜻입니다. 우선순위는 세 단계입니다. 먼저 이 컴퓨터에 자격 증명이 있는 벤더는 **계정의 실제 목록**을 씁니다. 그다음이 공개 [models.dev](https://models.dev) 카탈로그가 그 벤더에 대해 싣고 있는 가장 최신 대화형 모델인데, 같은 세대라면 저가형 대신 대표 모델을 고르고 preview·deprecated id는 건너뜁니다. 마지막이 이 릴리스에 내장된 문자열입니다. 각 행은 어느 단계가 답했는지도 함께 표시하므로, 대체값이 계정의 실제 답처럼 보이는 일은 없습니다. 실시간 조회는 2초로 제한되고 모든 벤더를 동시에 조회하므로 응답이 없는 벤더는 기다리지 않고 건너뜁니다. `SNOWPEA_MODELS_DEV=0`은 여기서도 중간 단계를 네트워크 없이 동작하게 합니다. 같은 값이 `setup.catalog`의 `defaultModel`, `defaultModelSource` 필드로 IDE에도 전달됩니다.

## 여러 모델과 에이전트별 모델

`snowpea setup providers`에서 모델을 여러 개 등록하고 기본 모델을 선택할 수 있습니다. 같은 제공자의 다른 모델도 각각 등록할 수 있습니다. 에이전트별 할당에서는 기본 내장 역할과 커스텀 에이전트에 등록한 모델을 지정하거나, 할당을 해제하여 기본 모델을 사용하게 합니다.

설정 파일의 모델 프로필은 제공자와 모델 ID를 묶습니다. API 키와 base URL은 기존 `providers.<제공자>` 설정을 공유하므로 프로필마다 키를 복사할 필요가 없습니다. 다음은 구조 예시이며 모델 ID는 실제 사용할 값으로 바꾸세요.

```json
{
  "models": {
    "default": "daily",
    "profiles": {
      "daily": {"provider": "openai", "model": "your-default-model-id"},
      "reasoning": {"provider": "anthropic", "model": "your-reasoning-model-id"}
    }
  },
  "agents": {
    "max_concurrent": 3,
    "models": {"architect": "reasoning", "critic": "reasoning"}
  }
}
```

### 한 턴이 실제로 쓰는 모델

다섯 단계이고, 위에서부터 먼저 결정되는 쪽이 이깁니다. 끝까지 정해지지 않으면 벤더 기본값으로 넘어갑니다.

| # | 단계 | 설정 방법 |
|---|---|---|
| 1 | 이번 위임에만 적용하는 일회성 지정 | `delegate_task(model=…)`, `agent.spawn(model=…)` |
| 2 | 에이전트별 할당 | `snowpea model assign <agent> <profile>`, 또는 `agents.models` / 프로젝트 `models.agents` |
| 3 | 세션 고정 | `/model <profile>`, `session.setModel` |
| 4 | 프로젝트 기본값 | 프로젝트 `models.default` |
| 5 | 전역 기본값 | `models.default`, 또는 `snowpea model default <profile>` |

2단계는 에이전트별 할당을 먼저 보고, 없으면 에이전트 정의의 `model:` 필드를 봅니다. 참조로는 프로필 id, `vendor:model` 쌍, 벤더 이름을 쓸 수 있고 `inherit`은 "의견 없음, 다음 단계로"라는 뜻입니다.

새 일반 세션은 4~5단계에서 시작합니다. 설정을 고쳐도 기존 세션의 모델은 자동으로 바뀌지 않으며, 직접 고정해 둔 세션은 그 고정을 유지합니다 — 고정값은 세션과 함께 저장되어 재시작 후에도 남습니다. 모델 프로필을 하나도 설정하지 않은 기존 설치는 종전 동작을 유지합니다.

### 프로젝트별 모델

저장소마다 `<workdir>/.snowpea/settings.json`에 자기 `models` 블록을 둘 수 있습니다. 전역과 같은 세 키에 `agents`가 더 있고, 각각 키 단위로 전역 위에 덮이므로 다른 것만 적으면 됩니다.

```json
{
  "models": {
    "default": "reasoning",
    "agents": {"executor": "daily"},
    "profiles": {"local": {"provider": "ollama", "model": "your-local-model-id"}}
  }
}
```

셸에서는:

```bash
snowpea model profiles                       # 병합된 목록. 각 줄에 global/project 표시
snowpea model default reasoning --project    # 이 저장소의 기본값
snowpea model assign executor daily --project
snowpea model assign executor                # id를 빼면 할당 해제
```

### 답변 언어

`agent.replyLanguage`는 답변이 어떤 언어로 오는지를 정합니다. 기본값 `"auto"`는 사용자가 쓴 언어를 그대로 따르고, `"ko"`나 `"ja"` 같은 태그를 넣으면 무엇을 쓰든 그 언어로 고정됩니다.

```json
{"agent": {"replyLanguage": "ko"}}
```

위임에도 그대로 적용됩니다. 서브에이전트는 이 대화를 볼 수 없으므로, `delegate_task`가 모든 브리프 끝에 출력 언어를 알려 주는 짧은 영어 한 줄을 붙입니다 — 설정이 언어를 지정했으면 그 언어, `auto`면 사용자의 마지막 메시지에서 판별한 언어입니다(한글 → 한국어, 가나 → 일본어, 한자 → 중국어, 키릴 → 러시아어, 그 외 → 영어). 브리프 자체는 모델이 가장 정확하게 읽는 영어로 두어도 되고, 돌아오는 결과는 사용자의 언어입니다. 자식의 보고서를 직접 읽는 일은 없습니다 — 메인 에이전트가 무엇을 찾았는지 사용자의 언어로, 자기 말로 정리해 전달합니다.

터미널 UI의 표현도 같은 설정을 따릅니다. 한국어 세션에서는 `Read 3 files`가 아니라 `파일 3개 읽음`으로 보입니다. 한국어·일본어·중국어는 각자의 표현을 쓰고, 그 밖의 언어에서는 기존 영어 표기를 유지합니다.

### 프로필 삭제

`settings.set`은 병합이라 삭제를 표현할 수 없습니다. 그래서 `null`이 키를 지웁니다:

```json
{"models": {"profiles": {"daily": null}}}
```

프로필을 지우기 전에 `models.default`를 다른 곳으로 옮겨 주세요. 기본값이 없는 프로필을 가리키는 문서는 설정 검증이 거부합니다.

## 벤더

v0.1에는 11종이 들어 있습니다.

| 벤더 id | 이름 | 어댑터 | 인증 |
|---|---|---|---|
| `anthropic` | Anthropic | native Messages API | API 키 |
| `openai` | OpenAI | OpenAI 호환 | API 키, 디바이스 코드 로그인 |
| `openrouter` | OpenRouter | OpenAI 호환 | API 키, OAuth PKCE 로그인 |
| `gemini` | Google Gemini | native | API 키, Google OAuth (`gcloud` ADC), access token 직접 입력 |
| `xai` | xAI Grok | OpenAI 호환 | API 키 |
| `glm` | Zhipu GLM | OpenAI 호환 | API 키 |
| `minimax` | MiniMax | OpenAI 호환 | API 키 |
| `kimi` | Moonshot Kimi | OpenAI 호환 | API 키 |
| `deepseek` | DeepSeek | OpenAI 호환 | API 키 |
| `qwen` | Qwen | OpenAI 호환 | API 키 |
| `local` | Local / OpenAI-compatible servers (vLLM, Ollama, LM Studio) | OpenAI 호환 | base URL, 키는 선택 |
| *직접 지은 이름* | `"preset": "local"`로 선언하는 추가 OpenAI 호환 서버 (개수 제한 없음) | OpenAI 호환 | base URL, 키는 선택 |

```bash
snowpea provider list
snowpea provider list --json
```

`provider list`는 각 벤더의 인증 방식, 기본 모델, 설정 여부를 보여줍니다. `--json`에는 벤더별 `authStatus`도 담깁니다: `active`, `expired`(만료된 OAuth 세션), `unconfigured`.


### 키 추가하기

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

환경 변수도 인식됩니다 — `OPENAI_API_KEY`나 `ANTHROPIC_API_KEY`가 이미 export되어 있으면 setup은 붙여넣으라고 묻는 대신 그 값을 제안합니다.

### 브라우저 로그인

세 벤더는 키를 붙여넣는 대신 브라우저로 로그인할 수 있습니다.

```bash
snowpea provider login openai        # ChatGPT: 동의 화면, localhost:1455 콜백
snowpea provider login gemini        # Google: 동의 화면, 비어 있는 localhost 포트로 콜백
snowpea provider login openrouter    # OAuth PKCE: 로컬 콜백이 코드를 받음
```

`snowpea setup --login openai`도 같은 동작을 하는 별칭입니다. 그 외 벤더는 `login_unsupported`로 답하며 대신 돌릴 `--vendor`/`--key` 명령을 알려줍니다.

```bash
snowpea provider login deepseek
```

**ChatGPT·Google 로그인은 API 키가 아닙니다.** 구독 계정으로 로그인하는 것이라 두 벤더의 API 키 엔드포인트는 이 토큰을 거부합니다. 그래서 snowpea는 해당 대화를 각 벤더의 공식 CLI가 쓰는 백엔드 — OpenAI는 `chatgpt.com`의 Codex API, Gemini는 Code Assist API — 로 보냅니다. 전환은 자동이며, 눈에 보이는 차이는 모델 목록이 그 백엔드가 제공하는 집합(`gpt-5-codex`, `gpt-5`, … / `gemini-2.5-pro`, `gemini-2.5-flash`)으로 바뀐다는 점입니다. API 키 사용자에게는 달라지는 것이 없습니다.

**브라우저를 쓸 수 없는 환경** — SSH 접속이거나 디스플레이가 없는 리눅스 세션 — 에서는 브라우저 단계를 건너뛰고 헤드리스 방식으로 자동 전환합니다. OpenAI는 device code, Gemini는 `gcloud auth application-default login`입니다. 어디서든 강제하려면 `--device-code`를 쓰거나 `SNOWPEA_HEADLESS_LOGIN=1`을 내보내세요.

```bash
snowpea provider login openai --device-code
snowpea provider login gemini --token        # 또는 OAuth 액세스 토큰 붙여넣기
```

`--token` 뒤의 값은 생략해야 셸 기록에 남지 않습니다. 붙여넣은 토큰은 저장 전에 인증 요청 한 번으로 확인하며, 실패해도 거부가 아니라 경고입니다(오프라인 등 토큰과 무관한 이유로도 실패할 수 있기 때문입니다).

**로그인은 만료되고, snowpea가 알아서 갱신합니다.** 리프레시 토큰을 액세스 토큰과 함께 저장해 두고, 만료가 임박한 턴 직전에 한 번, 그래도 백엔드가 거부하면 한 번 더 갱신합니다. 사용자가 직접 할 일은 갱신 자체가 실패했을 때뿐이고, 그때는 오류 문구가 그대로 알려 줍니다.

```
your ChatGPT login expired and could not be renewed — run `snowpea provider login openai` to sign in again
```

그 전까지는 `snowpea provider list`와 설치 화면에서 해당 벤더가 `active`가 아니라 `expired`로 표시됩니다.

**문제 해결**

| 표시되는 내용 | 의미 |
|---|---|
| `device authorization failed (HTTP 403)` | 일부 네트워크·계정은 device-code 요청을 거부합니다. 브라우저 로그인이나 API 키를 쓰세요. |
| `port 1455 is already in use` | OpenAI는 `http://localhost:1455/auth/callback`만 허용하므로 포트를 바꿀 수 없습니다. 그 포트를 잡고 있는 다른 로그인(다른 Codex나 snowpea)을 닫거나 `--device-code`를 쓰세요. |
| 브라우저는 열리는데 아무 일도 없음 | 콜백이 도착하지 않은 것입니다. 브라우저가 *이* 컴퓨터에서 열렸는지 확인하고, SSH라면 `--device-code`를 쓰세요. |
| `the callback state did not match` | 응답한 페이지가 snowpea가 시작한 로그인이 아닙니다. 로그인을 다시 실행하세요. |
| `gemini OAuth login needs the Google Cloud CLI` | `gcloud` ADC 방식에만 필요합니다. 브라우저 로그인에는 필요 없습니다. |

설치 마법사는 종료하지 않고 벤더가 보낸 오류 문구를 출력한 뒤 인증 방법을 다시 묻습니다. 로그인 실패로 설치가 끝나는 일은 없습니다.

### 로컬 모델

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

`/v1/chat/completions`를 말하는 것이면 무엇이든 동작합니다 — vLLM, Ollama, LM Studio, llama.cpp의 서버까지. 다만 tool calling은 불러온 모델이 직접 지원해야 하며, 그렇지 않으면 에이전트가 말은 하지만 행동은 하지 못합니다.

### 로컬 서버 여러 대

`local` 항목 하나는 서버 한 대입니다. vLLM 장비와 Ollama 노트북처럼 여러 대를
쓰려면 `providers` 아래에 각각 이름을 주면 됩니다. `"preset": "local"`이 붙은
블록은 키가 곧 이름인 로컬 OpenAI 호환 서버입니다.

```json
{
  "providers": {
    "local": {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
    "hon2": {
      "preset": "local",
      "label": "hon2 vLLM",
      "variant": "vllm",
      "base_url": "http://hon2.example.com:8000/v1",
      "model": "flash-next-mtp"
    }
  },
  "models": {
    "profiles": {"hon2:flash-next-mtp": {"provider": "hon2", "model": "flash-next-mtp"}},
    "default": "hon2:flash-next-mtp"
  }
}
```

이름이 곧 벤더 id이므로 `hon2:flash-next-mtp`는 모델을 가리킬 수 있는 모든
자리에서 동작합니다 — `models.default`, 프로젝트별 모델, `agents.models`,
세션의 `/model`. 이름은 소문자로 시작하는 영문자에 숫자·`-`·`_`를 쓸 수 있고,
기본 벤더 id와 겹칠 수 없습니다.

`variant`는 `vllm`, `ollama`, `lmstudio`, `generic` 중 하나입니다. 마법사가
제안하는 URL을 고르고, `/v1/models`가 없는 서버에 Ollama의 `/api/tags` 조회를
켭니다. `api_key`는 선택이고, `label`은 목록에 보이는 이름, `context_window`는
서버가 알려주지 않는 컨텍스트 길이를 직접 지정합니다.

명령줄에서는 이렇게 씁니다.

```bash
snowpea provider add-local hon2 --url http://hon2.example.com:8000/v1 --type vllm
snowpea provider add-local hon2 --url http://hon2.example.com:8000/v1 --key sk-local --model flash-next-mtp
snowpea provider models hon2
snowpea provider remove hon2
```

`remove`는 블록과 그 서버를 가리키던 모델 프로필, 그 프로필을 쓰던 에이전트
배정까지 함께 지웁니다.

`snowpea setup`에서는 **Local / OpenAI-compatible servers** 행이 이미 설정된
서버들을 보여 주고, 그 아래에 "Add another server…"와 "Remove a server…"가
있습니다. 새로 추가하면 이름·서버 종류·URL·선택 키를 물은 뒤 `/v1/models`를
조회해 기본 모델을 고르게 합니다.

`providers.local`만 있는 기존 설정은 그대로 동작합니다. 그 항목은 `preset`
표시가 없어도 로컬 서버로 취급합니다.

### 도구 호출 한도

`agent.max_tool_rounds`(기본 200)는 한 턴이 물어보지 않고 만들 수 있는 도구
호출 횟수입니다. 한도에 닿으면 에이전트가 선택창을 띄워 "계속"(같은 횟수만큼
더) 또는 "여기서 멈춤"을 묻습니다. 긴 구현 턴을 위한 체크포인트이지 작업량
제한이 아닙니다.

무엇을 고르든 **턴은 먼저 보고를 씁니다**: 도구를 끈 채 모델을 한 번 더 불러
무엇을 했고, 무엇을 찾았고, 무엇이 남았고, 어떤 파일을 고쳤는지 적게 합니다.
그래서 한도에서 끝나는 턴도 조용히 사라지지 않고 보고를 남긴 뒤
`turn.done{reason:"budget"}`으로 끝납니다(에러가 아닙니다). 물어볼 사람이 없는
헤드리스(`-c`) 턴과 위임된 자식은 보고하고 멈추며, 보고 있는 세션에서는 보고
다음에 질문이 뜹니다.

`agents.toolRounds`는 위임된 자식의 예산입니다. 숫자 하나로 전부에 적용하거나,
`agents.models`처럼 에이전트 이름을 키로 하는 매핑(`default`가 기본값)으로 줄 수
있습니다.

```json
{
  "agents": { "toolRounds": { "default": 80, "explorer": 150 } }
}
```

에이전트 정의의 `tool_rounds:` 프론트매터가 둘보다 우선합니다. 아무것도 설정하지
않으면 자식은 `agent.max_tool_rounds`를 쓰되 80회 밑으로는 내려가지 않습니다 —
자식은 위임한 세션보다 훨씬 많이 읽고, 밖에서 보이는 것은 마지막 보고뿐이기
때문입니다.

### 로컬 서버의 이미지 인식

모델에 이미지를 보낼 수 있는지는 강한 규칙부터 네 단계로 정합니다.

1. `providers.<vendor>.vision`, 또는 그 서버의 특정 모델에 대한
   `providers.<vendor>.models.<model>.vision`;
2. 캐시에 이미 있는 models.dev 카드;
3. 모델 이름 (`gpt-4o`, `claude`, `qwen2.5-vl`, `llava`, `-vl`로 끝나는 이름 등
   내장 목록과 대조);
4. 로컬·이름 붙인 OpenAI 호환 서버에 한해 **한 번 시도**. 이미지를 그대로
   보내고, 서버가 요청을 거부하면 그 사실을 기억하고 로그에 한 번 남긴 뒤,
   같은 턴을 텍스트 설명으로 즉시 다시 보내 답은 오게 합니다.

4단계가 이름을 모르는 로컬 모델을 쓸 수 있게 만드는 지점입니다. 목록에 없는
이름(`flash-next-mtp` 같은)으로 올린 비전 모델은 예전에는 이미지가 전부
"(this model cannot see images)"로 바뀌었습니다. 이제는 그냥 이미지를 보냅니다.
정말 텍스트 전용인 서버라면 모델당 거부되는 요청 한 번이 비용이고, 그 결과는
`<SNOWPEA_HOME>/cache/vision.json`에 일주일간 기억되므로 다음 프롬프트나 다음
재시작에서 다시 치르지 않습니다.

호스팅 벤더는 이렇게 떠보지 않습니다. 카탈로그를 알 수 있고, 거기서 거부되는
요청은 아무것도 얻지 못하는 과금이기 때문입니다.

떠보게 두지 않고 직접 정하려면 이렇게 씁니다.

```json
{
  "providers": {
    "hon2": {
      "preset": "local",
      "base_url": "http://hon2:8000/v1",
      "vision": true,
      "models": {"flash-next-mtp": {"vision": true}, "qwen3-8b": {"vision": false}}
    }
  }
}
```

모델별 규칙이 서버별 규칙을 이깁니다. 카탈로그만 고정할 때는 `models`를 예전처럼
id 목록으로 둬도 됩니다. 객체 형태는 모델마다 무언가를 말하고 싶을 때 씁니다.

명령줄에서는 이렇게 씁니다.

```bash
snowpea provider add-local hon2 --url http://hon2:8000/v1 --vision
snowpea provider add-local hon2 --url http://hon2:8000/v1 --no-vision
snowpea provider models hon2
```

`provider models`는 아는 모델에 표시를 답니다. 이미지를 받는 모델에는 👁,
받지 않는 모델에는 `(text only)`, 아직 아무도 확인하지 않은 모델에는 아무것도
붙이지 않습니다. 설치 마법사의 모델 목록에도 같은 눈 표시가 보입니다.

호스팅 벤더에도 덮어쓰기는 동작합니다. 앞단 프록시가 이미지를 떼어 내는 경우를
알려 주는 방법입니다.

### 추론 강도(effort)

`thinking`이 스위치라면 effort는 다이얼입니다. `low`, `medium`, `high`, `max`
한 가지 척도가 모든 벤더에 닿고, 각 어댑터가 그 벤더가 실제로 받는 필드로
옮깁니다.

| 벤더 | 전송 필드 | low | medium | high | max |
|---|---|---|---|---|---|
| `openai` (API 키) | `reasoning_effort` | `low` | `medium` | `high` | `high` |
| `openai` (ChatGPT 로그인, Codex 백엔드) | `reasoning.effort` | `low` | `medium` | `high` | `xhigh` |
| `anthropic` | `thinking.budget_tokens` | 2 048 | 8 192 | 32 768 | 65 536 |
| `gemini` | `thinkingConfig.thinkingBudget` | 2 048 | 8 192 | 32 768 | 65 536 |
| `openrouter`, `xai` | `reasoning_effort` | `low` | `medium` | `high` | `high` |
| `glm`, `minimax`, `kimi`, `deepseek`, `qwen` | — | 아무것도 보내지 않음 | | | |
| `local`과 이름 붙인 서버 | `reasoning_effort` (선택) | `low` | `medium` | `high` | `high` |

토큰 예산은 그 호출 `max_tokens`의 3/4까지만 잡습니다. 세게 생각하는 턴도 답을
쓸 자리는 남겨 두기 위해서입니다. `thinking: "off"`는 모든 단계보다 우선합니다.
thinking을 끈 사용자에게 effort 설정이 숨은 추론을 되돌려 주지는 않습니다.

OpenAI 필드는 그것을 받는 모델(`o` 계열, `gpt-5*`, `codex*`)에만 보냅니다. 그럼에도
모델이 거부하면(`HTTP 400: Unsupported parameter`) 그 호출만 필드 없이 한 번
다시 보내고, 그 모델에는 데몬이 사는 동안 다시 보내지 않습니다.

자체 호스팅 서버는 선택 사항입니다. vLLM은 불러온 모델의 채팅 템플릿에 따라
`reasoning_effort`를 무시하거나 거부하기 때문입니다.

```json
{
  "providers": {
    "hon2": {"preset": "local", "base_url": "http://hon2:8000/v1", "effort_param": true}
  }
}
```

설정은 약한 규칙부터 이렇습니다.

```json
{
  "agent": {
    "effort": "medium",
    "effortBy": {"openai": "high", "anthropic:claude-opus-4-1": "max"}
  }
}
```

`agent.effort`가 전체 기본값입니다. `agent.effortBy`는 벤더별(`"openai"`) 또는
모델별(`"openai:o3"`)로 그것을 덮고, 모델 규칙이 벤더 규칙을 이깁니다. 둘보다
위에 세션 핀이 있습니다.

```text
/effort            지금 적용 중인 값과 그것을 정한 규칙을 보여 줍니다
/effort high       이 세션에 고정합니다
/effort auto       고정을 풉니다
```

핀은 세션과 함께 저장되므로 대화를 이어 열어도 고른 단계가 유지됩니다.
`snowpea model profiles`는 프로필마다 적용될 effort를 보여 주고, TUI는 모델 옆에
(`⚙ high`) 그려 줍니다. `/model` 선택 화면에는 단계를 차례로 바꾸는 행이 있습니다.

예전 설정의 `providers.openai.reasoning_effort`는 Codex 백엔드용으로 계속
읽습니다. 업그레이드했다고 생각의 깊이가 달라지지는 않습니다. 다만 이제 아무도
그 키를 쓰지 않습니다. `/effort`는 통합된 키에 씁니다.

### 출력 한도와 thinking

추론 모델(Qwen3, DeepSeek-R1, GLM의 thinking 계열)은 답을 쓰기 *전에* 생각을
먼저 흘려보내고, 그 생각도 답과 같은 `max_tokens`에서 깎입니다. 한도를 너무
작게 주면 모델이 예산을 전부 생각에 쓰고 아무것도 답하지 못합니다.

```json
{
  "agent": { "max_tokens": 16384, "thinking": "auto" },
  "providers": {
    "local": { "max_tokens": 32768, "thinking": "off" }
  }
}
```

`agent.max_tokens`(16384)는 모델 호출 한 번이 낼 수 있는 토큰이고, 벤더 블록이
그 벤더에 한해 이를 덮어씁니다. 어느 쪽이든 모델이 실제로 받아들이는 값으로
잘리므로, `gpt-4`의 8192 같은 상한은 보내고 거절당하는 대신 지켜집니다.

`agent.thinking`은 `on`, `off`, `auto`입니다. 기본값 `auto`는 사람이 보고 있는
세션에서는 생각하고, 위임된 세션에서는 조용히 갑니다 — 거기서는 보고서가 곧
출력이고, 숨은 추론은 예산만 먹습니다. `off`는
`chat_template_kwargs: {"enable_thinking": false}`를 보내며, Qwen 계열
서버(vLLM, SGLang)가 이를 따르고 나머지는 무시합니다. 에이전트 정의는
프런트매터의 `thinking: on`으로 둘 다 덮어쓸 수 있습니다.

데몬은 턴이 한도에서 멈췄을 때도 반응합니다. 본문이 비어 있는데 추론 토큰만
쓴 답은 thinking을 끄고 한 번 다시 묻고(스위치가 없는 벤더면 예산을 두 배로),
문장 중간에 잘린 답은 최대 두 번까지 이어받아 조각을 붙입니다. TUI가 어느
쪽이었는지 알려 줍니다 — `response hit the output limit; continued`, 그래도
모자랐다면 `… and is incomplete`. 끝내 잘린 `delegate_task` 요약은
`[truncated at max_tokens after 2 continuations]`로 끝납니다.

**빈 응답 / 잘린 응답**

| 보이는 것 | 의미 |
|---|---|
| 모델이 아무것도 답하지 않음 | 예산을 전부 숨은 추론에 썼습니다. `agent.max_tokens`를 올리거나 해당 벤더의 `thinking`을 `off`로 두세요. |
| 답이 문장 중간에 끊김 | 이 작업에 예산이 부족합니다. 데몬이 두 번 이어받고 그래도 모자라면 그렇게 말합니다. `agent.max_tokens`를 올리세요. |
| `HTTP 400 … max_tokens` | 모델 상한이 예산보다 낮은데 snowpea 표에 없는 모델입니다. `providers.<vendor>.max_tokens`에 공개된 상한을 적으세요. |

### 어떤 벤더가 쓰이는가

우선순위 순서는: `SNOWPEA_PROVIDER` 환경 변수, 그다음 명령줄의 `--provider`나 세션의 `provider` 인자, 그다음 settings의 `providers.default`, 마지막으로 설정된 첫 번째 벤더입니다.

```bash
snowpea -c "summarize README.md" --provider deepseek
```

## 검색 제공자

`web_search`와 `web_extract`는 제공자 레지스트리 위에 있습니다. 기본값 `ddgs`는 키도, 계정도 필요 없습니다.

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider exa --search-key sk-your-exa-key
snowpea setup search
```

아무것도 필요 없는 제공자는 `ddgs` 하나뿐입니다. `*_free` id는 키 없이 쓸 수 있는 엔드포인트가 아니라 키가 필요한 서비스의 무료 **요금제**입니다. 키 없이 부르면 Exa는 `402`, Parallel과 Keenable은 `401`, Tavily도 `401`을 돌려줍니다. 그래서 `key required`로 표시되며, 키가 설정되기 전까지는 검색에 응답하지 못합니다.

| id | 태그 | 필요한 것 |
| --- | --- | --- |
| `ddgs` | free, no key | 없음 |
| `firecrawl` | paid, key optional | 없음. 클라우드 검색 엔드포인트는 키 없이도 응답하지만 호출 제한이 있습니다 |
| `brave_free` | free, key required | `BRAVE_API_KEY` |
| `exa_free` | 키 없음 | `https://mcp.exa.ai/mcp`의 익명·속도 제한 호스팅 MCP |
| `exa` | 키 필요 | `EXA_API_KEY` (직접 REST API) |
| `keenable_free`, `keenable` | key required | `KEENABLE_API_KEY` |
| `parallel_free`, `parallel` | key required | `PARALLEL_API_KEY` |
| `tavily` | free, key required | `TAVILY_API_KEY` |
| `xai_grok` | paid, key required | `XAI_API_KEY` |
| `searxng` | free, self-hosted | `SEARXNG_URL` |
| `firecrawl_selfhost` | free, self-hosted | `FIRECRAWL_URL` |

`snowpea setup search`에서 키가 필요한 제공자를 고르면 키를 (가려진 입력으로) 묻고 `search.credentials.<id>.api_key`에 저장합니다. 비워 두면 경고가 나옵니다. 키 없는 제공자는 검색에 답할 수 없기 때문입니다.

`exa_free`는 Exa 공식 호스팅 MCP의 `web_search_exa`와 `web_fetch_exa`를 익명으로 사용하므로 API 키를 묻지 않습니다. 단, 익명 사용량 제한은 적용됩니다. 유료 계정 한도와 직접 API를 사용하려면 `exa`를 선택하고 `EXA_API_KEY`를 입력합니다.

설정한 제공자가 동작하지 못하면 `web_search`는 다른 제공자로 넘어가되 그 사실을 숨기지 않습니다. 세션에는 `error{code:"search_provider_unavailable"}` 이벤트가 한 번 발생하며, 어시스턴트는 그 이유를 사용자에게 그대로 전하도록 지시받습니다.

실제로 어떤 제공자가 응답하는지 확인하려면:

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test`는 설정된 제공자로 실제 질의를 한 번 보내고, 응답한 제공자와 건너뛴 제공자들의 이유를 출력합니다. `snowpea tools list`는 `web_search`의 제공자를 함께 보여 주며, 설정한 id가 동작할 수 없으면 `exa_free → ddgs`처럼 표시합니다.

`web_extract`는 사설망·루프백·link-local 주소를 거부하고, 가져온 페이지를 `tools.max_output_chars`(기본 20000)까지 잘라냅니다.

파일 작업과 긴 출력, 라운드 비용에 관여하는 `tools` 설정이 다섯 더 있습니다.

| 설정 | 기본값 | 하는 일 |
|---|---|---|
| `tools.readBeforeWrite` | `true` | 이번 세션에서 전체를 읽지 않은 파일에 `patch`·`write_file`을 하면 결과에 `note:`로 그 사실을 알립니다. 읽은 뒤 형제 서브에이전트가 쓴 파일은 `owned_by` 오류로 거부합니다. 새 파일 생성에는 안내가 붙지 않습니다. `false`로 두면 가드가 꺼집니다. |
| `tools.maxResultLines` | `400` | 이 줄 수를 넘으면 `shell`·`execute_code`·`grep`·`glob`·`list_dir` 결과는 앞뒤만 남기고, 가운데는 `$SNOWPEA_HOME/cache/tool-output/`으로 빠집니다. 결과에는 그것을 다시 읽을 offset·limit이 적힌 `read_file` 포인터가 남습니다. |
| `tools.repeatGuard` | `true` | 같은 답에 두 번 값을 치르지 않게 합니다. 이번 세션에서 이미 읽었고 그 뒤로 내용이 바뀌지 않은 `read_file`은 한 줄 요약으로 돌아오고, 그런 요약이 두 번 나간 뒤의 반복은 `repeat_blocked` 오류로 거부됩니다. 어떤 툴이든 같은 인자로 연달아 세 번 부르면 결과에 경고가 붙고 네 번째는 거부되며, `shell`·`grep`·`glob`·`list_dir`·MCP 호출의 출력이 직전과 같으면 요약으로 바뀝니다. 해당 경로에 쓰기가 일어나거나 사이에 다른 툴 호출이 끼면 해당 카운터는 초기화됩니다. `false`로 두면 전부 꺼집니다. |
| `tools.deferred` | `true` | 매 라운드마다 핵심 툴 스키마만 온전히 보내고 나머지는 그룹별 한 줄로 이름만 제공하며 필요 시 `tool_search`로 로드합니다. `false`로 두면 매 라운드 모든 툴 스키마를 보냅니다. |
| `tools.eager` | `[]` | 기본 설정과 무관하게 언제나 전체 스키마를 보낼 툴 이름 목록. |

`read_file`은 `offset`(1부터 세는 시작 줄)과 `limit`(줄 수)을 선택적으로 받습니다. 일부만 읽은 파일에 쓰면 결과에 그 사실이 `note:`로 붙습니다.

같은 호출이 계속 돌아올 때 — 최근 20번의 호출 안에서 같은 인자로 다섯 번 — 결과에 `loop suspected: …` 줄이 붙고 세션은 턴당 한 번 `loop.suspected` 이벤트를 냅니다. 표면은 이것으로 턴이 제자리를 돌고 있음을 보여 줄 수 있습니다.

오래된 툴 출력은 요청이 나가기 전에 줄어듭니다. 저장된 대화 기록은 건드리지 않습니다.

| 설정 | 기본값 | 하는 일 |
|---|---|---|
| `agent.pruneToolOutputs` | `true` | 최근 `agent.keepToolRounds`번의 툴 라운드보다 오래된 결과는 **나가는 요청에서만** `[earlier shell output pruned — N chars; re-run the tool if you need it again]`로 바뀝니다. 남은 구간에서도 2000자를 넘는 결과는 `…[trimmed]…`를 사이에 두고 앞뒤만 남습니다. `skill_view` 본문은 `skills.protectRecentViews`가 맡습니다. `false`로 두면 전부 그대로 보냅니다. |
| `agent.keepToolRounds` | `6` | 온전히 제공자에게 전달되는 툴 라운드 수. |

이어 열거나 내보내거나 압축한 세션에는 툴이 만든 바이트가 모두 남아 있습니다. 잘라내기는 모델에게 건네는 사본에서만 일어납니다. 반복 방지, 지연 로딩 툴, 출력 정리, 자식 컨텍스트 및 컨텍스트 파일 상한에 대한 자세한 내용은 [툴과 컨텍스트 예산](tools.md)을 참고하세요.

에이전트가 매 턴 읽는 스킬 색인은 `skills` 설정 세 개가 좌우합니다([플러그인과 스킬](plugins.md) 참고).

| 설정 | 기본값 | 하는 일 |
|---|---|---|
| `skills.indexInPrompt` | `true` | 설치된 모든 스킬을 출처별로 묶어 시스템 프롬프트에 싣습니다. 에이전트가 먼저 툴을 불러 탐색하지 않고도 `skill_view`로 바로 불러올 수 있습니다. `false`로 두면 목록에 컨텍스트를 쓰지 않습니다. |
| `skills.indexMaxEntries` | `60` | 색인이 이름을 나열하다 "… and K more — skill_list shows all"로 접는 개수. |
| `skills.protectRecentViews` | `2` | 압축이 그대로 두는 `skill_view` 결과의 턴 수. 그보다 오래된 5000자 이상 본문은 다시 불러오라는 `[SKILL_PRUNED: …]` 표시로 바뀝니다. |

## 브라우저 제공자

`local_chromium`이 기본값이며, 내 컴퓨터에서 Playwright로 headless Chromium을 돌립니다. 처음 실행할 때 브라우저 바이너리를 내려받으라고 물을 수 있습니다. 나머지 id들 — `camoufox`, `browser_use_local`, `browserbase`, `firecrawl_cloud` — 는 목록에서 보고 선택할 수 있도록 등록만 되어 있고, 설정되기 전까지는 `browser_provider_unavailable`로 답합니다.

```bash
snowpea setup --browser-provider local_chromium
snowpea setup --browser-provider firecrawl_cloud --browser-key fc-your-key
snowpea setup browser
```

**`key required` 태그가 붙은 제공자는 자격 증명을 물어봅니다.** `browserbase`나 `firecrawl_cloud`를 고르면 목록 바로 뒤에 키를 마스킹된 입력으로 묻고, 이미 저장된 값이 있으면 Enter로 유지합니다. Browserbase는 값이 둘 필요하므로 둘 다 묻습니다. API 키 다음에 프로젝트 id를 묻는데, 이것은 비밀이 아니라 식별자라서 마스킹하지 않습니다.

| id | 태그 | 필요한 것 |
| --- | --- | --- |
| `local_chromium` ★ | free · no key | Playwright의 Chromium. 처음 쓸 때 내려받습니다 |
| `camoufox` | free · no key | 아직 없음 |
| `browser_use_local` | free · no key | 아직 없음 |
| `browserbase` | paid · key required | `BROWSERBASE_API_KEY`와 `BROWSERBASE_PROJECT_ID` |
| `firecrawl_cloud` | paid · key required | `FIRECRAWL_API_KEY` |

자격 증명은 검색과 같은 모양으로 `browser.credentials.<id>`에 저장됩니다.

```json
{ "browser": { "provider": "browserbase",
               "credentials": { "browserbase": { "api_key": "...", "browserbase_project_id": "..." } } } }
```

환경 변수보다 설정이 우선입니다. 마법사에 방금 입력한 키가 export 한 변수를 이깁니다. 저장된 값도 없는데 빈 답을 주면 조용히 넘어가지 않고 말로 거절합니다. 제공자는 기록되고, 요약에 키가 없다고 적히며, 키가 생기기 전까지 브라우저 툴은 계속 거절합니다.

키를 입력하면 설정이 값싼 호출 하나로 확인합니다. Browserbase는 세션 목록, Firecrawl은 `HEAD` 요청이며 예산은 3초입니다. 실패는 **경고이지 차단이 아닙니다**. 오프라인 기계나 프록시, 혹은 제공자의 나쁜 하루 때문에 방금 붙여넣은 키를 잃어서는 안 되므로 어느 쪽이든 저장합니다.

## 툴 카테고리

카테고리는 툴 그룹 전체를 켜고 끕니다. 쉼표로 구분한 목록을 넘기고, 앞에 `-`를 붙이면 그 카테고리를 끕니다.

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

카테고리는 `file`, `terminal`, `git`, `web`, `browser`, `delegate`, `schedule`, `memory`, `media`이고, `.mcp.json` 서버가 제공하는 것은 무엇이든 `mcp`에 들어갑니다. 그 `.mcp.json` 서버를 추가·테스트·삭제하는 것은 `/mcp`와 `snowpea mcp`이며, [MCP 서버](plugins.md#mcp-서버)에서 다룹니다. 미디어 툴(`image_generate`, `video_generate`, `music_generate`, `text_to_speech`)은 항상 등록되어 있지만 자격 증명이 없으면 `inactive` 상태로 남습니다. 설정이 끝나면 재시작 없이 `active`로 바뀌고, 그 전에 호출하면 힌트와 함께 `tool_inactive`가 돌아옵니다.

## 음성 입력과 출력

목록은 둘이지만 결정은 하나입니다. 말을 걸 수 있는가, 그리고 대답을 소리로 해주는가. 설치 여부와 상관없이 모두 보여줍니다. "왜 piper를 못 쓰지"라는 질문의 답이 화면에 없어서가 아니라 화면에 있어야 하기 때문입니다.

음성은 방향마다 **두 가지 상태**뿐입니다. 아무것도 고정되지 않았으면 꺼진 것이고, 엔진 하나가 고정되었으면 그것만 씁니다. 여러 개를 차례로 시도하는 "자동"은 없앴습니다. 조용히 다섯 개를 시도하는 체인은 결국 침묵만 보고할 수 있었고, 그중 무엇을 시도했는지조차 알려줄 수 없었습니다.

- **미설정** — `audio.stt.provider` / `audio.tts.provider`가 없는 상태. `audio.capabilities`가 그 방향을 false로 보고하며 이유는 `no engine set — install or pick one in setup`입니다.
- **고정됨** — 엔진 id 하나. 설치되어 있지 않으면 `engine <id> is not installed`로 false를 보고합니다. 다른 엔진으로 슬쩍 바꾸지 않습니다.

예전 `settings.json`에 남아 있는 `"auto"`는 **미설정**으로 읽습니다. 즉 직접 고르기 전까지 음성은 꺼져 있습니다. 의도적입니다. 실수로가 아니라 일부러 켜게 됩니다.

**설치와 선택은 별개의 단계입니다.** `snowpea audio install <engine>`은 엔진을 기계에 올려놓을 뿐 설정을 바꾸지 않습니다. 고정하는 것은 고르는 행동입니다. 마법사는 둘 중 무엇이 남았는지 알려줍니다. *Not set*, *X is installed, not selected — pick it to use it*, 또는 *Pinned: X*.

권장 엔진 둘은 모두 로컬·CPU 전용입니다. 계정도 GPU도 없이 음성이 동작합니다.

**음성 입력** — 권장 기본값은 **SenseVoiceSmall**입니다.

| 항목 | 무엇인가 | 고르면 무슨 일이 일어나나 |
|---|---|---|
| `sherpa-onnx-sensevoice` ★ | SenseVoiceSmall. zh/en/ja/ko/yue, CPU에서 실시간의 약 17~20배, VAD를 함께 받아 긴 녹음을 스스로 잘라냅니다 | 설치합니다(패키지 + 약 230MB 모델). 그 다음 고르면 고정됩니다 |
| `sherpa-onnx-zipformer-ko` | 한국어 스트리밍 Zipformer INT8. CPU에서 실시간의 약 10~38배 | 설치합니다. 그 다음 고르면 고정됩니다 |
| `sherpa-onnx-zipformer-en` | 영어 스트리밍 Zipformer INT8 | 설치합니다. 그 다음 고르면 고정됩니다 |
| `local-whisper` | 이미 있을 수도 있는 whisper CLI | `faster-whisper`를 설치합니다. 그 다음 고르면 고정 |
| `openai` | 호스팅 전사 | 키를 가려진 입력으로 묻고 고정합니다 |
| `command` | 직접 만든 템플릿 | 템플릿을 묻고 검사한 뒤 자가 테스트하고 고정합니다 |

**음성 출력** — 권장 기본값은 **Supertonic**입니다.

| 항목 | 무엇인가 | 고르면 무슨 일이 일어나나 |
|---|---|---|
| `supertonic` ★ | Supertone의 온디바이스 신경망 TTS. 한국어·영어 포함 31개 언어, CPU에서 동작 | 설치합니다(ONNX 음성은 스스로 받아옴). 그 다음 고르면 고정 |
| `piper` | 로컬 신경망 음성 | 기본 음성과 함께 설치합니다. 그 다음 고르면 고정 |
| `edge-tts` | 마이크로소프트 신경망 음성 | 설치합니다. 그 다음 고르면 고정됩니다 |
| `espeak-ng` | 작고 기계적이며 어디에나 있음 | 플랫폼 명령을 보여주고 실행할지 물어봅니다 |
| `say` / `powershell` | macOS / Windows 기본 제공 | 고정합니다. 다른 플랫폼에서는 **목록에 없음** |
| `openai` | 호스팅 음성 | 키를 가려진 입력으로 묻고 고정합니다 |
| `command` | 직접 만든 템플릿 | 템플릿을 묻고 검사한 뒤 자가 테스트하고 고정합니다 |

**모든 행은 어딘가로 이어집니다.** 설치되지 않은 엔진을 고르면 침묵을 부르는 값을 저장하는 대신 그 자리에서 설치합니다. 시스템 패키지는 자기 명령을 보여주고 실행할지 물어봅니다. 사용자 명령은 입력받아 자리표시자를 검사하고 3초 자가 테스트를 거친 뒤 고정합니다. 여기서 절대 동작할 수 없는 엔진 — 리눅스의 macOS `say` — 은 아예 목록에 넣지 않습니다.

설치는 로그만이 아니라 **단계**를 보고합니다. `[download 3/6] 63% ▇▇▇▇▇▁▁▁ sherpa-onnx-sensevoice.tar.bz2` 아래에 로그 꼬리가 붙습니다.

음성 출력이 켜져 있으면 에이전트는 **여는 응답**도 말합니다. 첫 도구 호출 전에 쓰는 그 한 줄이라서, 말로 부탁한 뒤 1분간 조용한 일이 없습니다. `audio.tts.speakAck: false`로 끕니다.


음성 모델은 공식 sherpa-onnx 릴리스 자산에서 받아 `$SNOWPEA_HOME/models/sherpa-onnx/` 아래에 놓입니다. 중단된 다운로드는 이어받고, 완전히 풀린 뒤에야 설치된 것으로 칩니다. 취소된 다운로드가 엔진을 준비된 것처럼 보이게 하는 일은 없습니다.

`audio.stt.language`를 `ko` 같은 태그로 두면 SenseVoice에 기대할 언어를 알려주고 맞는 Zipformer를 고릅니다. 비워 두면 SenseVoice가 스스로 언어를 판별합니다.

**엔진 설치.** 사용자 영역 패키지인 셋은 데몬이 대신 설치합니다.

```bash
snowpea audio install sherpa-onnx-sensevoice
snowpea audio install supertonic
snowpea audio install faster-whisper
snowpea audio install piper
snowpea audio install edge-tts
```

명령줄 도구인 엔진(`piper`, `edge-tts`, `faster-whisper`)은 `uv`가 PATH에 있으면 `uv tool install`, 없으면 `pipx`, 그것도 없으면 `pip install --user` 순서로 설치하며 로그를 그대로 보여줍니다.

`sherpa-onnx`와 `supertonic`은 쓸 만한 명령줄이 없는 파이썬 라이브러리입니다. 그래서 snowpea가 직접 가진 인터프리터 **`$SNOWPEA_HOME/audio-runtime`** 안으로 들어갑니다. 이 디렉터리는 처음 쓸 때 `uv venv`나 `python -m venv`로 만들어지고, 엔진은 그 인터프리터의 자식 프로세스에서 문서화된 파이썬 API를 실행합니다. 시스템이나 사용자 site-packages에는 아무것도 쓰지 않으며, "설치됐는가"는 PATH가 아니라 그 디렉터리에 대한 질문이 됩니다. 처음부터 다시 하려면 디렉터리를 지우면 되고, 다음 설치가 다시 만듭니다. sherpa-onnx 항목은 패키지를 설치한 뒤 모델까지 내려받습니다.

`piper`를 설치하면 기본 음성 하나를 `$SNOWPEA_HOME/voices/`에 내려받아 `audio.tts.voice`에 기록합니다. 음성 파일이 없는 piper 바이너리는 아무 말도 못 하기 때문입니다. 끝나면 탐지를 다시 돌리므로 재시작 없이 바로 쓸 수 있습니다.

설정 마법사의 음성 화면 둘은 **할 일 중심**입니다. 행이 설정값이 아니라 지금 할 수 있는 동작입니다.

```text
Recommended: SenseVoiceSmall (CPU) — Install     ← 아무것도 설치되지 않았을 때만
Install Piper…
Choose a specific engine…
Skip — decide later
```

아무것도 없으면 권장 엔진이 맨 위에 오고 미리 선택되어 있습니다. 설치가 도움이 되는 유일한 동작이기 때문입니다. 엔진이 하나라도 생기면 권장 행은 사라지고, 더 정확한 문장이 그 자리를 대신합니다. **Automatic will use espeak-ng.**

자동(Automatic)은 여전히 설정이고 여전히 기본값입니다. 다만 더 이상 *행*이 아닙니다. 동작이 아니기 때문입니다. 설치된 게 없으면 지킬 수 없는 약속이고, 있으면 화면이 무엇을 쓸지 그냥 알려주면 됩니다.

`Choose a specific engine…`는 엔진을 직접 고정하는 하위 메뉴를 엽니다. 설치된 것부터, 나머지도 표시와 함께 모두 나열하고, 그 뒤에 Off, 사용자 명령, 계정이 필요한 것 순입니다. Esc는 질문을 포기하는 게 아니라 뒤로 가기이며, 고정한 뒤에는 그 행이 무엇이 고정됐는지 알려줍니다. Install 행은 설치를 실행하고 그 엔진을 바로 선택하므로, 마법사는 다음 질문으로 넘어갑니다.

`espeak-ng`, `say`, `powershell`은 시스템 패키지이고, 데몬은 사용자를 대신해 root로 패키지 관리자를 돌리지 않습니다. 대신 플랫폼에 맞는 명령을 알려줍니다.

```
$ snowpea audio install espeak-ng
could not install espeak-ng: sudo apt install espeak-ng
```

**snowpea-studio는 이제 음성 선택지가 아닙니다.** `text_to_speech` 미디어 도구는 설정된 studio MCP 서버로 그대로 넘어가고, 자동(Automatic)도 다른 게 하나도 없으면 여전히 studio로 떨어집니다. 다만 음성 목록에는 나오지 않고 체인의 맨 앞도 아닙니다. 한마디라도 하려면 MCP 서버 설정이 먼저 필요해서, 맨 앞에 두면 "자동"이 대부분의 기계에 없는 백엔드로 해석됐습니다.

### 음성(보이스)

엔진을 고르는 것은 결정의 절반입니다. 마법사는 그 다음에 **보이스 단계**를 보여줍니다. 언어마다 탭이 하나씩입니다. 한 엔진이 한국어와 영어에서 서로 다른 사람처럼 들릴 수 있고, 그래야 하기 때문입니다. 디스크에 없는 항목은 먼저 내려받고, Preview는 그 언어로 한 문장을 합성해 들려주며, Skip은 엔진의 기본값을 그대로 둡니다. 그것도 훌륭한 답입니다.

```json
{ "audio": { "tts": { "voices": { "ko": "F2", "en": "M1", "*": "M1" } } } }
```

답변은 그 언어의 항목, 없으면 `*`, 그것도 없으면 엔진 기본값으로 말합니다. 이 설정이 매핑이 되기 전에 쓰인 파일의 `voice: "M1"`은 `"*"`로 읽히며 그대로 동작합니다.

**Supertonic은 다국어 모델 하나입니다.** 프리셋 열 개(M1~M5, F1~F5)가 31개 언어 전부에서 동작하므로, 언어마다 다른 보이스를 골라도 추가 다운로드가 없습니다. **Piper는 반대입니다.** 보이스가 곧 다운로드이고, 보이스 파일이 없는 언어는 말할 수 없는 언어입니다. 그래서 그런 행은 그렇게 표시되고 고르면 설치합니다. `edge-tts`, `espeak-ng`, macOS `say`, Windows SAPI는 실행 시점에 직접 물어보고, 한국어·영어·설정된 답변 언어로 걸러 보여줍니다.

`audio.voices {engine}`은 같은 목록을 RPC로 돌려주고, `audio.install {engine, voice}`는 같은 단계 진행률로 하나를 내려받습니다. 보이스를 설치한다고 선택되지는 않습니다. 엔진 설치가 고정으로 이어지지 않는 것과 같은 이유입니다.

### 전사 언어

```json
{ "audio": { "stt": { "language": "auto" } } }
```

`auto`가 기본값이고, "언어 없음"이라는 뜻이 아닙니다. 아무도 강제하지 않았다는 뜻이라서, 스스로 판별하는 엔진은 판별하고, 그러지 못하는 엔진은 지금 답변하는 언어를 씁니다. BCP-47 태그를 주면 강제됩니다.

**SenseVoice**, whisper, OpenAI에게는 힌트입니다. **sherpa Zipformer에게는 모델을 고르는 값**입니다. 이 모델들은 단일 언어라서, 한국어 모델에 영어를 시키는 것은 다른 모델을 요구하는 것과 같습니다. 필요한 모델이 없으면 이유가 어느 것인지 말해줍니다. `sherpa-onnx-zipformer-en does not speak ko; install sherpa-onnx-zipformer-ko`. `audio.capabilities`는 `sttLanguage`와 `sttLanguageSource`(`setting`/`reply`/`detect`)를 보고하므로, 화면이 누가 정했는지 말해줄 수 있습니다.

## 게이트웨이

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

이 명령은 토큰을 `$SNOWPEA_HOME/credentials.json`(권한 `0600`)에 저장할 뿐, 그 이상은 하지 않습니다 — 봇을 에이전트나 세션에 바인딩하는 것은 별도 단계이며 [게이트웨이](gateway.md)에서 다룹니다.

## 프로젝트 지시 파일

프로젝트는 자기 규칙을 파일로 알려주고, 에이전트는 매 턴 그것을 읽습니다. 탐색 순서는 Hermes와 같고 **먼저 발견된 한 종류만** 읽습니다 — 두 가지 관례를 함께 쓰는 저장소가 값을 두 번 치르지 않게 하기 위함입니다.

1. `.snowpea/instructions.md` 또는 `SNOWPEA.md` — 가까운 것부터, git 루트까지 거슬러 올라가며.
2. `AGENTS.md` 체인 — git 루트에서 세션 디렉터리까지 내려오며, 디렉터리마다 `AGENTS.override.md`·`AGENTS.md`·`agents.md` 중 첫 번째. `.override.` 이름은 gitignore 하라고 있는 것으로, 커밋된 파일을 건드리지 않고 개인 지시를 옆에 둘 수 있습니다. 아래쪽에서 내용이 같으면 한 번만 읽습니다.
3. 세션 디렉터리의 `CLAUDE.md` 또는 `claude.md`.
4. 세션 디렉터리의 `.cursorrules`와 `.cursor/rules/*.mdc`.

`.git` 조상이 없으면 체인은 세션 디렉터리 하나뿐입니다. `/tmp`나 홈 디렉터리에 놓인 파일이 프롬프트 권위를 얻는 일은 없습니다.

크기. 파일 하나가 프롬프트에 들어가는 양은 `clamp(컨텍스트 윈도우 × 4 × 0.06, 20 000, 500 000)`자이며(32k 이하의 작은 윈도우에서는 하한이 8 000자로 내려갑니다), 전체 `# Project Context` 블록은 `clamp(컨텍스트 윈도우 × 4 × 0.10, 12 000, 120 000)`자로 제한됩니다. 총합 예산을 초과하면 루트 파일을 온전히 남기기 위해 깊은 파일부터 `…[truncated: N more chars; read <path> for the rest]` 마커와 함께 먼저 잘립니다. 개별 파일이 잘릴 때는 앞부분과 뒷부분을 남기고 그 사이에 어떤 파일을 `read_file` 하면 되는지 알려주는 표시가 들어가며, 블록 끝에 잘렸다는 사실이 문장으로도 적힙니다. `agent.contextFileMaxChars`와 `agent.contextFilesMaxChars`로 값을 고정하거나 `agent.ignoreContextFiles`로 전부 끌 수 있습니다.

중첩 파일. `/deepinit`은 디렉터리마다 `AGENTS.md`를 쓰는데 세션은 평생 저장소 루트에 앉아 있으므로 체인만으로는 그 파일들에 닿지 않습니다. 그래서 예산이 허락하는 만큼 미리 각자의 섹션으로 실립니다 — 그 프로젝트에서 새로 연 대화도, 이어받은 세션도, 같은 디렉터리의 서브에이전트도 처음부터 계층 전체를 가지고 있습니다. 탐색은 4단계까지, 최대 40개이며 `.git`·`node_modules`·`.venv`·`dist`·`build`·`__pycache__`와 숨김 디렉터리는 지나갑니다.

들어가지 못한 것은 이름만 알려줍니다.

```text
Nested instructions not loaded (read_file when you work there): src/AGENTS.md, test/AGENTS.md
```

그리고 그 디렉터리를 실제로 건드리는 첫 툴 결과에 가장 가까운 파일이 덧붙습니다 — 그 안의 파일을 읽거나 쓰거나 고칠 때, 목록을 볼 때, glob·grep할 때, 셸 명령이 `cd`로 들어갈 때입니다. 세션당 한 번이고, 이미 프롬프트에 인용된 파일에는 붙지 않습니다.

변경은 즉시 반영됩니다. 깊이에 상관없이 이 파일들을 쓰거나 고치면 캐시된 프롬프트가 버려지고, `/init`·`/deepinit`·`/skill create`가 끝날 때도 마찬가지입니다 — 바로 다음 턴이 방금 쓴 내용을 봅니다.

## 디스크에 남는 것

`$SNOWPEA_HOME/settings.json`에는 `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `tools.readBeforeWrite`(true), `tools.maxResultLines`(400), `tools.repeatGuard`(true), `tools.deferred`(true), `tools.eager`([]), `gateway`, `agents.max_concurrent`(3), `agents.childContext`("lean"), `team.max_conflict_retries`(2), `approvals.timeoutSec`(300), `agent.max_tokens`(16384), `agent.thinking`(`auto`), `agent.autoAttachImages`(true), `agent.contextFilesMaxChars`, `agent.pruneToolOutputs`(true), `agent.keepToolRounds`(6), `memory.enabled`(true), `memory.askScope`(true), `memory.digestEntries`(30), `memory.digestChars`(6000), `skills.indexInPrompt`(true), `skills.indexMaxEntries`(60), `skills.protectRecentViews`(2)가 담깁니다. 모드·allowlist·백엔드에 대한 프로젝트별 오버라이드는 `<project>/.snowpea/settings.json`에 있고 전역 파일보다 우선합니다. 비밀값은 `settings.json`에 절대 쓰이지 않고, 로그에도 남지 않습니다.

## 다음

[모드](modes.md) — 에이전트가 묻지 않고 얼마나 할 수 있는지 정합니다.
