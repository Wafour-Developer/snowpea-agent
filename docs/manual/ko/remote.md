# 원격 세션

`snowpea --remote`는 세션을 다른 머신에서 실행합니다. 에이전트, 툴, 프로젝트 파일, 메모리, 승인 큐가 모두 그 호스트에 있고, 이 머신에는 터미널만 있습니다. 로컬 세션이 어딘가로 실행을 "보내는" 것이 아니라 세션 자체가 원격입니다.

```
snowpea --remote me@build-box
snowpea --remote me@build-box:2222 --cwd /srv/app
```

`--cwd`는 **원격 호스트의** 경로입니다. 생략하면 원격 홈 디렉터리에서 시작합니다. `--mode`, `--inline`은 평소처럼 동작합니다.

## 동작 순서

1. `snowpea`가 `ssh`를 한 번 호출합니다. 비밀번호나 키 암호는 여기서 한 번만 묻고, 이후 단계는 그 로그인을 재사용합니다.
2. 호스트에 `snowpea`가 없으면 설치 전에 묻습니다: `Install snowpea 0.2.81 there?` `y`라고 답하면 이 머신과 같은 버전으로 고정된 프로젝트 설치기가 호스트에서 실행됩니다. Node는 설치하지 않고(터미널 UI는 이 머신에 있습니다) 셸 rc 파일도 고치지 않습니다. 답이 없거나 대화형 터미널이 아니면 아무것도 설치하지 않습니다.
3. 호스트의 데몬을 시작하거나(이미 떠 있으면 재사용) 그 데몬이 열어 둔 포트와 토큰을 읽습니다.
4. 그 데몬으로 `ssh -L` 터널을 열고 터널에 터미널 UI를 붙입니다. 연결이 끊기면 터널을 다시 열고 UI는 같은 데몬에 재접속합니다.

데몬은 호스트의 루프백 주소에서만 듣습니다. 호스트에 열리는 포트는 없고, 모든 통신은 ssh 연결을 지납니다.

원격 데몬에 붙어 있는 동안 상태줄에 `⇄ user@host`가 표시됩니다(`--docker`는 `⇄ docker:<이름>`). 로컬 세션에는 아무것도 표시되지 않습니다.

## 요구 사항

- 이 머신의 `ssh`, 그리고 ssh 설정이 이미 받아 주는 호스트 로그인(키, 에이전트, `~/.ssh/config` 별칭 모두 가능).
- 호스트: `sh`가 있는 Linux 또는 macOS. snowpea를 설치해야 한다면 `curl`도 필요합니다.
- 양쪽의 프로토콜 major가 같아야 합니다. minor가 다르면 경고만 나오고(더 오래된 쪽에 없는 기능은 쓸 수 없음), major가 다르면 업데이트하라는 안내와 함께 거부됩니다.

## 무엇이 어디에 있나

| | |
|---|---|
| 에이전트가 읽고 쓰는 파일, 셸 명령 | 호스트 |
| 세션, 메모리, 설정, 자격 증명, 승인 | 호스트의 `~/.snowpea` |
| 터미널, 키보드, 클립보드 | 이 머신 |

