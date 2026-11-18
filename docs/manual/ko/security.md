# 보안 목록

**이 페이지가 약속하는 것과 약속하지 않는 것.** `auto` 모드에서 에이전트는 사용자 본인의 권한으로 프로그램을 실행합니다. snowpea 는 각 명령 줄을 실행하기 전에 읽고(그 줄에 직접 적힌 코드와 그 줄이 이름으로 가리키는 스크립트도 읽습니다), 보안 목록과 snowpea 자체 파일에 대한 규칙에 해당하는 것을 거절하거나 사용자에게 묻습니다. 그 프로그램이 다시 불러오거나 시작하는 것은 읽지 **않습니다**: 테스트, 빌드, git 훅, `PATH` 에서 찾은 프로그램, 라이브러리. 그렇게 실행되는 코드는 이 컴퓨터에서 사용자가 할 수 있는 모든 일을 할 수 있으며, snowpea 자체 명령을 실행하거나 snowpea 의 파일을 고치는 것도 포함됩니다. snowpea 가 실행 중인 동안에는 변조 감시가 신뢰한 프로젝트 기록과 자비스 위임 규칙의 사본을 직접 가지고 그 사본으로 판단하며, 다른 무언가가 파일을 바꾸면 되돌려 놓습니다. snowpea 가 시작한 프로그램이 snowpea 에게 사용자 본인처럼 행동하지 못하게 하려면 운영체제의 도움이 필요하며, snowpea 는 아직 그렇게 하지 않습니다(다음 계획된 단계입니다). **신뢰하지 않는 저장소에서는 accept 모드를 쓰세요.** 그 모드에서는 모든 명령과 쓰기를 사용자가 직접 승인합니다.

한 번 설정하면 모든 세션에 적용되는 두 가지 목록입니다. 자비스 모드든 아니든, 어떤 모드든, 누가 호출하든 똑같이 적용됩니다.

