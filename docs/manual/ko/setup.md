# 설정

`snowpea setup`은 `$SNOWPEA_HOME/settings.json`을 씁니다. 형태는 세 가지입니다.

```bash
snowpea setup            # quick: asks for one LLM vendor, defaults everything else
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

처음이라면 quick이 정답입니다. 뭘 바꾸고 싶은지 알게 되면 full을 한 번 훑어볼 가치가 있습니다. blank는 스크립트 설치와 CI를 위한 것입니다.

## 화면들

`--full`은 다섯 화면과 요약 화면을 거칩니다. 모든 화면은 **Skip — keep defaults**로 끝나고, 모든 화면에는 명령줄 플래그가 있어서 대화형으로 진행할 필요가 전혀 없습니다.

| 화면 | 선택 | 플래그 |
|---|---|---|
| Providers | LLM 벤더, 키, 모델 | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | 웹 검색 제공자 하나 | `--search-provider` |
| Browser | 브라우저 제공자 하나 | `--browser-provider` |
| Tools | 어떤 툴 카테고리를 켤지 | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token` |
| Done | 무엇이 쓰였는지 요약 | — |

목록은 키가 필요 없는 무료 항목이 먼저, 그다음 키가 필요하거나 자체 호스팅해야 하는 무료 항목, 마지막이 유료입니다. 각 목록의 기본값에는 별표가 붙어 있습니다. LLM 벤더를 빼면 기본 설정 어디에도 유료 계정이 필요한 곳은 없습니다 — 웹 검색과 브라우저 모두 키 없이 동작합니다.

## 벤더

v0.1에는 11종이 들어 있습니다.

| 벤더 id | 이름 | 어댑터 | 인증 |
|---|---|---|---|
| `anthropic` | Anthropic | native Messages API | API 키 |
| `openai` | OpenAI | OpenAI 호환 | API 키, 디바이스 코드 로그인 |
| `openrouter` | OpenRouter | OpenAI 호환 | API 키, OAuth PKCE 로그인 |
| `gemini` | Google Gemini | native | API 키 |
| `xai` | xAI Grok | OpenAI 호환 | API 키 |
| `glm` | Zhipu GLM | OpenAI 호환 | API 키 |
| `minimax` | MiniMax | OpenAI 호환 | API 키 |
| `kimi` | Moonshot Kimi | OpenAI 호환 | API 키 |
| `deepseek` | DeepSeek | OpenAI 호환 | API 키 |
| `qwen` | Qwen | OpenAI 호환 | API 키 |
| `local` | 로컬 OpenAI 호환 (vLLM, Ollama, LM Studio) | OpenAI 호환 | base URL, 키는 선택 |

```bash
snowpea provider list
snowpea provider list --json
```

`provider list`는 각 벤더의 인증 방식, 기본 모델, 설정 여부를 보여줍니다.

### 키 추가하기

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

환경 변수도 인식됩니다 — `OPENAI_API_KEY`나 `ANTHROPIC_API_KEY`가 이미 export되어 있으면 setup은 붙여넣으라고 묻는 대신 그 값을 제안합니다.

### 브라우저 로그인

두 벤더는 키를 붙여넣는 대신 브라우저로 로그인하는 방식을 지원합니다.

```bash
snowpea provider login openai        # device code: a code appears, you approve it in the browser
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai`도 같은 동작을 하는 별칭입니다. 그 외 벤더는 `login_unsupported`로 답하며 대신 돌릴 `--vendor`/`--key` 명령을 알려줍니다.

```bash
snowpea provider login deepseek
```

### 로컬 모델

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

`/v1/chat/completions`를 말하는 것이면 무엇이든 동작합니다 — vLLM, Ollama, LM Studio, llama.cpp의 서버까지. 다만 tool calling은 불러온 모델이 직접 지원해야 하며, 그렇지 않으면 에이전트가 말은 하지만 행동은 하지 못합니다.

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
| `exa_free`, `exa` | key required | `EXA_API_KEY` |
| `keenable_free`, `keenable` | key required | `KEENABLE_API_KEY` |
| `parallel_free`, `parallel` | key required | `PARALLEL_API_KEY` |
| `tavily` | free, key required | `TAVILY_API_KEY` |
| `xai_grok` | paid, key required | `XAI_API_KEY` |
| `searxng` | free, self-hosted | `SEARXNG_URL` |
| `firecrawl_selfhost` | free, self-hosted | `FIRECRAWL_URL` |