떠날 때 호스트의 데몬은 유휴 시간 제한(`daemon.idleTimeoutSec`, 기본 30분)까지 계속 돌아서 진행 중인 턴이 끝날 수 있습니다. 같은 호스트에 다시 `snowpea --remote`로 들어오면 세션이 그대로 있습니다. ([`remote up`](#프로그램에서-쓰기-snowpea-remote-up)으로 연결한 데몬은 더 짧게 붙잡습니다.)

## 프로그램에서 쓰기: `snowpea remote up`

자체 UI가 있는 앱(스노우피 브라우저)은 터미널 UI가 필요 없습니다. `remote up`은 `--remote`·`--docker`와 같은 준비 과정(호스트 키, 데몬, 터널, 준비 확인, 재접속)을 하고 그 결과를 JSON 줄로 알려 줍니다. node도 `snowpea` 스크립트도 없는 번들 `snowpea-core` 바이너리에도 들어 있습니다.

```
snowpea remote up me@build-box:2222 --cwd /srv/app --json
snowpea remote up --docker --docker-volume work --docker-keep --cwd ~/src/app --json
snowpea-core remote up me@build-box --json
```

`--json`은 필수입니다. `--hold-sec N`(기본 300, `0`이면 끔)은 "유휴 시간 제한"에서 설명합니다. 터미널에서는 아무것도 묻지 않습니다. ssh는 `BatchMode=yes`로 실행되므로(키와 `ssh-agent`만 가능, 비밀번호나 키 암호를 묻는 상황은 `auth` 오류) 설치기도 pty 없이 실행됩니다.

**stdout**에는 한 줄에 JSON 객체 하나만 즉시 flush해서 내보내며 다른 것은 없습니다. 진단 메시지는 stderr로 갑니다.

| 이벤트 | 필드 | 뜻 |
|---|---|---|
| `ready` | `port`, `token`, `version`, `protocol`, `machine`, `cwd` | 데몬에 `127.0.0.1:<port>`와 이 토큰으로 접속할 수 있습니다. `machine`은 데몬의 `{id, hostname, os, container}`이고(프로토콜 1.40.0 미만 데몬은 `null`), `cwd`는 `--cwd`, 없으면 원격 로그인 디렉터리입니다. |
| `reconnecting` | | 터널이나 데몬이 사라져서 다시 연결하는 중입니다. |
| `ready` | 위와 같음 | 재접속 뒤에 다시 나옵니다. 원격 데몬이 재시작했거나 토큰이 바뀌면 `port`와 `token`이 새 값일 수 있습니다. 로컬 포트는 비어 있으면 그대로 씁니다. |
| `ask` | `id`, `kind`, … | 사람에게 묻는 질문입니다. 아래를 보세요. |
| `error` | `code`, `message`, 선택 `reason` | 실행을 끝내는 실패입니다. 뒤이어 `closed`가 나오고 종료 코드는 1입니다. |
| `closed` | | 모든 것이 정리되었습니다. 마지막 줄입니다. |

토큰은 stdout의 `ready`에**만** 나옵니다. stderr, 로그, 명령줄, `error` 메시지에는 나오지 않습니다.

**stdin**으로는 답을 한 줄에 JSON 객체 하나씩 보냅니다. stdin이 닫히거나 SIGTERM·SIGINT(Windows는 Ctrl+Break)를 받으면 ssh 터널과 제어 마스터를 닫고, `--docker` 컨테이너를 멈추고(`--docker-keep`이 아니면 삭제), `closed`를 내보내고 0으로 종료합니다.

### 질문

```
{"event":"ask","id":"7f3a…","kind":"install","version":"0.2.82","target":"me@build-box"}
{"event":"ask","id":"c01b…","kind":"hostKey","fingerprint":"SHA256:…","keyType":"ssh-ed25519","host":"me@build-box"}
```

`{"id":"7f3a…","answer":true}` 또는 `false`로 답합니다. 기본값은 없습니다. 답이 오거나 stdin이 닫힐 때까지(정상 종료로 처리) 질문은 열려 있습니다.

- `install`: 호스트에 snowpea가 없습니다. `true`면 이 머신과 같은 버전으로 고정된 설치기를 호스트에서 실행합니다. `false`는 `installDeclined` 오류입니다.
- `hostKey`: 호스트가 `known_hosts`에 없습니다(어느 파일인지는 `ssh -G`로 압니다). snowpea가 `ssh-keyscan`으로 키를 읽어 **첫 접속 전에** 묻습니다. `true`면 키를 사용자의 `known_hosts`에 추가하고, ssh는 `StrictHostKeyChecking=yes`와 그 키 종류로 실행되어 다른 키에는 속지 않습니다. `false`는 `hostKey` 오류입니다. 키가 **바뀐** 호스트는 질문 없이 `hostKey` 오류입니다. `ProxyJump`·`ProxyCommand` 너머의 호스트는 스캔할 수 없어서 모르는 키를 ssh가 직접 거부합니다(`hostKey`).

### 오류 코드

| `code` | 언제 | 흔한 `reason` |
|---|---|---|
| `auth` | ssh가 거부됨(받아 주는 키가 없음. 암호가 걸린 키는 `ssh-agent`에 있어야 함) | |
| `hostKey` | 호스트 키를 거부했거나, 프록시 너머의 모르는 키이거나, 키가 바뀜 | `rejected` |
| `unreachable` | 호스트가 응답하지 않음(DNS, 연결 거부, 시간 초과, 경로 없음), 터널이 열리지 않음, 10분 동안 재접속 실패 | |
| `notInstalled` | 설치기가 실패했거나, 설치 뒤에도 호스트에서 snowpea를 찾지 못함 | |
| `installDeclined` | `install` 질문에 `false`로 답함 | |
| `versionMajor` | 호스트의 프로토콜 major가 이 클라이언트와 다름 | |
| `dockerMissing` | Docker가 없거나, 데몬이 꺼져 있거나, 이 사용자가 쓸 수 없거나, 플랫폼이 `--docker`를 지원하지 않음(Windows) | `notInstalled`, `notRunning`, `denied`, `unsupportedPlatform` |
| `dockerBuild` | 샌드박스 이미지를 빌드하지 못함(또는 잘못된 `SNOWPEA_SANDBOX_SOURCE`) | |
| `internal` | 그 밖의 모든 경우(잘못된 명령줄 포함) | `usage`, `sshMissing`, `daemonFailed`, `containerFailed`, `containerExited` |

잘못된 명령줄은 `reason: "usage"`인 `error`이고 종료 코드는 2입니다. `sshMissing`은 `ssh`가 `PATH`에 없다는 뜻입니다(Windows는 `System32\OpenSSH\ssh.exe`를 `PATH` 없이도 찾습니다).

### 재접속과 포기

터널 프로세스를 지켜보고, 몇 초마다 터널을 통해 데몬이 응답하는지 확인합니다. 터널이 죽었거나 확인이 세 번 실패하면 `reconnecting`을 내보냅니다. 그 뒤 호스트에 다시 물어보고(데몬이 새 토큰으로 재시작했을 수 있음) 터널을 다시 열어 `ready`를 내보냅니다. 재시도로 고쳐지지 않는 실패(`auth`, `hostKey`, `versionMajor`, `notInstalled`, `installDeclined`, `docker…`)는 `error`로 끝납니다. `unreachable`은 백오프하며 10분간 재시도하고 그다음에 `error`입니다. `--docker` 컨테이너가 멈추면 몰래 새로 만들지 않고 `error`/`internal`/`containerExited`로 끝납니다(`--docker-volume`이면 세션은 볼륨에 남아 있습니다).

### 유휴 시간 제한

기본적으로 할 일이 없는 데몬은 `daemon.idleTimeoutSec`(30분) 뒤에 종료합니다. `remote up`이 연결한 데몬은 짧은 네트워크 끊김은 견뎌야 하지만 사라진 클라이언트를 30분씩 기다려서는 안 됩니다. 그래서 `remote up`은 데몬에 `holdSec`(`--hold-sec`, 기본 300)을 담은 `system.hello`를 보냅니다. 그 뒤로 데몬은 마지막 연결이 끊긴 지 그 시간이 지나면 **세션이 아직 로드되어 있어도** 종료합니다. 세션은 디스크에 남고 `includeClosed`를 준 `session.list`로 여전히 찾을 수 있습니다. 진행 중인 턴, 예약 작업, 게이트웨이 바인딩, 이름 있는 에이전트, 연결된 `keepAlive` 클라이언트는 여전히 데몬을 붙잡습니다. `tui`, `desktop`, `ide` 클라이언트가 접속하면 홀드가 풀리고 일반 시간 제한이 다시 적용됩니다. 프로토콜 1.40.0 미만 데몬은 이 값을 무시하고 30분을 유지합니다.

### 원격 머신의 파일

`ready.machine`으로 앱은 어느 머신과 대화하는지 압니다. 프로토콜 1.40.0 이상(기능 `fs`)의 데몬은 세션에 대해 `fs.stat`, `fs.list`, `fs.glob`, `fs.read`, `fs.write`, `fs.hash`도 처리합니다. 대상은 세션의 작업 디렉터리, 워크스페이스, 대화의 첨부 폴더로 한정되고, snowpea 자신의 설정이나 자격 증명은 절대 다루지 않습니다. [프로토콜 문서](../../protocol.md#fsstat)와 [설계 노트](../../design/remote-up-and-fs.md)를 보세요.

## 한계

- 대화형 터미널 UI만 됩니다. `--remote`는 아직 `-c`나 하위 명령과 함께 쓸 수 없습니다.
- 첨부하거나 언급한 경로는 이 머신이 아니라 호스트에서 읽힙니다. 이 머신에만 있는 파일은 경로로 첨부할 수 없습니다.
- 마이크와 스피커는 데몬이 있는 머신의 것이라 음성 입력과 음성 응답은 쓸 수 없습니다.
- MCP 서버의 브라우저 로그인(OAuth)은 호스트의 루프백 주소에서 기다리므로 이 머신의 브라우저로 열 수 없습니다.
- 이 머신의 컨테이너는 원격이 아니라 [샌드박스 세션](sandbox.md)입니다.
