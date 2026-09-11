# 기여하기

[English](CONTRIBUTING.md)

snowpea에 손대려는 분을 환영합니다. 이 문서는 첫 PR 전에 알아야 할 것들입니다. 작업 트리를 굴리는 법, 반드시 통과해야 하는 검사, 그리고 흔한 종류의 변경이 실제로 어디에 들어가는지입니다.

## 개발 환경

[uv](https://docs.astral.sh/uv/)로 관리하는 Python 3.11+와 Node 20+가 필요합니다.

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync          # resolve uv.lock into .venv
npm ci           # install the sdk and tui workspaces
npm run build    # build @snowpea/sdk and bundle the TUI
uv run snowpea --version
```

`npm run build`는 `tui/dist/snowpea-tui.js`를 만듭니다. `snowpea`는 wheel에 동봉된 번들을 먼저 찾고 그다음 이 파일을 찾으므로, 빌드한 체크아웃에서는 방금 컴파일한 UI가 뜹니다. UI를 계속 고치는 중이라면 `SNOWPEA_TUI_ENTRY`를 직접 만든 엔트리 파일로 지정해 번들 단계를 건너뛰세요.

내 설정이 섞이지 않도록 임시 홈을 쓰고 돌리는 편이 안전합니다.

```bash
SNOWPEA_HOME=/tmp/snowpea-dev uv run snowpea daemon status
```

## 검사

아래는 전부 CI에서 돕니다. 푸시 전에 로컬에서 돌려 보세요.

```bash
uv run pytest -q                                    # Python test suite
uv run ruff check .                                 # lint
uv run ruff format --check .                        # formatting
uv run mypy core                                    # type check
uv run python scripts/gen_protocol.py --check       # generated protocol is current
uv run python scripts/verify_vendor_integrity.py    # vendored code matches upstream + patch
npm test                                            # SDK contract tests and TUI tests
```

이 중 두 개는 설명이 필요합니다.

**`gen_protocol.py --check`** 는 `core/snowpea_core/server/protocol.py`에서 `docs/protocol.md`와 `sdk/src/protocol.ts`를 메모리에 다시 생성해 보고, 커밋된 파일이 낡았으면 diff와 함께 실패합니다. 둘 다 생성물입니다. 절대 손으로 고치지 말고 `protocol.py`를 고친 뒤 생성기를 돌리세요.

```bash
uv run python scripts/gen_protocol.py
```

**`verify_vendor_integrity.py`** 는 `core/snowpea_core/vendor/hermes/` 아래 모든 파일에 대해 "원본 + 커밋된 패치 == 작업본"이 바이트 단위로 성립하는지 검사합니다. 고정된 커밋의 hermes-agent 참조 클론이 필요합니다.

```bash
git clone https://github.com/NousResearch/hermes-agent /tmp/hermes-ref
HERMES_REF=/tmp/hermes-ref uv run python scripts/verify_vendor_integrity.py
```

테스트는 네트워크도, 실제 벤더 키도 쓰지 않습니다. 벤더 동작은 `tests/fixtures/providers/<vendor>/`의 녹화 픽스처에서 나오고, 여러 단계를 도는 에이전트 흐름은 스크립트 가짜 프로바이더에서 나옵니다.

```bash
SNOWPEA_PROVIDER=fake:tests/fixtures/providers/fake/basic.json uv run pytest -q
```

Docker와 SSH 백엔드 테스트는 인프라가 없으면 실패가 아니라 skip 합니다. 이걸 실패로 바꾸지 마세요.

## 브랜치와 커밋

`main`에서 브랜치를 따서 작업하고 PR을 엽니다. `main`은 보호되어 있습니다. 커밋 메시지는 기존 로그와 같이 Conventional Commits를 따르고 스코프에 마일스톤을 씁니다.

```
feat(m5): scheduler (cron/NL), daemon keepalive reasons, messenger gateway
fix(m2): web_extract rejects link-local addresses
docs(m8): manual pages for backends and headless runs
test: tool-contract checks tolerate activated stubs
```

PR 하나는 관심사 하나로 유지하세요. 생성 파일을 건드렸다면 어떤 생성기가 만든 것인지 적어 주세요.

## 이탈 기록(deviations log)

`docs/design/` 아래 계약 문서는 공유 문서입니다. 예전에는 여러 스토리가 동시에 append 하다가 서로의 섹션을 잃어버렸습니다. 그래서 각 작업은 자기 파일에 이탈을 기록합니다.

```
docs/design/deviations/US-0NN.md
```

자기 파일을 만들고, 남의 파일은 절대 고치지 마세요. 구현이 계약과 달라져야 했던 지점을 이유와 함께 적습니다. 중요한 것들은 리드가 마일스톤 커밋에서 계약 문서로 되접어 넣습니다. 이탈 기록은 변명이 아닙니다. 방금 읽은 문서와 코드가 왜 다른지를 다음 사람이 알아내는 유일한 경로입니다.

## 벤더 프리셋 추가

지금 11종이 들어 있고, 열두 번째를 붙이는 일은 새 코드 경로가 아니라 항목 하나여야 합니다.

1. `core/snowpea_core/providers/presets.py`에 `VendorPreset`을 추가합니다. id, label, 어댑터(`anthropic_native`, `gemini_native`, `openai_compat` 중 하나), `base_url`, `default_model`, `auth_methods`, `env_keys`, 그리고 quirk 플래그 `supports_parallel_tools`, `tool_call_style`, `stream_delta_shape`.
2. 플래그로 표현할 수 없는 방식으로 OpenAI 와이어 포맷에서 벗어난다면 `providers/normalize.py`를 확장합니다. 이 파일이 유일한 정규화 지점입니다. 어댑터 안에서 벤더로 분기하지 마세요.
3. "툴 한 번 호출하고 답하기" 시나리오의 골든 픽스처를 `tests/fixtures/providers/<vendor>/basic.json`에 자격증명을 지운 상태로 기록합니다. `uv run python scripts/scrub_fixtures.py --check tests/fixtures/providers`로 확인하세요.
4. 프로바이더 매트릭스 테스트와 `README.md`·`docs/manual/ko/setup.md`의 벤더 표에 추가합니다.

브라우저 로그인은 새 코드가 아니라 프리셋 선언입니다. `auth_methods`에 `device_code` 또는 `oauth_pkce`를 넣고 `providers/auth_web.py`의 플로우를 재사용하면 끝입니다.

## 검색 제공자 추가

1. `core/snowpea_core/tools/search_providers/`에 `SearchProvider` 프로토콜을 구현합니다. `SearchProviderMeta`에 `id`, `label`, `tier`(`free`/`paid`/`subscription`), `key`(`no key`/`key optional`/`key required`/`self-hosted`), 필요한 `env` 이름을 담습니다.
2. 레지스트리의 올바른 위치에 등록합니다. 레지스트리 순서가 곧 setup 화면의 순서이고, 그 순서는 테스트가 검증합니다. 무료·키 없음이 먼저, 그다음 무료·키 필요 또는 셀프호스트, 마지막이 유료입니다.
3. 답할 수 없는 제공자는 예외를 던지지 말고 `search_provider_unavailable`로 실패해야 합니다. 그래야 `web_search`가 무료 체인을 따라 폴백할 수 있습니다.
4. `docs/manual/ko/setup.md`의 검색 제공자 목록에 추가합니다.

브라우저 제공자도 `tools/browser_providers/`에서 같은 방식입니다.

## 툴 추가

1. 해당하는 `core/snowpea_core/tools/*.py` 모듈에 `Tool`을 만듭니다. `name`, `category`, `description`, `input_schema`, 그리고 `read`·`write`·`exec`·`network`·`send` 중 하나인 `permission` 태그가 필요합니다. 모드 매트릭스가 작동하는 대상이 이 태그이므로 정직하게 고르세요. 머신 밖으로 나가는 것은 전부 `network`이고, 사람에게 메시지를 보내는 것은 `send`입니다.
2. 파일시스템과 명령 실행은 전부 `ctx.backend`를 거칩니다. `open()`이나 `subprocess`를 직접 부르면 그 툴은 Docker·SSH 백엔드를 조용히 무시하게 됩니다.
3. `tools/registry.py`에 등록합니다. 자격증명이 필요한 툴은 `state="inactive"`로 등록되고, 자격증명이 생기면 재시작 없이 active로 바뀝니다.
4. `tests/test_tools_contract.py`에서 존재·카테고리·권한 태그를 단언하고, `docs/manual/ko/modes.md`의 툴 표에 추가합니다.

## 명령 추가

슬래시 명령은 TUI가 아니라 코어에 삽니다. 구현 하나가 터미널·헤드리스·예약 잡·메신저 메시지를 전부 감당해야 하기 때문입니다.

- **제어 흐름은 Python으로.** 반복하거나, 팬아웃하거나, 상태를 추적하거나, 파일을 쓰는 명령은 `core/snowpea_core/commands/`에 `Command`로 만들어 `CommandRegistry`에 등록합니다. `ralph.py`, `ultrawork.py`가 그 예입니다.
- **프롬프트가 본질이면 마크다운으로.** 사실상 긴 지시문인 명령은 `core/snowpea_core/builtin_skills/<name>/SKILL.md`에 두고, 사용자 스킬과 같은 로더로 읽힙니다. `deep-interview`, `ralplan`이 그 예입니다.

명령에는 `name`, `summary`, `args_schema`를 반드시 주세요. TUI는 `command.list`로 팔레트와 자동완성을 만들기 때문에, 모호한 summary를 가진 명령은 아무도 찾지 못하는 명령입니다. 그런 다음 실제로 뜨는지 확인합니다.

```bash
snowpea commands list --json
```

동작 테스트는 `tests/`에 명령당 최소 하나 둡니다.

## 문서

문서는 변경의 일부이지 나중에 할 일이 아닙니다. 플래그·명령·벤더를 추가했다면 영어 페이지와 한국어 페이지를 같이 고칩니다. 다른 언어는 번역이므로 한 릴리즈쯤 뒤처져도 괜찮습니다.

`README*.md`와 `docs/manual/**/*.md`의 펜스 코드 블록에 들어 있는 모든 `snowpea …` 호출은 CLI 자신의 `--help` 출력과 대조되고, 모든 상대 링크는 실제 파일로 해석되는지 확인됩니다.

```bash
uv run python scripts/check_docs_cli.py
uv run pytest tests/test_docs_cli.py -q
```

아직 들어오는 중인 명령이라 검사가 거부한다면, 검사를 약하게 만들지 말고 `scripts/check_docs_cli.py` 상단의 ALLOWLIST에 그 명령을 담당 스토리와 함께 적으세요.

## 라이선스와 vendored 코드

snowpea는 MIT이고, 기여도 같은 라이선스로 받습니다.

hermes-agent에서 복사한 코드는 `core/snowpea_core/vendor/hermes/` 아래에 살고, 원본 프로젝트·커밋·라이선스를 적은 출처 헤더를 유지하며, `docs/vendoring-map.md`에 기록됩니다. vendored 파일을 수정해도 되지만 그 diff는 패치로 커밋해야 합니다.

```bash
uv run python scripts/verify_vendor_integrity.py --add <upstream-path> <destination> --reason "why"
uv run python scripts/verify_vendor_integrity.py --update-patch <destination>
```

oh-my-claudecode에서 이식한 개념은 `docs/omc-porting-map.md`에 매핑되어 있고, 이식한 명령은 원본 대비 달라진 점을 자기 `SKILL.md` 하단에 적습니다. 두 프로젝트의 고지는 [NOTICE](../NOTICE)에 있습니다. MIT가 아니거나 호환되지 않는 프로젝트의 코드를 붙여 넣지 말고, 출처 헤더를 지우지 마세요.