- **차단 명령**은 snowpea 가 보는 곳 어디서든 멈춥니다: 명령 줄, snowpea 가 읽는 코드, snowpea 가 읽는 스크립트. 자비스, 예약 작업, 기억된 답, "항상 허용" 규칙, auto 모드 어느 것도 통과시킬 수 없고, 사용자 본인만 한 번에 한 호출씩 허용할 수 있습니다(아래). 그 줄이 시작한 프로그램이 다시 그 명령을 실행하는 경우(테스트, 빌드 단계, git 훅)는 읽지 않습니다: [snowpea 가 읽지 않는 것](#snowpea-가-읽지-않는-것)을 보세요.
- **보호 폴더**는 snowpea 가 보는 변경의 대상이 되면 멈춥니다. 항목마다 수준이 있습니다: **변경 금지**(`write`, 기본값: 읽기는 허용) 또는 **접근 금지**(`all`: 읽기, 목록, 변경 모두 금지).

두 목록 모두 켜고 끌 수 있는 **프리셋**과 직접 입력하는 항목으로 이루어집니다.

## 차단된 호출은 사용자에게 한 번 묻습니다

차단된 호출은 저절로 실행되지 않지만, 사용자가 허용할 수 없는 일은 없습니다(0.2.94). snowpea 는 호출을 멈추고 이유를 적은 카드로 묻습니다: "차단 목록에 있음", "보호 폴더를 건드림", "snowpea 자체 설정을 건드림"(아래)과 걸린 규칙. 허용하면 작업이 이어지고, 거절하면 에이전트는 사용자가 거절했다고 읽습니다.

- 답은 **이 호출 하나**에만 적용됩니다. 세션 동안에도, 규칙으로도, 같은 줄이 다시 와도 기억하지 않습니다.
- 사용자 본인만 답할 수 있습니다: IDE, TUI, CLI, 웹 페이지, 또는 Snowpea 브라우저의 카드 — 0.4.0 이상, 이 컴퓨터에서(원격 링크가 아닌) 그 브라우저가 열었거나 맡은 대화에서, 브라우저 자체 확인 창에서 확인한 경우에만. 자비스, auto 모드, "항상 허용" 규칙, 위임, 커넥터는 답할 수 없습니다.
- 물어볼 사람이 없는 실행(예약 작업, 자비스 작업)은 전처럼 거절되고, 에이전트는 `blocked by the security list: …` 를 읽습니다.
- plan 모드는 여전히 쓰지 않습니다. 그것은 차단이 아니라 모드입니다.
- snowpea 의 보안 설정, 신뢰 기록, 켜고 끈 기록을 바꾸는 호출을 허용하면 무엇에 걸렸는지와 함께 `logs/security.jsonl` 과 `logs/approvals.jsonl` 에 남습니다.

**묻지 않고 절대 실행하지 않는 두 가지:** 에이전트가 데몬의 토큰이나 주소(`token`, `daemon.json`)를 **읽는 것**, 그리고 저장된 로그인(`credentials.json`, `mcp-oauth.json`, 아래에 적힌 다른 에이전트 CLI 의 로그인)을 **읽는 것**. 이것을 읽은 에이전트는 사용자의 권한으로 행동할 수 있습니다. 이 파일을 바꾸거나 지우는 것은 다른 차단과 똑같이 묻습니다.

## 설정하는 곳

- **snowpea IDE와 snowpea web**: 설정 → 보안.
- **터미널 UI**: 보안 화면(`/security`).
- 브라우저 호스트와 원격 웹 페이지는 목록을 읽을 수만 있고 바꿀 수 없습니다. 로컬 연결(IDE, TUI, CLI, 로컬 웹 페이지)의 사용자 본인만 바꿀 수 있습니다.

화면 뒤에서는 `$SNOWPEA_HOME/settings.json`의 두 설정입니다.

```json
{
  "security": {
    "blockedCommands": { "presets": ["essential"], "custom": ["git push --force"] },
    "protectedPaths": {
      "presets": ["essential"],
      "custom": [{ "path": "~/finance", "level": "all" }]
    }
  }
}
```

새로 설치했거나 이 기능 이전에 설치한 경우, 두 목록 모두 `essential` 프리셋이 켜진 상태로 시작합니다. 일부러 비운 목록은 비어 있는 채로 유지됩니다.

프로젝트는 `<project>/.snowpea/settings.json`의 `security`(같은 형태)에 자기 항목을 **추가**할 수 있습니다. 사용자 목록에서 무언가를 뺄 수는 없습니다.

## 프리셋

차단 명령:

| 프리셋 | 막는 것 |
|---|---|
| `essential`(기본으로 켜짐) | `/` 나 홈 폴더(또는 홈이나 프로젝트를 담은 폴더) 삭제, 디스크 포맷·덮어쓰기(`mkfs`, `dd of=/dev/…`, `wipefs`), `/` 나 홈에 대한 `chmod -R`/`chown -R`, 포크 폭탄, `kill -9 -1` |
| `mass-delete` | 어디서든 `rm -r`/`rm -rf`, `find -delete`, `git clean -fd`/`-fx` |
| `disk` | `dd`, `mkfs`, `fdisk`, `parted`, `wipefs`, 마운트와 언마운트 |
| `permissions` | `chmod -R`, `chown -R`, `chgrp -R`, `setfacl -R` |
| `power` | `shutdown`, `reboot`, `halt`, `poweroff`, `systemctl poweroff` 등 |
| `git-destructive` | 강제 푸시(`--force`, `-f`, `+브랜치`), 원격 삭제, `git reset --hard`, `filter-branch`, `git branch -D`, `git clean -f`, `git stash clear` |
| `remote-script` | 내려받은 것을 바로 실행: `curl … \| sh`, `bash <(curl …)`, `sh -c "$(curl …)"` |
| `package-global` | `sudo`, `doas`, 시스템 패키지 관리자의 설치·삭제(`apt install`, `brew install` 등), `npm -g`, 가상환경 밖의 `pip install` |
| `process-kill` | `pkill`, `killall`, `kill -9 -1` |
| `containers` | `docker system prune`(그리고 모든 `docker … prune`), `docker rm -f`, `docker volume rm`, `compose down -v`, `kubectl delete`, `helm uninstall` |
| `database` | 데이터베이스 클라이언트로 보내는 `DROP DATABASE/TABLE`, `TRUNCATE`, `dropdb`, redis `FLUSHALL` |
| `cloud` | `terraform destroy`, `aws s3 rm --recursive`, `aws … delete-*`, `gcloud … delete`, `az … delete` |

보호 폴더:

| 프리셋 | 보호하는 것 | 수준 |
|---|---|---|
| `essential`(기본으로 켜짐) | `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` 은 snowpea 가 읽는 호출로는 바꾸지 않고, `/` 와 홈 폴더 자체(그리고 홈 위의 모든 폴더)는 그런 호출로 삭제·이동·재귀 변경하지 않음. 로그인하거나 셸을 열 때 실행되는 것도 그런 호출로 바꾸지 않음: `~/.ssh`, 셸 시작 파일(`~/.bashrc`, `~/.bash_profile`, `~/.bash_login`, `~/.bash_logout`, `~/.profile`, `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, `~/.zlogin`, `~/.config/fish/config.fish`), `~/.config/autostart`, `~/.config/systemd/user`, `~/Library/LaunchAgents`, `/Library/LaunchAgents`, `/Library/LaunchDaemons` | 변경 금지 |
| `credentials` | `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.config/gh`, `~/.config/gcloud`, `~/.azure`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc`, 비밀번호 저장소, 키링 | 접근 금지 |
| `snowpea-home` | `$SNOWPEA_HOME`(snowpea 의 계획 파일은 계속 쓸 수 있음) | 변경 금지 |
| `other-projects` | snowpea 가 아는 모든 프로젝트, 세션이 작업 중인 프로젝트만 제외 | 변경 금지 |
| `mounts` | 세션 프로젝트 밖의 마운트된 데이터·네트워크 디스크(`/mnt/…`, `/media/…`, `/Volumes/…`). 홈이나 snowpea 폴더가 있는 디스크는 제외 | 변경 금지 |

## 직접 추가하는 차단 명령

한 줄에 패턴 하나를 입력합니다. 한 줄이 실행할 모든 명령과 대조합니다. `sudo`, `env`, `nice`, `timeout`, `xargs`, `find -exec` 를 거쳐도, `sh -c '…'`/`bash -c '…'` 안이어도, 파이프와 `&&`/`;` 의 양쪽, `$( … )` 와 here-string 안이어도 찾아냅니다.

| 패턴 | 막는 예 | 막지 않는 예 |
|---|---|---|
| `rm -rf` | `rm -rf x`, `rm -r -f x`, `rm -fR x`, `sudo rm --recursive --force x` | `rm -r x`(`-f` 없음) |
| `git push --force` | `git push --force`, `git push -f`, `git push --forc`, `git -C repo push origin --force` | `git push --force-with-lease`(다른 옵션이므로 필요하면 따로 추가) |
| `git push origin` | `git push origin main` | `git push upstream main` |
| `docker * prune` | `docker system prune`, `docker image prune -a` | `docker prune` |
| `mkfs*` | `mkfs.ext4 /dev/sdb1` | |
| `rm -r ~/keep` | `rm -rf ~/keep`, `cd ~ && rm -r keep` | 다른 폴더에서 실행한 `rm -r keep` |
| `curl --upload-file=*` | `curl --upload-file=notes.txt https://…` | |
| `re:^ssh .*prod` | `ssh deploy@prod.example` | `ssh dev.example` |

규칙:

- **첫 단어는 프로그램**이며 이름으로 비교합니다(`/usr/bin/rm` 은 `rm`, `python3.12` 는 `python` 이기도 함). 와일드카드(`mkfs*`)를 쓸 수 있지만 와일드카드만으로 쓸 수는 없습니다.
- **옵션**(`-` 로 시작하는 단어)은 모두 있어야 하며, 위치와 순서는 상관없습니다. `-rf` 는 `-r` 과 `-f` 를 뜻하고, 어떻게 쓰든(`-rf`, `-fr`, `-r -f`, `-rfv`) 맞습니다. `--force` 는 프로그램이 받아들이는 줄임말(`--forc`)과 `--force=…` 도 맞습니다. `rm` 에서는 `-r` = `-R` = `--recursive`, `-f` = `--force`, `chmod`/`chown`/`chgrp` 에서는 `-R` = `--recursive`, `git push` 에서는 `-f` = `--force`, `git branch -D` = `--delete --force` 입니다. `find` 의 옵션은 단어 전체입니다(`find -delete`).
- **나머지 단어**는 명령의 다른 단어들 사이에 같은 순서로 있어야 합니다(붙어 있지 않아도 됩니다). `*` 는 아무 단어 하나입니다. `/` 나 `~` 로 시작하는 단어는 경로이며, 해석한 뒤(`~`, `$HOME`, 명령이 실행되는 폴더 기준 상대 경로) 비교합니다.
- `re:` 뒤에 정규식을 쓰면 각 명령의 텍스트(단어를 공백으로 이은 것)에서 찾습니다. 빈 명령에도 맞는 정규식은 거절됩니다.

쓸 수 없는 패턴은 저장할 때 이유와 함께 거절됩니다("the program cannot be only a wildcard", "the regular expression does not compile: …").

## 직접 추가하는 보호 폴더

절대 경로(`/srv/backups`)나 홈 아래 경로(`~/finance`)를 입력하고 수준을 고릅니다. 폴더를 보호하면 그 아래 모든 것이 보호됩니다. 다음과 같은 변경이 해당됩니다.

- 그 폴더나 그 아래를 쓰기, 삭제, 비우기(truncate), `chmod`/`chown`, 출력 리디렉션, 압축 해제, 그곳에서 또는 그곳으로 이동·이름 바꾸기;
- 그 **위** 폴더를 삭제, 이동, 재귀 변경(`chmod -R`, `git clean`, 압축 해제);
- **접근 금지**이면 추가로: 읽기, 목록 보기, 위 폴더를 재귀로 검색(`grep -r`, `find … -exec`). **접근 금지** 폴더 안의 경로를 이름으로 언급하기만 하는 명령도 거절됩니다.

심볼릭 링크는 양쪽으로 따라갑니다(보호 폴더를 가리키는 링크도 보호되고, 링크로 지정한 보호 경로는 링크가 가리키는 곳을 보호합니다). 와일드카드(`rm -rf /srv/*`)나 셸만 아는 부분이 있는 경로(`rm -rf /srv/$NAME`)는 닿을 수 있는 가장 넓은 범위로 판단합니다. 프로젝트 안의 보호 폴더도 다른 곳과 똑같이 보호됩니다.

## 시험해 보기

화면에 "시험해 보기" 칸이 있습니다. 명령이나 경로를 입력하면, 저장된 목록이나 아직 저장하지 않은 편집 내용 기준으로 막히는지, 무엇 때문인지 알려 줍니다. 아무것도 실행하지 않습니다.

## snowpea 가 읽는 것

snowpea 는 호출이 실행되기 전에 그 내용을 읽고 판단합니다: 명령 줄, 그 안의 모든 명령(`sudo`, `env`, `xargs`, `sh -c`, 파이프, `$( … )` 를 거친 것도), 그 줄에 적힌 코드(`python -c`, `node -e`, here-document), 그 줄이 이름으로 가리키는 스크립트(`./x.sh`, `python tool.py`, `awk -f prog.awk`), git 별칭, 그 줄이 가리키는 `npm`/`deno` 작업. 파일 도구의 경로도 같은 방식으로 읽습니다.

읽은 내용이 이 페이지에서 설명하는 대로 목록과 snowpea 자체 파일 규칙에 비추어 허용되면 묻지 않고 실행합니다. 읽을 수 없었던 것은 사용자에게 묻습니다(아래). 이것은 호출이 말하는 내용을 확인하는 것이지, 호출이 그다음에 하는 일을 둘러싼 벽이 아닙니다.

### snowpea 자체 파일

목록과 별개로, 끌 수 없는 규칙이 하나 있습니다. snowpea 가 무엇을 신뢰할지, 누가 snowpea 와 통신할 수 있는지를 정하는 파일에 대한 규칙이며, 어떤 모드든, 어떤 프리셋이든, 두 목록이 모두 비어 있어도 snowpea 가 읽는 모든 호출에 적용됩니다.

- **읽기는 절대 안 됨, 변경은 한 번 물어봄**: `$SNOWPEA_HOME` 의 `daemon.json`, `token`, `credentials.json`, `mcp-oauth.json`, 그리고 그 옆에서 같은 이름으로 시작하는 모든 파일(`credentials.json.tmp`, 쓰다 만 사본). 이 파일들이 있으면 프로그램이 사용자 본인처럼 행동할 수 있습니다. 다른 에이전트 CLI 의 로그인 파일도 snowpea 폴더 안에서든 홈 폴더에서든 똑같이 다룹니다: `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.claude.json`, Gemini 와 Cursor 의 로그인 파일, `~/.config/anthropic`.
- **변경은 한 번 물어봄**(읽기는 허용): `settings.json`, 모든 프로젝트에서 시작되는 MCP 서버 목록(`$SNOWPEA_HOME` 의 `.mcp.json`), 신뢰한 프로젝트 기록(`trust/`), 자비스의 규칙과 기록(`jarvis/`), 세션 저장소, 업데이트 파일, 승인·보안 로그 — 그리고 **`$SNOWPEA_HOME` 바로 아래의 다른 모든 이름** 중 아래 작업 공간이 아닌 것. snowpea 폴더는 기본적으로 닫혀 있고 에이전트가 일하는 곳만 열려 있습니다: 계획 파일, 세션 작업 폴더(`sessions/`), 첨부 파일, 도구 결과, 오디오, 캐시, 체크포인트, 일반 로그, 팀, `memory.md`, 확장 데이터.
- **물어봄**: `$SNOWPEA_HOME` 의 사용자 훅, 플러그인, 확장, 스킬, 명령, 에이전트 정의에 쓰는 셸 줄이나 코드; `$SNOWPEA_HOME` 안 어디에든 만드는 링크(`ln`, `ln -s`, `cp -s`, `os.symlink`) — 링크는 그 안의 무언가에 붙는 두 번째 이름이거나 밖으로 나가는 길입니다; snowpea 자체 프로그램 파일(패키지 폴더, snowpea 가 실행되는 Python, `snowpea` 실행 파일)에 쓰는 것. 단, 그 폴더가 작업 중인 프로젝트 안에 있으면 제외합니다.
- **물어봄**: snowpea 자체 명령을 실행하는 줄(`snowpea …`, `python -m snowpea_core …`, `uv run` 을 거친 경우, snowpea 의 스크립트, snowpea 가 읽는 언어의 코드 안에서 부르는 경우). 이 명령은 사용자 본인으로 동작하므로 사용자만 허용할 수 있습니다. 카드에 "snowpea 자체 CLI 를 실행합니다"라고 표시됩니다.
- **물어봄**: 위의 "변경은 한 번 물어봄" 파일을, 그 파일을 바꿀 수도 있는 프로그램에 넘기는 경우. `cat`, `grep`, `jq`, `ls`, `stat`, `cp`(원본으로 쓸 때) 같은 프로그램은 읽기만 하므로 전처럼 실행됩니다. 그 밖에는 모두 묻습니다. 세션 저장소를 데이터베이스 클라이언트로 여는 경우(`sqlite3 ~/.snowpea/state.db …`, 보기만 해도), 그 파일로 가는 링크(`ln -s`), 그 경로를 담은 변수, 그 파일을 이름으로 가리키면서 읽기 이상의 일을 하는 코드가 그렇습니다.

거절된 읽기는 `blocked by the security list: security — $SNOWPEA_HOME/token …` 처럼 표시됩니다. 이 파일들을 바꾸는 호출은 "snowpea 자체 설정을 건드림"이라고 적힌 카드로 [한 번](#차단된-호출은-사용자에게-한-번-묻습니다) 묻습니다. "물어봄"은 사용자만 답할 수 있습니다. 자비스가 결정하지 않으며, 답을 기억하지도 않습니다.

snowpea 는 자기 파일도 조심해서 씁니다. 파일마다 옆에 임의의 이름으로 새 파일을 써서 제자리로 옮기며, 심볼릭 링크인 파일(또는 사용자 소유가 아닌 파일)에는 쓰지 않고, 그런 파일을 `credentials.json`, `.mcp.json`, `hooks.json`, `daemon.json` 으로 불러오지도 않습니다. 대신 로그에 이유를 남기며, 그 파일의 내용은 쓰지 않습니다: 저장된 자격 증명, 그 파일의 MCP 서버나 훅, 그 파일로 찾은 데몬 모두 쓰지 않습니다. `credentials.json` 의 경우 로그와 보안 화면이 파일 이름과 고치는 법(링크를 파일 자체로 바꾸기)을 알려 줍니다.

`settings.json` 은 예외입니다. dotfile 관리 도구(stow, chezmoi, home-manager)가 이 파일을 링크로 두기 때문입니다. 사용자 소유의 일반 파일을 가리키는 링크는 따라갑니다. snowpea 는 그 파일을 읽고, 바뀐 내용을 그 파일에 씁니다(링크는 링크로 남고, 파일의 권한도 그대로입니다). 그리고 그 파일이 어디에 있든 `settings.json` 과 똑같이 보호합니다. 셸 명령, 코드, `fs.write`, 파일 도구가 그 파일에 쓰려 하면 `settings.json` 과 같이 거절하거나 묻습니다. 세션에서 그런 링크를 만들면 사용자에게 묻습니다(`$SNOWPEA_HOME` 안에 만드는 링크는 항상 묻습니다). 다른 것을 가리키는 링크(폴더, 사용자 소유가 아닌 파일, 없는 파일, 순환)는 쓰지 않습니다. 그때는 읽을 수 없는 파일과 같이 시작합니다(아래).

## snowpea 가 읽지 않는 것

다음은 snowpea 에게 보여 준 적 없는 코드를 실행합니다. 테스트를 돌리거나 커밋할 때마다 묻는다면 auto 모드를 쓸 수 없으므로 snowpea 는 이것들을 그대로 실행합니다. 그러니 무슨 뜻인지 알아 두세요.

- **테스트 실행기와 빌드 도구.** `pytest`, `npm test`, `make`, `cargo build`, `tox`, `pre-commit`, `mkdocs build` 등은 프로젝트 자체 파일을 불러옵니다: 테스트 파일, `conftest.py`, `Makefile`, `package.json` 스크립트, 플러그인. snowpea 는 명령 줄을 읽지, 그 파일들이 하는 일을 읽지 않습니다. 에이전트가 테스트나 빌드 파일에 쓴 코드는 테스트나 빌드가 실행될 때 실행됩니다.
- **git.** git 은 훅(`.git/hooks/*`)과 설정이 가리키는 프로그램(`core.pager`, `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`, `diff.external`, `*.textconv`, `filter.*`, `credential.helper`, `gpg.program` 등)을 실행합니다. snowpea 가 여기서 하는 일: `.git/hooks`, `.git/config`, `.git/info/attributes` 에 쓰면 묻습니다; `git config`, `git -c`, `--config-env` 로 그런 키를 설정하면 묻습니다(`core.pager=cat` 처럼 아무것도 실행하지 않는 값은 묻지 않음); 에이전트가 훅을 쓰거나 바꾼 저장소, 또는 에이전트가 그런 키를 넣어 설정 파일을 쓴 저장소에서 git 명령을 실행하면 묻습니다. 세션 전부터 있던 훅이나 설정은 snowpea 가 없을 때와 똑같이 실행됩니다.
- **`PATH` 에서 찾는 프로그램.** 이름만 쓴 명령(`mytool`)이면 snowpea 는 그 명령이 받을 것과 같은 `PATH` 로 무엇이 실행될지 찾아봅니다. 그 파일을 에이전트가 썼거나(snowpea 가 기억하는 어느 세션에서든) 임시 폴더에 있으면 에이전트가 쓴 스크립트처럼 읽습니다: 하는 일을 판단하고, 읽을 수 없는 파일(바이너리)이면 묻습니다. 프로젝트 밖에 있는, snowpea 자신의 `PATH` 에 있는 폴더(`~/.local/bin`, `/usr/local/bin`)에 쓰면 묻습니다. 그 밖에 `PATH` 에 있는 것은 시스템의 프로그램이며 이름 그대로 실행됩니다.
- **프로그램이 불러오거나 시작하는 것.** 라이브러리, 플러그인, 인터프리터가 찾는 모듈, 실행 중에 조합한 하위 프로세스, 컴파일된 프로그램.

이런 방식으로 실행되는 코드는 이 컴퓨터에서 사용자가 할 수 있는 모든 일을 할 수 있습니다: snowpea 의 토큰을 읽고, `snowpea …` 를 실행하고, snowpea 의 파일을 바꿀 수 있습니다. 변조 감시(다음)는 snowpea 가 실행 중인 동안 신뢰 기록과 위임 기록을 지킵니다. 나머지는 운영체제가 할 일입니다([앞으로 할 일](#앞으로-할-일) 참고).

## snowpea 가 실행 중일 때

### 변조 감시

가장 중요한 두 기록에는 어떤 명령도 읽지 않는 두 번째 장치가 있습니다. snowpea 가 실행 중인 동안에는 신뢰한 프로젝트 기록(`trust/`)과 자비스의 위임 규칙(`jarvis/delegation.db`)의 사본을 메모리에 직접 가지고 있습니다. 모든 판단은 파일을 다시 읽은 내용이 아니라 이 사본으로 하며, 사본은 snowpea 자신이 쓸 때만 바뀝니다. snowpea 는 기록을 쓰기 전에 파일을 확인합니다. 다른 무언가가 파일을 바꾸거나 바꿔치기하거나 지웠다면, 또는 위임 규칙 데이터베이스 옆에 저널 파일을 남겼다면, snowpea 는 그 변경을 쓰지 않고 자기 사본을 되돌려 놓고 `logs/security.jsonl` 에 한 줄을 남기고 사용자에게 알립니다("snowpea 밖의 무언가가 신뢰한 프로젝트 기록을 바꿨습니다 … snowpea 는 그 변경을 쓰지 않고 자기 사본을 유지했습니다". 보안 화면에도 나옵니다). 자비스 규칙의 경우에는 자비스 알림으로 옵니다. 파일이 거듭 바뀌면 처음 한 번은 바로, 그 뒤로는 1분에 한 번까지 횟수와 함께 알립니다. 파일을 되돌리지 못했다면 알림에 그렇게 적힙니다.

이 감시는 snowpea 가 실행 중일 때만 동작합니다. snowpea 가 꺼져 있으면 지켜보는 것이 없고, 다음에 시작할 때는 파일을 있는 그대로 받아들입니다. 다만 위임 규칙 데이터베이스 옆의 저널이나 WAL 파일은 읽지 않고 지웁니다. 사용자 권한으로 실행되는 프로그램은 snowpea 를 멈출 수도 있습니다.

### 사용자를 기다리는 설정

`settings.json` 은 손으로 고칠 수 있고, 대부분은 전처럼 바로 반영됩니다. 프로그램을 실행하거나 권한을 넓힐 수 있는 설정은 snowpea 가 실행 중일 때 파일을 고친 것만으로는 적용되지 않습니다: `security`(목록과 `unknownCode`), `jarvis`, `web.remote`, 허용 규칙(`allowlist`), MCP 권한, 그리고 프로그램이나 snowpea 가 호출하는 주소나 snowpea 자체 코드를 가리키는 모든 설정 — MCP 서버의 명령과 주소, 언어 서버, `media.mcp`, 오디오 재생기·녹음기·음성 명령, 제공자의 base URL, 검색·브라우저 자격 증명과 제공자 블록, 메신저 블록의 주소 모양 필드(이름이 `url`, `uri`, `endpoint`, `host`, `proxy` 로 끝나는 것: `base_url`, `baseUrl`, `SEARXNG_URL` 등), 확장, 스킬 레지스트리, 업데이트 채널. snowpea 는 가지고 있던 값을 유지하고 "settings.json 이 디스크에서 바뀌었습니다: 프로그램을 실행하거나 권한을 넓힐 수 있는 설정(…)이 적용되지 않고 확인을 기다립니다"라고 알린 뒤, 사용자가 로컬 연결(IDE, TUI, CLI, 로컬 웹 페이지)에서 적용하라고 할 때만 적용합니다. 고친 내용을 버릴 수도 있으며, 그러면 snowpea 가 가진 값을 파일에 다시 씁니다. 설정 화면과 보안 화면에서 같은 설정을 바꾸는 것은 전과 똑같이 됩니다. 다른 설정과 API 키는 전처럼 바로 반영됩니다. `jarvis` 에는 **원격 웹 로그인에서 위임 허용**(`jarvis.delegation.remoteGrant`)도 들어 있습니다. 이 설정은 에이전트의 설정 도구로는 아예 바꿀 수 없습니다([snowpea 웹](web.md#원격-로그인에서-위임하기)).

시작할 때는 `settings.json` 을 있는 그대로 받아들입니다. 다만 사용자 소유의 일반 파일이 아니거나(사용자 파일을 가리키는 링크는 따라갑니다. 위를 보세요) 올바르지 않으면 다릅니다. 그때는 해당 섹션이 기본값을 씁니다(파일을 아예 읽을 수 없으면 모든 섹션). `security` 는 기본 보안 목록에 알 수 없는 코드는 묻기로, `web` 은 원격 접속이 꺼진 채로입니다. 보안 화면과 `snowpea daemon status` 가 이를 알려 주고, 원래 파일은 `settings.json.invalid` 로 남깁니다. 파일 전체를 읽을 수 없는 동안에는 snowpea 가 그 파일을 덮어쓰지 않습니다. 설정을 바꾸려 하면 "… could not be read, so snowpea runs on defaults and does not write over it" 이라며 거절하고, 파일을 고친 뒤 snowpea 를 다시 시작하거나 설정을 다시 읽게 하면 됩니다. 일부 섹션만 올바르지 않으면 나머지는 그대로 쓰고 평소처럼 저장하며, 올바르지 않은 섹션을 설정하면 그 섹션이 바뀝니다.

## snowpea 가 확인할 수 없는 줄

실행 전에 무엇을 건드릴지 snowpea 가 알 수 없는 줄(변수로 정해지는 프로그램, 읽을 수 없는 스크립트, 실행 중에 계산되는 쓰기 대상)은 "snowpea가 실행 전에 무엇을 건드릴지 알 수 없어 확인합니다" 표시와 함께, 확인하지 못한 부분(명령, 원인이 스크립트 안에 있으면 그 파일과 줄)을 하나씩 보여 주며 사용자에게 묻고, 자비스가 결정하지 않습니다. 리터럴 인자로 실행한 스크립트나 변수에 리터럴 값 하나만 넣는 스크립트(`OUT=${1:-/tmp/shot.png}`, `ADB=/opt/sdk/adb`)는 그 값으로 읽어 다른 줄과 똑같이 확인합니다. 거절하는 것이 아니라 사용자가 결정합니다. 기본값 `security.unknownCode: "auto"`(아래)에서는 자동 모드 세션에서 이런 줄을 묻지 않고 실행합니다. snowpea 자체 제어에 닿는 줄은 언제나 묻습니다.

snowpea 프로그램을 읽기만 하려고 실행하는 것은 평범한 읽기입니다: `snowpea --help`, `snowpea --version`, `snowpea session list`, `snowpea daemon status`, `snowpea agents`(`export PATH="$HOME/.local/bin:$PATH"` 뒤에서도). 그 밖의 `snowpea …`(`project trust`, `--home`, `daemon stop` 등)는 어떤 모드든 묻습니다.

읽을 수 없는 것은 다음과 같습니다. 프로그램이 변수나 치환에서 오는 경우(`$CMD …`, `` `echo rm` … ``), 변수의 `eval`, 셸로 파이프한 스크립트(`… | sh`), 변수로 만든 `sh -c`, snowpea 가 읽는 방법을 모르는 프로그램에 넘긴 인라인 코드(`mongosh --eval`, `gdb -ex`, `Rscript -e`, `elixir -e` 등), 다른 컴퓨터로 보내는 명령(`ssh host '명령'`. 이 컴퓨터로 보내는 경우에는 명령을 읽습니다)이나 나중에 실행되도록 맡기는 명령(`crontab 파일`, `at`, `batch`, `launchctl load`), 셸 내장 명령의 이름을 가져간 함수(`cd() { … }`), 셸만 아는 경로인데 앞부분으로도 어디에 닿을지 알 수 없는 경우. 앞쪽 항목들은 **두 목록이 모두 비어 있어도** 묻습니다. snowpea 자체 파일에 대한 행동이 그 안에 숨어 있을 수 있기 때문입니다.

다음 프로그램이 무엇인지, 무엇을 하는지를 바꾸는 줄도 마찬가지입니다.

- 코드를 실어 보내거나 설정 위치를 바꾸는 변수: `LD_PRELOAD`, `BASH_ENV`, `NODE_OPTIONS=--require …`, `GIT_SSH_COMMAND`, 프로그램으로 지정한 `EDITOR`/`PAGER`, 프로젝트 안 폴더나 임시 폴더나 상대 경로가 들어간 `PATH`, `PIP_INDEX_URL`, `NPM_CONFIG_SCRIPT_SHELL`, `JAVA_TOOL_OPTIONS`/`JDK_JAVA_OPTIONS`, `LUA_INIT`, `DOTNET_STARTUP_HOOKS`, `PHPRC`, `VIMINIT`, `ERL_AFLAGS`, `RSYNC_RSH`, `KUBECONFIG`, `AWS_CONFIG_FILE`, `DOCKER_CONFIG`, `CURL_HOME`, `WGETRC`, `GCONV_PATH`, `LESSOPEN`;
- snowpea 가 모르는 **그 밖의 대문자 변수**. 단, 값이 평범한 단어나 숫자(`FOO=bar`, `LEVEL=3`)이거나, 흔한 빌드·테스트·앱 설정이면 묻지 않습니다: `NODE_ENV`, `CI`, `DEBUG`, `LANG`, `LC_*`, `TZ`, `PORT`, `HOST`, `FORCE_COLOR`, `RUST_LOG`, `PYTEST_*`, `DJANGO_*`, `FLASK_*`, `VITE_*`, `_URL`, `_KEY`, `_TOKEN`, `_SECRET`, `_ENV`, `_PORT`, `_HOST` 로 끝나는 이름, 프로젝트 안을 가리키는 `*_DIR`. 소문자 이름(`my_var=…`)은 셸 자체의 것이며 전처럼 실행됩니다;
- `HOME`, `PWD`, `TMPDIR`, `SNOWPEA_HOME`, `CDPATH`, `XDG_*` 변수에 다른 값을 주는 경우: snowpea 는 `~` 와 `$HOME` 을 실제 홈 폴더로 읽으므로, 이런 대입 뒤에는 경로가 어디에 닿는지 말할 수 없습니다;
- 프로그램을 실행하는 설정을 준 `git -c`, 명령과 함께 시작한 편집기(`vim -c '!…'`), `system` 을 부르는 `awk` 프로그램.

묻지 않고 그대로 실행되는 예도 적어 둡니다. `PATH=/usr/local/bin:/usr/bin:/bin make`, `export PATH="$PATH:/usr/local/bin"`, 그리고 snowpea 자신의 `PATH` 에 이미 있는 폴더; `NODE_ENV=production npm test`, `CI=1 …`, `FOO=bar ./script.sh`, `DATABASE_URL=postgres://… pytest`, `GOOS=linux GOARCH=amd64 go build`; `PYTHONPATH=src pytest`; `CC=gcc make`, `MAKEFLAGS=-j8`; `GIT_PAGER=cat`, `EDITOR=true`, `git -c core.pager=cat log`; `PIP_NO_CACHE_DIR=1`, `NPM_CONFIG_LOGLEVEL=error`, `CARGO_TERM_COLOR=always`; 원래 임시 폴더가 `/tmp` 일 때의 `TMPDIR=/tmp`.

## snowpea 가 판단할 수 없는 코드

snowpea 는 한 줄이 실행하는 코드를 실행하지 않고 읽습니다. `python -c`, `node -e`, `bun -e`, `deno eval`, 인터프리터에 넘긴 here-document, 스크립트 파일, `execute_code` 가 해당됩니다.

- **대상을 직접 적은** 삭제·덮어쓰기·이동 코드는 그 코드가 뜻하는 명령으로 판단합니다. `shutil.rmtree('/home/me')` 는 어떻게 쓰든, 어떻게 import 하거나 별칭을 붙이든 `rm -rf /home/me` 입니다.
- 읽기, 출력, 계산만 하는 코드와 적어 둔 경로에 쓰는 코드는 전처럼 실행됩니다.
- snowpea 가 **판단할 수 없는** 코드는 어떤 모드든 "이 코드가 무엇을 바꾸는지 알 수 없습니다"라고 묻습니다. 실행 중에 대상이 정해지는 삭제·쓰기·복사·이동·`chmod`(`os.remove(path)`), 변수로 조합한 명령, 문법 오류로 읽을 수 없는 코드, `getattr`·`eval`·`exec`·계산된 import 로 닿는 호출, snowpea 가 읽을 수 없는 코드를 받는 인터프리터(`curl … | python3`, `python3 -` 만 있는 경우), 알려진 도구도 아니고 프로젝트에도 없는 모듈의 `python -m` 이 그렇습니다.

연결된 호스트가 호스트 안에서 실행하는 코드(Snowpea 브라우저의 `repl` 스크립트는 브라우저 자신의 페이지에서 실행됩니다)는 이 검사가 아니라 그 호스트가 판단합니다. `exec` 로 표시된 호스트 도구의 코드는 여전히 읽고, 모든 호스트 도구의 파일 경로도 여전히 판단합니다. `language: "javascript"` 나 `"typescript"` 를 받은 코드 도구의 코드는 JavaScript 리더로 읽습니다(최상위 `await` 포함).

세션 전부터 프로젝트에 있던 스크립트는 프로젝트의 것입니다. 분명히 하는 일은 판단하지만(그 안의 차단 명령은 차단됨), 계산해서 정하는 부분을 문제 삼지는 않습니다. 에이전트가 쓴 스크립트는 엄격하게 읽습니다: 읽을 수 없는 부분과 계산해서 정하는 부분은 묻습니다. snowpea 는 에이전트가 쓴 파일을 세션마다 기억하며, 재시작 뒤에도 기억합니다. snowpea 가 시작한 뒤 세션이 쓴 것은 지워지지 않고, 목록으로 담을 수 있는 것보다(20 000 개) 많은 파일을 쓴 세션은 그때부터 바뀌는 모든 파일을 그 세션이 쓴 것으로 봅니다.

다음도 묻습니다. 문자열을 다른 경로로 코드로 실행하는 호출(`timeit.timeit("…")`, `cProfile.run`, `pdb.run`, node 의 `new Function(…)`, 문자열로 시작하는 워커)에서 그 문자열이 그대로 적혀 있지 않은 경우; 피클 데이터 불러오기(`pickle.load`. 불러오는 순간 코드가 됩니다); snowpea 몰래 자기 이름이나 환경을 바꾸는 코드(`os.chdir`, `os.environ` 의 `HOME` 변경, `__main__` 에 값 넣기).

물어보게 되는 흔하고 해롭지 않은 예는 다음과 같습니다. 에이전트가 방금 쓴 스크립트가 인자로 받은 경로에 쓰는 경우(`open(args.out, "w")`), 조합한 명령으로 부르는 `subprocess.run(cmd)`, `python -m http.server`, 에이전트가 방금 쓴 Makefile 이 make 변수를 쓰는 경우의 `make`, `python -c "print('$NAME')"`(Python 이 읽기 전에 셸이 글자를 채워 넣습니다), `pickle.load(open('model.pkl', 'rb'))`, `HOME=/tmp/x npm test`, `ssh build-host make`, 같은 줄에서 빌드되어 아직 없는 `dist/index.js` 를 실행하는 `node dist/index.js`, `PYTHONPATH=$(pwd) pytest`(`PYTHONPATH=. pytest` 로 쓰면 묻지 않습니다), `TAG=$(git describe) make`, `BUILD_DIR=/tmp/build make`, `CONFIG_FILE=config/dev.yaml npm start`, `JAVA_OPTS=-Xmx2g ./gradlew build`, `EXTRA_ARGS='--verbose' make`(snowpea 가 모르는 변수에 평범한 단어 이상의 값), `git config core.editor vim`, `$SNOWPEA_HOME` 바로 아래에 직접 파일을 쓰는 경우(거절됨: `sessions/` 나 `plans/` 아래에 두세요).

### `security.unknownCode`

보안 화면의 "실행 전에 알 수 없는 명령·스크립트", 또는 `$SNOWPEA_HOME/settings.json`(이 설정을 파일에서 직접 고치면 위에서 말한 대로 확인을 기다립니다):

```json
{ "security": { "unknownCode": "auto" } }
```

- `"ask"`: 어떤 모드든 묻습니다.
- `"auto"`(0.2.90 부터 기본값): 자동 모드 세션에서는 묻지 않고, 계획·수락 모드에서는 묻습니다.
- `"allow"`: 어떤 모드에서도 묻지 않습니다(snowpea 가 해석하지 못하는 줄도 포함). 차단 명령, 보호 폴더, snowpea 자체 파일, 그리고 아래의 항상 묻는 경우(예: 내려받은 스크립트를 바로 실행)만 묻습니다. 읽지 못한 줄은 원문을 최악의 경우로 확인합니다: 어디든 차단 명령이 있으면 거절하고, 그 안의 경로를 아래까지 통째로 지운다고 볼 때 보호 폴더나 snowpea 자체 파일에 닿으면 묻습니다.

포기하는 것: `"auto"`(자동 모드) 나 `"allow"` 에서는 실행 중에 대상이 계산되는 스크립트나 명령이 묻지 않고 보호 폴더에 쓸 수 있습니다. snowpea 가 읽을 수 있는 것은 그대로 판단합니다: 보이는 차단 명령과 보호 경로는 거절됩니다. 설정과 상관없이 언제나 묻는 것: snowpea 자체 파일이나 명령에 닿는 모든 것(읽을 수 없는 부분 옆에서 snowpea 를 가리키는 것 포함); 셸만 아는 프로그램에 보호 경로를 넘기는 것(`$X -rf ~`); `HOME`, `PWD`, `PATH` 같은 변수를 바꾸는 것; 프로그램을 실행하라고 시킨 git, git 이나 `PATH` 가 프로그램을 찾는 곳에 쓰는 것; 같은 줄에서 쓰고 바로 실행하는 스크립트.

snowpea 0.2.88·0.2.89 가 쓴 설정에는 `"ask"`(예전 기본값)가 저장되어 있습니다. 0.2.90 을 처음 시작할 때 저장된 `"ask"` 를 한 번만 `"auto"` 로 바꾸고, 보안 화면에 알리고, `$SNOWPEA_HOME/security/unknown-code-migrated.json` 에 기록합니다. 그 뒤로는 다시 바꾸지 않으므로 `"ask"` 로 되돌리면 그대로 유지됩니다.

로컬 연결이나 Snowpea 브라우저의 설정 화면에서 사용자 본인이 바꿀 수 있고, 프로젝트는 바꿀 수 없습니다.

### `security.enabled`: 보안 검사 끄기

보안 화면의 "보안 검사", 또는 `$SNOWPEA_HOME/settings.json`(파일에서 직접 고치면 위에서 말한 대로 확인을 기다립니다):

```json
{ "security": { "enabled": false } }
```

끄면 차단 명령과 보호 경로(프리셋, 직접 넣은 항목, 프로젝트가 더한 항목)를 적용하지 않고, 실행 전에 읽을 수 없는 코드는 `unknownCode: "allow"` 처럼 실행됩니다. 목록과 `unknownCode` 값은 저장된 그대로 남으므로 다시 켜면 모두 돌아옵니다. 꺼져 있어도 그대로인 것: snowpea 자체 파일이나 명령에 닿는 모든 것, 모드 규칙(계획 모드는 쓰지 않음, 수락·자동은 평소대로), Jarvis 위임 규칙, 사용자 본인만 답할 수 있는 질문.

꺼져 있는 동안 보안 화면에 "보안 검사가 꺼져 있음"과 누가(데스크톱, TUI, 브라우저 …) 언제 껐는지가 나옵니다(`security.get` 의 `enabled`, `disabledBy`, `disabledAt`, 문제 코드 `security-disabled`). 켜고 끌 때마다 `$SNOWPEA_HOME/logs/security.jsonl` 에 한 줄(`"kind": "security-switch"`)이 남고, 누가·언제는 `$SNOWPEA_HOME/security/switch.json`(어떤 도구도 바꿀 수 없는 제어 파일)에 기록되며, 연결된 모든 앱·TUI·브라우저에 바로 알립니다.

바꿀 수 있는 사람: 로컬 연결의 사용자 본인과 Snowpea 브라우저의 설정 화면(프로토콜 1.58.0). 브라우저는 `security.enabled`, `security.unknownCode`, `security.blockedCommands`, `security.protectedPaths` 만, 설정 화면에서만 바꿀 수 있습니다. 원격 웹 로그인, 커넥터, Jarvis, 에이전트 자신의 도구는 어느 것도 바꿀 수 없고, 프로젝트는 보안 검사를 끌 수 없습니다.

### 허락한 스크립트 기억하기

호출이 오직 프로젝트 안 스크립트가 하는 일 때문에 묻는 경우 — 예를 들어 스크립트가 계산한 파일 이름에 쓰는 `bash bin/shot.sh` — 카드는 답을 **바로 그 명령과 지금 그대로의 스크립트**에 대해 기억하겠다고 제안합니다("bash bin/shot.sh (while bin/shot.sh is unchanged)"). 기본은 "이 세션 동안"이고, "이 프로젝트에서"를 고르면 사용자 본인 설정에 남습니다(프로젝트의 `.snowpea/settings.json` 에는 절대 쓰지 않음). 다른 허용 규칙과 함께 목록에 보이고 거기서 지울 수 있습니다. 스크립트 내용이 바뀌는 순간 snowpea 는 스크립트가 바뀌었다고 말하며 다시 묻습니다.

스크립트만이 이유가 아니면 제안하지 않습니다: 줄 자체에 snowpea 가 읽지 못하는 부분이 있을 때, 같은 줄이 먼저 쓰는 스크립트, 프로젝트 밖의 스크립트, snowpea 자체 파일이나 명령에 닿는 것, 다른 곳에서 가져온 코드를 실행하는 스크립트(파일 내용의 `eval`, 셸로 보내는 파이프, 변수로 정한 프로그램, 코드로 읽히는 변수). 앱, TUI, 브라우저에서 사용자 본인이 한 답만 기억되며, 원격 웹 로그인, 메신저, Jarvis 의 답은 기억되지 않습니다.

### `;`, `&&`, `||`, `|` 가 있는 줄

각 명령이 혼자서도 묻지 않고 실행될 명령들의 연결(각각 허용 규칙이 있거나, 계획 중에는 각각 살펴보기만 함)은 묻지 않고 실행됩니다. 묻는 연결 줄에 "이 세션 동안"이라고 답하면 **바로 그 줄**을, 같은 폴더에서, 이 세션 동안만 기억합니다(공백 차이는 상관없고 다른 글자는 모두 다르게 봄). 아무것도 저장하지 않으므로 다시 시작하면 잊고, 그런 줄에 "이 프로젝트에서"라고 답해도 세션 동안만 적용됩니다. snowpea 가 확인할 수 없는 줄, 원격 웹 로그인, 메신저, Jarvis 는 줄을 기억시키지 않습니다. 단일 명령에 대한 답은 예전과 똑같이 기억됩니다.

## 프로젝트 신뢰는 훅이 실행하는 것까지 고정합니다

프로젝트를 신뢰하면(`snowpea project trust` 또는 신뢰 카드) snowpea 는 프로젝트의 훅, MCP 서버, 명령, 에이전트, 프롬프트, 설정을 지금 상태 그대로 기록합니다. **그것들이 실행하는 프로젝트 파일**도 함께 기록합니다. 훅이 부르는 스크립트(`bash scripts/guard.sh`), 프로젝트 안에 있는 서버 프로그램, 스킬 폴더의 파일이 해당됩니다. 나중에 그중 하나가 바뀌면, 다시 확인하고 신뢰할 때까지 그 훅이나 서버는 꺼집니다. 심볼릭 링크인 제어 파일은 이 기록에 읽지 않습니다.

카드의 "runs" 아래에 이 파일들이 나옵니다. **not pinned** 아래에는 snowpea 가 코드를 기록할 수 없는 명령이 이유와 함께 나옵니다.

| 이유 | 뜻 |
|---|---|
| `computed` | 명령에 변수나 치환이 있어, 무엇이 실행될지 실행할 때 정해짐 |
| `project-script` | `npm run …`, `make …`: 프로젝트의 스크립트 목록이 정하며 바뀔 수 있음 |
| `package-runner` | `npx …`, `uvx …`: 실행할 때 찾거나 내려받는 패키지 |
| `module` | `python -m …`: 실행할 때 인터프리터가 찾음 |
| `missing` | 지금 없는 프로젝트 파일을 가리킴 |
| `outside-project` | 프로젝트 밖의 파일을 실행함(`/home/me/tools/x.sh`). 프로젝트의 것이 아니므로 기록하지 않음 |
| `symlink` | 프로젝트 안에 있지만 프로젝트 밖을 가리키는 링크를 실행함 |
| `too-large`, `too-many`, `unreadable` | 기록할 수 있는 양을 넘거나 읽을 수 없는 파일 |

"not pinned" 이어도 프로젝트를 신뢰할 수 있습니다. 다만 그 명령은 보이는 줄까지만 신뢰가 적용되고, 그 줄이 실행하는 것은 snowpea 가 다시 묻지 않아도 바뀔 수 있다는 뜻입니다.

**목록에 나온 파일만 고정됩니다.** 그 파일이 다시 불러오거나 실행하는 것(source 하는 스크립트, import 하는 모듈, 시작하는 프로그램)은 기록되지 않으며, 묻지 않고 바뀔 수 있습니다. 카드에도 그렇게 적혀 있습니다.

프로젝트가 가져오는 에이전트 정의(`.snowpea/agents/*.md`, `.claude/agents/*.md`)는 모두 여기에 포함됩니다. 프로젝트를 에이전트에 대해 신뢰하기 전에는 쓰이지 않고, 내용이 바뀌면 다시 신뢰해야 합니다. 사용자가 직접 `/agent create` 를 입력해서(또는 자기 IDE 나 터미널에서) 만든 에이전트는 snowpea 가 쓴 그대로 기록되어 바로 쓸 수 있습니다. 에이전트나 도구가 요청한 것(대기열에 넣은 명령, 자비스, 예약 작업)은 프로젝트의 것처럼 사용자의 신뢰를 기다립니다.

프로젝트 안에 또 프로젝트가 있는 경우(자체 `.snowpea` 가 있는 패키지 폴더), 같은 이름의 명령, 스킬, 에이전트는 안쪽 프로젝트 것이 우선하고, 바깥 프로젝트 것은 바깥 프로젝트를 신뢰하는 동안에만 보입니다.

신뢰하는 것은 화면에서 본 그대로입니다. 목록을 본 뒤 클릭하기 전에 파일이 바뀌면 아무것도 신뢰하지 않고 목록을 다시 보여 줍니다. `snowpea project trust` 는 목록을 출력하고, 묻고, 정확히 그 내용을 신뢰합니다(`--yes` 는 질문을 건너뜁니다).

신뢰한 프로젝트 기록이 없어지거나 손상되면(snowpea 가 꺼져 있는 동안 기록과 그 표식 파일이 함께 사라진 경우도 포함), 다시 신뢰할 때까지 어떤 프로젝트도 신뢰하지 않습니다. 세션의 첫 메시지와 보안 화면에서 그 사실을 알려 줍니다.

## 앞으로 할 일

오늘 막지 못하는 것을 그대로 적습니다.

- **읽히지 않고 실행되는 코드**(위 절)는 사용자가 할 수 있는 모든 일을 할 수 있습니다: snowpea 의 토큰을 읽고, 사용자 본인처럼 snowpea 에 말을 걸고(프로젝트를 신뢰시키거나 자비스의 규칙을 바꾸기 — 그런 변경은 snowpea 자신을 통해 들어오므로 변조 감시가 되돌리지 않습니다), snowpea 의 파일을 고칠 수 있습니다.
- **실행 중에 snowpea 의 로그인을 찾아내는 프로그램.** `$SNOWPEA_HOME` 의 토큰은 사용자 권한으로 실행되는 모든 프로그램이 읽을 수 있습니다. snowpea 는 그 파일을 이름으로 가리키는 호출을 거절하고 읽을 수 없는 호출은 묻지만, 실행 중에 경로를 만들어 내는 프로그램은 보지 못합니다.
- **snowpea 가 꺼져 있는 동안의 모든 것.** 변조 감시와 설정의 확인 대기는 실행 중인 프로그램 안에만 있습니다.

다음 단계는 무언가를 읽는 데 기대지 않는 경계입니다. snowpea 가 시작한 프로그램(직접이든 다른 프로그램을 거쳐서든)에서 오는 연결과 사용자에게서 오는 연결을 구별하려 합니다 — Linux 에서는 시작한 모든 프로그램을 자기 프로세스 트리에 묶어 두고 연결이 어디서 왔는지 확인하고, Windows 에서는 작업 개체(job object)로, macOS 에서는 샌드박스 프로필로 — 그러면 에이전트가 시작한 어떤 것도 snowpea 에게 사용자 본인처럼 행동할 수 없습니다. 그때까지 확실한 보장은 운영체제에 맡기세요.

## 실용적인 조언

- **신뢰하지 않는 저장소에서는 accept 모드를 쓰세요.** 모든 명령과 쓰기를 사용자가 직접 승인합니다. 또는 [샌드박스 세션](sandbox.md)을 쓰세요.
- `auto` 모드에서는 프로젝트 자체의 테스트, 빌드, 훅이 실행된다는 것을 기억하세요. 에이전트가 혼자 실행하게 두기 전에 저장소의 코드를 신뢰할 수 있는지 확인하세요.
- 원격 접속을 공개 인터넷에 열어 두지 마세요. 사설 네트워크나 직접 관리하는 터널 뒤에 두세요.
- 확실한 보장이 필요하면 운영체제 수준의 보호도 함께 쓰세요: 별도 사용자, 읽기 전용 마운트, 파일 권한, 또는 [샌드박스 세션](sandbox.md).

목록은 snowpea 를 거치는 것에 적용됩니다: snowpea 자체 도구, 승인, 자비스, 예약 작업, snowpea 가 제어하는 외부 CLI 세션(권한 요청이 연결된 경우). 다음은 볼 수 없습니다.

- 프로그램이 실행 중에 정하는 것: 내려받은 내용, 파일 내용, 문자열 계산으로 만든 경로나 명령. snowpea 는 판단할 수 없다는 것을 알 때 묻지만, 항상 알 수 있는 것은 아닙니다.
- 지정한 곳에 쓰는 라이브러리(`numpy.save`, `DataFrame.to_csv`), 컴파일된 프로그램, snowpea 가 읽지 못하는 언어의 스크립트(에이전트가 쓴 경우에만 묻습니다).
- unrestricted 모드의 외부 CLI 세션, 자기 터미널(tmux)에서 권한 요청에 답하는 세션, 다른 터미널에서 실행 중인 세션. 세션 목록에 표시되며, 목록이 설정된 상태에서 그런 세션이 열리면 snowpea 가 경고합니다.
