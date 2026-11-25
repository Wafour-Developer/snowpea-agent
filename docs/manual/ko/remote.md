# 원격 세션

예전의 원격 모드는 없어졌습니다. 다른 컴퓨터에서 세션 전체를 터미널로 돌리던 `snowpea --remote`와, 프로그램용 원격 데몬을 띄우던 `snowpea remote up`은 1.78.0에서 **원격 머신**으로 바뀌었습니다. 컴퓨터를 한 번 등록하면 그 컴퓨터의 snowpea 세션과 터미널이 이 컴퓨터의 것과 함께 사이드바에 나타납니다.

머신을 추가하고 연결해서 쓰는 방법, 데이터가 각각 어느 컴퓨터에 있는지, 보안 규칙은 [원격 머신 (beta)](machines.md)을 보세요.

바뀐 점:

- `snowpea --remote`는 더 이상 세션을 시작하지 않습니다. `snowpea machine add`를 안내하는 메시지를 출력하고 종료 코드 2로 끝납니다.
- `snowpea remote up`과 `snowpea-core remote up`은 데몬을 시작하지 않습니다. `"code":"removed"`가 든 JSON 오류 한 줄을 출력하고 종료 코드 2로 끝납니다.
- 데스크톱 앱의 원격 창 모드는 없어졌습니다. 그 컴퓨터를 머신으로 추가해서 쓰세요.
- 로컬 샌드박스는 원격 머신이 아닙니다. [`snowpea --docker`](sandbox.md)는 그대로입니다.

다른 언어: [English](../en/remote.md) · [모든 페이지](../README.md)

관련: [원격 머신 (beta)](machines.md) · [샌드박스 세션](sandbox.md) · [자비스 (beta)](jarvis.md#원격-머신) · [snowpea 웹](web.md)
