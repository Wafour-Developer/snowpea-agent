# 보안 목록

한 번 설정하면 모든 세션에 적용되는 두 가지 목록입니다. 자비스 모드든 아니든, 어떤 모드든, 누가 호출하든 똑같이 적용됩니다.

- **차단 명령**은 절대 실행되지 않습니다. 묻지도 않고, 승인할 수도 없으며, 누구도(자비스, 예약 작업, 기억된 답, "항상 허용" 규칙, auto 모드) 통과시킬 수 없습니다. 에이전트는 `blocked by the security list: commands:essential — …` 같은 거절 결과를 읽고, 화면에는 "보안 목록으로 차단됨"이 표시됩니다.
- **보호 폴더**는 절대 변경되지 않습니다. 항목마다 수준이 있습니다: **변경 금지**(`write`, 기본값: 읽기는 허용) 또는 **접근 금지**(`all`: 읽기, 목록, 변경 모두 금지).

두 목록 모두 켜고 끌 수 있는 **프리셋**과 직접 입력하는 항목으로 이루어집니다.

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
| `essential`(기본으로 켜짐) | `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` 은 바꾸지 않고, `/` 와 홈 폴더 자체(그리고 홈 위의 모든 폴더)는 삭제·이동·재귀 변경하지 않음 | 변경 금지 |
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

## snowpea 자체 파일은 항상 보호됩니다

목록과 별개로, 끌 수 없는 규칙이 하나 있습니다. snowpea 가 무엇을 신뢰할지, 누가 snowpea 와 통신할 수 있는지를 정하는 파일은 어떤 도구도 건드릴 수 없습니다. 어떤 모드든, 어떤 프리셋이든, 두 목록이 모두 비어 있어도 마찬가지입니다.

- **읽기도 변경도 금지**: `$SNOWPEA_HOME` 의 `daemon.json`, `token`, `credentials.json`, `mcp-oauth.json`. 이 파일들이 있으면 프로그램이 사용자 본인처럼 행동할 수 있습니다.
- **변경 금지**(읽기는 허용): `settings.json`, 신뢰한 프로젝트 기록(`trust/`), 자비스의 규칙과 기록(`jarvis/`), 세션 저장소, 업데이트 파일, 승인·보안 로그.
- **항상 물어봄**: `$SNOWPEA_HOME` 의 사용자 훅, 플러그인, 확장, 스킬, 명령, 에이전트 정의에 쓰는 셸 줄이나 코드.
- **항상 물어봄**: snowpea 자체 명령을 실행하는 줄(`snowpea …`, `python -m snowpea_core …`, `uv run` 을 거친 경우, snowpea 의 스크립트). 이 명령은 사용자 본인으로 동작하므로 사용자만 허용할 수 있습니다. 카드에 "snowpea 자체 CLI 를 실행합니다"라고 표시됩니다.

에이전트의 작업 공간은 그대로입니다: 계획 파일, 세션 작업 폴더, 첨부 파일, 캐시.

차단된 호출은 `blocked by the security list: security — $SNOWPEA_HOME/token …` 처럼 표시됩니다. "항상 물어봄"은 사용자만 답할 수 있습니다. 자비스가 결정하지 않으며, 답을 기억하지도 않습니다.

## snowpea 가 확인할 수 없는 줄

확인할 수 없는 줄이 있습니다. 프로그램이 변수에서 오는 경우(`$CMD …`), 변수의 `eval`, 셸로 파이프한 스크립트(`… | sh`), 변수로 만든 `sh -c`, 또는 셸만 아는 경로인데 앞부분으로도 어디에 닿을지 알 수 없는 경우입니다. 목록이 설정되어 있으면 이런 줄은 혼자 실행되거나 자비스가 결정하는 일이 없습니다. 어떤 모드든 "차단 목록과 대조할 수 없습니다" 표시와 함께 사용자에게 묻습니다. 거절하는 것이 아니라 사용자가 결정합니다.

실행할 것을 숨기는 줄도 마찬가지입니다. 다음 프로그램에 코드를 실어 보내는 변수(`NODE_OPTIONS=--require …`, `BASH_ENV=…`, `LD_PRELOAD=…`), 프로그램을 실행하는 설정을 준 `git -c`, 명령과 함께 시작한 편집기(`vim -c '!…'`), `system` 을 부르는 `awk` 프로그램이 그렇습니다.

## snowpea 가 판단할 수 없는 코드

snowpea 는 한 줄이 실행하는 코드를 실행하지 않고 읽습니다. `python -c`, `node -e`, 인터프리터에 넘긴 here-document, 스크립트 파일, `execute_code` 가 해당됩니다.

- **대상을 직접 적은** 삭제·덮어쓰기·이동 코드는 그 코드가 뜻하는 명령으로 판단합니다. `shutil.rmtree('/home/me')` 는 어떻게 쓰든, 어떻게 import 하거나 별칭을 붙이든 `rm -rf /home/me` 입니다.
- 읽기, 출력, 계산만 하는 코드와 적어 둔 경로에 쓰는 코드는 전처럼 실행됩니다.
- snowpea 가 **판단할 수 없는** 코드는 어떤 모드든 "이 코드가 무엇을 바꾸는지 알 수 없습니다"라고 묻습니다. 실행 중에 대상이 정해지는 삭제·쓰기·복사·이동·`chmod`(`os.remove(path)`), 변수로 조합한 명령, 문법 오류로 읽을 수 없는 코드, `getattr`·`eval`·`exec`·계산된 import 로 닿는 호출, snowpea 가 읽을 수 없는 코드를 받는 인터프리터(`curl … | python3`, `python3 -` 만 있는 경우), 알려진 도구도 아니고 프로젝트에도 없는 모듈의 `python -m` 이 그렇습니다.

