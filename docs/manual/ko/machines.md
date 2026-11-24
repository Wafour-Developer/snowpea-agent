# 원격 머신

snowpea는 snowpea가 설치된 다른 PC로 SSH 연결을 여러 개 유지할 수 있습니다. IDE, 웹, CLI에서 그 컴퓨터의 세션과 터미널을 로컬 데몬을 통해 열지만, 실제 작업은 원격 PC에서 돌아갑니다.

## 머신 등록

IDE 사이드바 **Machines → Add machine**에서 `user@host[:port]`, 라벨, 그리고 개인 키 파일 경로 또는 붙여 넣은 키를 입력합니다. 붙여 넣은 키는 `$SNOWPEA_HOME/machines/<id>/id_key`에 저장되며(모드 0600) 이 컴퓨터 밖으로 나가지 않습니다.

데몬은 호스트 키 신뢰(첫 접속 시)와, 필요하면 원격에 snowpea 설치를 묻습니다. `snowpea remote up`과 같은 흐름입니다.

## 사용

머신이 연결되면 사이드바에 그 PC의 프로젝트 폴더가 보입니다. CLI 세션을 열 때 로컬과 같은 세션 목록·권한 선택을 씁니다. 터미널은 원격 데몬이 제공하고 로컬에서 프록시합니다.

허용된 RPC에 `machine: "<id>"`를 넣습니다(`workspace.list`, `session.*`, `external.*`, `terminal.*`). `machine`을 보내지 않는 예전 클라이언트는 이 컴퓨터만 봅니다.

## 설정

- `web.remote.machines` — 켜면 원격 웹 로그인도 이 컴퓨터에서 이미 연결된 머신을 쓸 수 있습니다(기본 꺼짐).
- 머신마다 **Jarvis allowed** 스위치가 있습니다(기본 꺼짐). Jarvis가 원격 터미널을 만들거나 입력할 때 필요합니다.

## CLI

```bash
snowpea machine list
snowpea machine add user@host
snowpea machine connect <id>
snowpea machine disconnect <id>
snowpea machine remove <id>
```

창 전체가 원격으로 바뀌는 모드(`snowpea --remote`)는 [remote](remote.md)를 보세요. 동작은 그대로입니다.