`snowpea setup search`에서 키가 필요한 제공자를 고르면 키를 (가려진 입력으로) 묻고 `search.credentials.<id>.api_key`에 저장합니다. 비워 두면 경고가 나옵니다. 키 없는 제공자는 검색에 답할 수 없기 때문입니다.

설정한 제공자가 동작하지 못하면 `web_search`는 다른 제공자로 넘어가되 그 사실을 숨기지 않습니다. 도구 출력은 `[search via ddgs — fallback from exa_free: exa_free needs an API key ($EXA_API_KEY)]`로 시작하고, 세션에는 `error{code:"search_provider_unavailable"}` 이벤트가 한 번 발생하며, 어시스턴트는 그 이유를 사용자에게 그대로 전하도록 지시받습니다.

실제로 어떤 제공자가 응답하는지 확인하려면:

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test`는 설정된 제공자로 실제 질의를 한 번 보내고, 응답한 제공자와 건너뛴 제공자들의 이유를 출력합니다. `snowpea tools list`는 `web_search`의 제공자를 함께 보여 주며, 설정한 id가 동작할 수 없으면 `exa_free → ddgs`처럼 표시합니다.

`web_extract`는 사설망·루프백·link-local 주소를 거부하고, 가져온 페이지를 `tools.max_output_chars`(기본 20000)까지 잘라냅니다.

## 브라우저 제공자

`local_chromium`이 기본값이며, 내 컴퓨터에서 Playwright로 headless Chromium을 돌립니다. 처음 실행할 때 브라우저 바이너리를 내려받으라고 물을 수 있습니다. 나머지 id들 — `camoufox`, `browser_use_local`, `browserbase`, `firecrawl_cloud` — 는 목록에서 보고 선택할 수 있도록 등록만 되어 있고, 설정되기 전까지는 `browser_provider_unavailable`로 답합니다.

```bash
snowpea setup --browser-provider local_chromium
```

## 툴 카테고리

카테고리는 툴 그룹 전체를 켜고 끕니다. 쉼표로 구분한 목록을 넘기고, 앞에 `-`를 붙이면 그 카테고리를 끕니다.

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

카테고리는 `file`, `terminal`, `git`, `web`, `browser`, `delegate`, `schedule`, `memory`, `media`이고, `.mcp.json` 서버가 제공하는 것은 무엇이든 `mcp`에 들어갑니다. 미디어 툴(`image_generate`, `video_generate`, `music_generate`, `text_to_speech`)은 항상 등록되어 있지만 자격 증명이 없으면 `inactive` 상태로 남습니다. 설정이 끝나면 재시작 없이 `active`로 바뀌고, 그 전에 호출하면 힌트와 함께 `tool_inactive`가 돌아옵니다.

## 게이트웨이

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

이 명령은 토큰을 `$SNOWPEA_HOME/credentials.json`(권한 `0600`)에 저장할 뿐, 그 이상은 하지 않습니다 — 봇을 에이전트나 세션에 바인딩하는 것은 별도 단계이며 [게이트웨이](gateway.md)에서 다룹니다.

## 디스크에 남는 것

`$SNOWPEA_HOME/settings.json`에는 `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, `agents.max_concurrent`(3), `team.max_conflict_retries`(2), `approvals.timeoutSec`(300), `memory.enabled`(true)가 담깁니다. 모드·allowlist·백엔드에 대한 프로젝트별 오버라이드는 `<project>/.snowpea/settings.json`에 있고 전역 파일보다 우선합니다. 비밀값은 `settings.json`에 절대 쓰이지 않고, 로그에도 남지 않습니다.

## 다음

[모드](modes.md) — 에이전트가 묻지 않고 얼마나 할 수 있는지 정합니다.