세션 전부터 프로젝트에 있던 스크립트는 프로젝트의 것입니다. 분명히 하는 일은 판단하지만(그 안의 차단 명령은 차단됨), 계산해서 정하는 부분을 문제 삼지는 않습니다. 이번 세션에서 에이전트가 쓴 스크립트는 세션이 끝날 때까지, 재시작 뒤에도 엄격하게 판단합니다.

이제 물어보게 되는 흔하고 해롭지 않은 예는 다음과 같습니다. 에이전트가 방금 쓴 스크립트가 인자로 받은 경로에 쓰는 경우(`open(args.out, "w")`), 조합한 명령으로 부르는 `subprocess.run(cmd, shell=True)`, `python -m http.server`, 에이전트가 방금 쓴 Makefile 이 make 변수를 쓰는 경우의 `make`.

### `security.unknownCode`

이 질문이 너무 많다면 `$SNOWPEA_HOME/settings.json` 에서 끌 수 있습니다(이 스위치가 들어간 앱 버전에서는 보안 화면에도 같은 스위치가 있습니다).

```json
{ "security": { "unknownCode": "allow" } }
```

기본값은 `"ask"` 입니다. `"allow"` 로 두면 snowpea 가 판단할 수 없는 코드도 세션 모드에 따라 다른 줄처럼 실행됩니다. 나머지는 그대로입니다. 차단 명령과 보호 폴더는 계속 차단되고, snowpea 자체 파일은 계속 보호되며, snowpea 자체 명령은 계속 물어봅니다. 로컬 연결의 사용자 본인만 바꿀 수 있고, 프로젝트는 바꿀 수 없습니다.

## 프로젝트 신뢰는 훅이 실행하는 것까지 고정합니다

프로젝트를 신뢰하면(`snowpea project trust` 또는 신뢰 카드) snowpea 는 프로젝트의 훅, MCP 서버, 명령, 에이전트, 프롬프트, 설정을 지금 상태 그대로 기록합니다. **그것들이 실행하는 프로젝트 파일**도 함께 기록합니다. 훅이 부르는 스크립트(`bash scripts/guard.sh`), 프로젝트 안에 있는 서버 프로그램, 스킬 폴더의 파일이 해당됩니다. 나중에 그중 하나가 바뀌면, 다시 확인하고 신뢰할 때까지 그 훅이나 서버는 꺼집니다.

카드의 "runs" 아래에 이 파일들이 나옵니다. **not pinned** 아래에는 snowpea 가 코드를 기록할 수 없는 명령이 이유와 함께 나옵니다.

| 이유 | 뜻 |
|---|---|
| `computed` | 명령에 변수나 치환이 있어, 무엇이 실행될지 실행할 때 정해짐 |
| `project-script` | `npm run …`, `make …`: 프로젝트의 스크립트 목록이 정하며 바뀔 수 있음 |
| `package-runner` | `npx …`, `uvx …`: 실행할 때 찾거나 내려받는 패키지 |
| `module` | `python -m …`: 실행할 때 인터프리터가 찾음 |
| `missing` | 지금 없는 프로젝트 파일을 가리킴 |
| `too-large`, `too-many`, `unreadable` | 기록할 수 있는 양을 넘거나 읽을 수 없는 파일 |

"not pinned" 이어도 프로젝트를 신뢰할 수 있습니다. 다만 그 명령은 보이는 줄까지만 신뢰가 적용되고, 그 줄이 실행하는 것은 snowpea 가 다시 묻지 않아도 바뀔 수 있다는 뜻입니다.

신뢰하는 것은 화면에서 본 그대로입니다. 목록을 본 뒤 클릭하기 전에 파일이 바뀌면 아무것도 신뢰하지 않고 목록을 다시 보여 줍니다. `snowpea project trust` 는 목록을 출력하고, 묻고, 정확히 그 내용을 신뢰합니다(`--yes` 는 질문을 건너뜁니다).

신뢰한 프로젝트 기록이 없어지거나 손상되면, 다시 신뢰할 때까지 어떤 프로젝트도 신뢰하지 않습니다. 세션을 열 때와 보안 화면에서 그 사실을 알려 줍니다.

## 한계

snowpea 를 거치는 모든 것에 적용됩니다: snowpea 자체 도구, 승인, 자비스, 예약 작업, snowpea 가 제어하는 외부 CLI 세션(권한 요청이 연결된 경우). 다음은 볼 수 없습니다.

- 프로그램이 실행 중에 정하는 것: 내려받은 내용, 파일 내용, 문자열 계산으로 만든 경로나 명령. snowpea 는 판단할 수 없다는 것을 알 때 묻지만, 항상 알 수 있는 것은 아닙니다.
- 지정한 곳에 쓰는 라이브러리(`numpy.save`, `DataFrame.to_csv`), 컴파일된 프로그램, snowpea 가 읽지 못하는 언어의 스크립트(에이전트가 쓴 경우에만 묻습니다).
- unrestricted 모드의 외부 CLI 세션, 자기 터미널(tmux)에서 권한 요청에 답하는 세션, 다른 터미널에서 실행 중인 세션. 세션 목록에 표시되며, 목록이 설정된 상태에서 그런 세션이 열리면 snowpea 가 경고합니다.

이 모든 것은 호출이 실행되기 전에 그 내용을 확인하는 것이지, 프로세스를 둘러싼 벽이 아닙니다. 확실한 보장이 필요하면 운영체제 수준의 보호도 함께 쓰세요: 별도 사용자, 읽기 전용 마운트, 파일 권한, 또는 [샌드박스 세션](sandbox.md).
