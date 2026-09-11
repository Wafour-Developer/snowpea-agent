# 설치

snowpea는 런타임 두 개가 필요합니다: Python 3.11+([uv](https://docs.astral.sh/uv/)가 관리)와 Node 20+. 없으면 설치 스크립트가 둘 다 마련해 줍니다. 터미널 UI는 Python wheel 안에 미리 번들되어 있어서, 내 컴퓨터에서 `npm install`을 돌릴 일은 없습니다.

## 한 줄 설치

**macOS, Linux, WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
```

**Windows (PowerShell)**

```powershell
iwr https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex
```

그다음 확인합니다.

```bash
snowpea --version
```

이 스크립트는 멱등적입니다 — 다시 돌리면 그 자리에서 업그레이드됩니다. 공식 설치 프로그램으로 uv를 설치하고, `node --version`이 20 이상을 보고하는지 확인하고, `snowpea` 명령을 uv tool로 설치한 뒤, `~/.local/bin`이 아직 `PATH`에 없으면 셸 프로필에 추가합니다. Windows에서는 데이터 디렉터리가 `~/.snowpea` 대신 `%LOCALAPPDATA%\snowpea`입니다.

아무것도 건드리지 않고 무엇을 할지만 보려면 먼저 내려받아 `--dry-run`을 붙이세요.

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh -o install.sh
sh install.sh --dry-run
```

## 수동 설치

스크립트를 셸로 곧장 흘려보내고 싶지 않거나, 한 줄 설치가 어느 단계에서 실패해서 직접 그 단계를 하고 싶다면:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, if missing
uv tool install snowpea-agent
snowpea --version
```

터미널 UI를 쓰려면 Node 20+가 `PATH`에 있어야 합니다. 헤드리스 실행(`snowpea -c`)과 모든 `snowpea <subcommand>`는 Node 없이도 동작합니다. Node가 필요한 것은 TUI뿐입니다.

## 체크아웃에서 직접

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync
npm ci
npm run build
uv run snowpea --version
```

`npm run build`는 `tui/dist/snowpea-tui.js`를 만들고, `snowpea`는 번들된 패키지보다 이 파일을 먼저 찾습니다. UI를 작업하는 동안은 `SNOWPEA_TUI_ENTRY`가 직접 만든 엔트리 파일을 가리키게 해서 번들을 아예 건너뛸 수 있습니다.

## 업그레이드

```bash
uv tool upgrade snowpea-agent
snowpea daemon stop
snowpea --version
```

업그레이드 후에는 데몬을 멈추세요. 돌고 있는 데몬은 옛 코드를 메모리에 그대로 들고 있고, 다음에 붙는 클라이언트는 이미 설치된 것과 더 이상 맞지 않는 프로토콜 버전으로 협상하게 됩니다.

## 제거

```bash
snowpea daemon stop
uv tool uninstall snowpea-agent
```

여기까지는 데이터를 남깁니다. 데이터까지 지우려면 `$SNOWPEA_HOME`(`~/.snowpea`, Windows에서는 `%LOCALAPPDATA%\snowpea`)을 삭제하세요. 데몬을 서비스로 등록해 두었다면 먼저 등록을 해제합니다.

```bash
snowpea service uninstall
```

## 뭔가 잘못됐을 때

**설치 직후 `snowpea: command not found`.** 이 셸에서는 `~/.local/bin`이 아직 `PATH`에 없습니다. 새 터미널을 열거나 셸 프로필을 다시 불러오세요. 설치 스크립트는 그 줄을 프로필에 추가할 뿐, 지금 서 있는 셸 자체를 바꾸지는 못합니다.

**Node가 없거나 너무 낮습니다.** TUI가 뜨지 않습니다. 패키지 매니저나 [nodejs.org](https://nodejs.org)에서 Node 20+를 설치한 뒤 `snowpea`를 다시 실행하세요. 그동안 나머지는 계속 동작합니다.

```bash
snowpea -c "hello" --json
```

**데몬이 시작되지 않습니다.** `$SNOWPEA_HOME/logs/daemon.log`를 보고, 오래된 프로세스가 기록된 포트를 붙잡고 있지 않은지 확인하세요.

```bash
snowpea daemon status
snowpea daemon stop
snowpea daemon start
```

`daemon status`는 `$SNOWPEA_HOME/daemon.json`을 읽고 그 pid가 살아 있는지 확인합니다. pid가 죽은 오래된 파일은 지워도 안전합니다.

**회사 프록시 또는 오프라인 머신.** `uv tool install`은 PyPI가 필요합니다. 설치 스크립트를 돌리기 전에 `HTTPS_PROXY`를 설정하거나, 직접 들고 온 wheel로 설치하세요: `uv tool install ./snowpea_agent-0.1.0-py3-none-any.whl`.

## 다음

[설정](setup.md) — 벤더를 고르고 키를 마련합니다.
