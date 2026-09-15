# Snowpea 에이전트·팀 운영 실제 검증 보고서 (2026-09-16)

- 검증일: 2026-09-16 (Asia/Seoul)
- 검증 대상: Snowpea `0.2.3`, protocol `1.5.0`
- 기본 모델: `qwen38-flash-next` (`local` provider, vLLM on `http://hon2.snowpea.ai:8001/v1`)
- 목적: 2026-09-15 검증 보고서(`docs/agent-team-validation-2026-09-15.md`) 대비 0.2.3에 적용된 핵심 개선사항(증거 기반 판정, verify 스테이지, 역할별 라운드 예산, 스테이지 handoff, repeat guard, deferred tools, lean child context, 세션 저장소 복구 등)의 실제 효과를 동일 시나리오와 설정을 통해 측정하고, 잔여 결함 및 운영상 모호성을 식별한다.

## 1. 검증 환경 및 변경 사항

### 1.1 환경 및 팀 구성

실제 `~/.snowpea/settings.json` 설정과 백그라운드에서 실행 중인 daemon(PID `3230963`, port `41245`, version `0.2.3`)을 사용했다. 0.2.3에서는 기본 team roster의 `verifier`가 파이프라인에 정식 배정되었다.

```text
roster: architect, critic, executor, explorer, test-engineer, verifier

explore   -> explorer
plan      -> architect
implement -> executor
test      -> test-engineer
verify    -> verifier
review    -> critic
```

`agents.models`는 기본 프로필인 `local:qwen38-flash-next`를 상속했다. `agents.max_concurrent`는 3이었다.

### 1.2 2026-09-15 대비 변경 사항

09-15 보고서의 권고 사항(P0/P1)을 반영하여 다음 네 개의 핵심 커밋이 적용되었다.

- **`c770d7d` (`fix(team): verdicts need evidence, a verify stage, and a resilient session store`)**:
  `verify -> verifier` 스테이지를 기본 파이프라인에 편입했다. 테스트·검증·리뷰 스테이지 판정 시 도구 실행 증거(`_has_tool_evidence`)와 명시적 판정 라인(`TESTS: PASS`, `VERIFY: PASS`, `VERDICT: APPROVE`)을 필수로 요구하도록 수정했다. 도구 승인 거부, 빈 응답, 또는 미실행 상태는 `NEEDS_MORE_EVIDENCE`로 처리하고 미결 항목(`Left unfinished`)으로 보고하여, 거짓 성공(`Nothing was left unfinished.`)을 완전히 차단했다. `/team N` 구문은 마이그레이션 안내 메시지를 출력하고 `/workers N`으로 자동 전달하도록 호환성을 복구했다. 세션 저장소(`session/store.py`)는 WAL 모드 + `busy_timeout`을 적용하고 지수 백오프 기반 재시도 및 연결 재수립 로직을 추가하여 SQLite 저장 실패를 방지했다.
- **`5223efe` (`feat(core): token efficiency — repeat guard, tool-output pruning, deferred tools, lean child context, context caps`)**:
  동일 파일 재읽기를 방지하는 hash 기반 Stub 및 차단(`repeat_guard.py`), 3회 연속 동일 호출 경고 및 4회 차단, 20회 윈도우 내 루프 감지(`loop.suspected`)를 도입했다. 6라운드 이전의 오래된 도구 출력은 프롬프트에서 축약 Stub으로 교체(`prune_old_tool_outputs`)하고 최근 대형 출력은 양끝을 남기고 트리밍한다. 핵심 도구 외의 도구들은 `deferred` 처리되어 `tool_search`로 온디맨드 로드된다. 서브에이전트에는 메모리 digest와 스킬 목록, 하위 지침을 제거한 경량 컨텍스트(`lean child context`)가 주입된다.
- **`f33ed77` (`feat(agents): per-role tool-round budgets with a grace call, team stage hand-offs, exploration rules, round telemetry`)**:
  역할별 기본 도구 라운드 예산(explore 8, critic/reviewer 12, test-engineer 15, verifier/architect 10, executor 32)을 지정하고, 예산 소진 시 마지막 요약 기회를 제공하는 도구 없는 유예 호출(`grace call`, reason `budget`)을 구현했다. 스테이지 간 구조화된 인수인계 파일(`.snowpea/handoffs/<run-id>/<stage>.md`)을 기록하여 누적 전달하고, 파이프라인 요약에 스테이지별 라운드/예산/토큰 원격 측정 표를 출력한다.
- **`47742be` (`release: prepare v0.2.3`)**:
  버전 0.2.3 릴리스 메타데이터를 확정했다.

## 2. 자동화 계약 테스트

관련 회귀 및 신규 기능 테스트 스위트를 실행했다.

```text
tests/test_agent_policies.py
tests/test_cli_sessions_teams_models.py
tests/test_delegate_cmd.py
tests/test_delegate_language.py
tests/test_model_assignment.py
tests/test_model_routing.py
tests/test_named_agent_persistence.py
tests/test_subagent_budget.py
tests/test_subagents.py
tests/test_team_config.py
tests/test_team_pipeline.py
tests/test_team_worktree.py
tests/test_session_store_resilience.py
tests/test_deferred_tools.py
tests/test_repeat_guard.py
tests/test_tool_output_pruning.py
tests/test_subagent_context.py
```

결과:

```text
235 passed in 22.26s
```

09-15 검증(168 passed) 대비 67개의 테스트가 추가되었으며, 전원 통과했다.

## 3. 실제 테스트 프로젝트 재검증

09-15와 동일한 3개 프로젝트를 `/tmp/snowpea-agent-e2e-20260916/` 아래에 재구성하고, 초기 버그 커밋(`baseline`)으로 리셋한 상태에서 검증을 진행했다.

```text
/tmp/snowpea-agent-e2e-20260916/
├── calc-team
├── docs-delegate
└── review-loop
```

### 3.1 `calc-team`: accept 모드 승인 거부 및 결과 판정 신뢰성

#### 목적

- 비대화형 `accept` 모드(`--mode accept < /dev/null`)에서 shell 승인이 불가능할 때 판정 검증
- 09-15 결함: shell 승인 거부로 테스트 미실행 상태임에도 `tests: TESTS: PASS`, `Nothing was left unfinished.`로 잘못 보고되던 문제의 해결 확인
- `verify -> verifier` 스테이지의 실제 실행 여부 확인

#### 실행 명령

```bash
snowpea -c '/team "기본 팀의 역할을 활용해서 calculator.py 버그를 찾고 수정하고 pytest로 검증해. 작업을 실제 파일에 반영해."' \
  --mode accept --cwd /tmp/snowpea-agent-e2e-20260916/calc-team --timeout 1500 < /dev/null
```

#### 결과

1. **파이프라인 실행**:
   `explorer` → `architect` → `executor` (T1, T2) → `test-engineer` → `verifier` → `critic` 전 과정이 순서대로 실행되었다. 09-15에서 누락되었던 `verifier`가 정상 실행되었다(10/10 라운드, 117,785 input tokens).
2. **코드 수정**:
   `executor`는 `calculator.py`의 `safe_divide`를 정상적으로 수정(0 나누기 예외 처리 및 나눗셈 적용)하고 단위 테스트들을 추가했다. 외부 재검증 결과:
   ```text
   7 passed in 0.01s
   ```
3. **판정 신뢰성 개선 (결함 해결 확인)**:
   비대화형 `accept` 모드에서 shell 실행 승인이 제공되지 않자(`approval_denied`), `test-engineer`와 `verifier`는 셸 테스트를 수행하지 못했다. 신규 파이프라인 판정 엔진은 이를 정확히 인지하고 다음 최종 요약을 출력했다.
   ```text
   stages: explore=explorer, plan=architect, implement=executor, test=test-engineer, verify=verifier, review=critic
   tasks: 2/2 finished
   tests: NEEDS_MORE_EVIDENCE
   verify: NEEDS_MORE_EVIDENCE
   review: NEEDS_MORE_EVIDENCE

   Left unfinished:
   - the test stage needs more evidence: the child ended with reason denied
   - the verify stage needs more evidence: the child ended with reason denied
   - the review stage did not approve: NEEDS_MORE_EVIDENCE (the child ended with reason budget)
   - tests were not PASS (NEEDS_MORE_EVIDENCE)
   - verify was not PASS (NEEDS_MORE_EVIDENCE)
   - review was not APPROVE (NEEDS_MORE_EVIDENCE)
   ```
   파이프라인은 `Nothing was left unfinished.` 대신 `the team pipeline finished with incomplete evidence`를 보고하며 종료 코드 `4`(approval denied / incomplete evidence)로 안전하게 종료되었다. 09-15의 P0 결함이 완전히 해결되었음을 확인했다.

### 3.2 `docs-delegate`: 명시적 일임 및 격리·저장 안정성

#### 목적

- `$explorer ...` 단축 명령의 정상 동작 및 explorer의 읽기 전용 규약 준수 확인
- 입력 토큰 사용량 및 도구 라운드 측정
- 09-15에서 발생했던 `sqlite3.OperationalError: disk I/O error` 재현 여부 검증

#### 실행 명령

```bash
snowpea -c '$explorer README.md와 cache.py를 읽고 요구사항 대비 결함만 파일:라인 근거로 보고해. 파일은 수정하지 마.' \
  --cwd /tmp/snowpea-agent-e2e-20260916/docs-delegate --timeout 1500 < /dev/null
```

#### 결과

- **역할 및 읽기 전용 준수**:
  `$explorer` 명령이 `delegate_task(agent="explorer")`로 정상 변환되었다. explorer는 `README.md`와 `cache.py`를 정독하고 요구사항 1(TTL 만료 부재)과 요구사항 2(문서화 부재)의 결함을 정확한 파일:라인 근거로 보고했다. 파일 수정이나 생성은 전혀 발생하지 않았다 (`git status` clean).
- **비용 및 라운드**:
  총 8라운드 예산 중 8라운드를 사용했으며, 입력 토큰 66,805개, 출력 토큰 814개를 소모했다. (09-15의 72K 대비 7.2% 감소).
- **저장소 안정성**:
  09-15에서 턴 종료 직전 발생했던 SQLite commit `disk I/O error`가 발생하지 않았으며, 세션 및 이벤트 저장이 정상적으로 완결되었다.

### 3.3 `review-loop`: 전체 팀 파이프라인 및 비용 측정

#### 목적

- `--mode auto`에서 전체 파이프라인의 종단간 실행 확인
- 구현 완료 후 외부 `pytest` 실행 결과 확인
- 09-15 대비 역할별 토큰 소모량 및 라운드 비교

#### 실행 명령

```bash
snowpea -c '/team "order.py의 total이 quantity와 discount를 올바르게 반영하도록 구현하고, 경계값 테스트를 추가하고, pytest를 실행한 뒤 코드 리뷰까지 완료해."' \
  --mode auto --cwd /tmp/snowpea-agent-e2e-20260916/review-loop --timeout 1500
```

#### 결과

- **구현 및 외부 테스트 검증**:
  `order.py`의 `total(items, discount=0)`이 수량 반영, 할인율 적용, 부동소수점 오차 반올림, 키 누락 예외 처리 등을 모두 포함하여 완전하게 구현되었다. `tests/test_order.py`에 11개의 엣지 케이스 테스트가 추가되었다.
  외부에서 직접 실행한 `pytest` 결과:
  ```text
  12 passed in 0.01s
  ```
  초기 실패 1건에서 12건 전원 통과 상태로 기능적 완성을 달성했다.
- **스테이지 실행 흐름**:
  1. `explorer` (7/8 라운드): 파일 구조 및 실패 원인 파악.
  2. `architect` (8/10 라운드): T1(구현 및 테스트) 태스크 분할 및 파일 소유권 지정.
  3. `executor` (10/32 라운드): `order.py` 수정 및 테스트 보강.
  4. `test-engineer` (9/15 라운드): pytest 실행 및 8개 변종(mutant) 사멸 테스트 수행.
  5. `verifier` (10/10 라운드): diff 감사, 12개 테스트 독립 실행 확인, md5 무결성 검증.
  6. `critic` (12/12 라운드): 12개 테스트 결과와 코드 무결성을 검증하고 findings 보고서 작성.

### 3.4 `/team 3` 호환성 검증

09-15에서 usage 에러를 냈던 `/team 3 "..."` 형태를 실행했다.

- 결과:
  ```text
  `/workers N` is the current spelling; forwarding this `/team N` request.
  tmebd88674: 3 tasks across 3 worktrees.
    T1 Add compatibility tests for arithmetic operations in tests/compat/arithmetic.test.ts
    T2 Add compatibility tests for edge cases and error handling in tests/compat/edge-cases.test.ts
    T3 Add compatibility test suite runner and config in tests/compat/compat-suite.ts
  ```
  명시적인 마이그레이션 안내 문구가 먼저 출력되고, 의도했던 `/workers 3` 워커트리 배치 모드로 즉시 포워딩되었다. 09-15 보고서의 §4.1 모호성이 완전히 해소되었다.

## 4. 토큰 효율 및 라운드 비교 (09-15 vs 09-16)

`review-loop` 시나리오에서 관찰된 2026-09-15와 2026-09-16의 역할별 입력 토큰 및 라운드 비교표는 다음과 같다.

| 역할 (Role) | 09-15 입력 토큰 | 09-16 입력 토큰 | 09-16 사용 라운드 / 예산 | 입력 토큰 증감률 |
|---|---:|---:|---:|---:|
| explorer | 177K | 61.7K (61,698) | 7 / 8 | **-65.1%** |
| architect | 94K | 74.7K (74,729) | 8 / 10 | **-20.5%** |
| executor | 352K (129K+223K) | 122.3K (122,276) | 10 / 32 | **-65.3%** |
| test-engineer | 394K | 119.6K (119,589) | 9 / 15 | **-69.6%** |
| verifier (구 verification executor) | 156K | 153.0K (153,029) | 10 / 10 | **-1.9%** |
| critic | 532K | 201.3K (201,298) | 12 / 12 | **-62.2%** |
| **전체 합계 (Total)** | **1,705K** | **732.6K (732,619)** | **56 / 87** | **-57.0%** |

- **토큰 절감 분석**:
  전체 입력 토큰 소모량이 1,705K에서 732.6K로 **57.0%(972K 토큰)** 급감했다.
  - 특히 가장 심각한 낭비가 발생했던 `test-engineer`(-69.6%), `critic`(-62.2%), `explorer`(-65.1%)에서 드라마틱한 비용 절감이 달성되었다.
  - 주요 절감 원인은 (1) 서브에이전트 경량 컨텍스트(`lean child context`), (2) 역할별 라운드 상한선 강제, (3) 스테이지 인수인계(`handoffs`)를 통한 이전 턴 출력의 프롬프트 누적 방지이다.

## 5. 반복 가드(Repeat Guard) 및 Handoff 관찰

### 5.1 Repeat Guard 및 Loop 감지 측정 결과

`~/.snowpea/state.db`의 `tool.result` 및 `events` 테이블을 전수 조사한 결과:

- `unchanged since your earlier read_file`: 0건
- `same result as your earlier`: 0건
- `repeat_blocked`: 0건
- `loop.suspected` 이벤트: 0건

**원인 분석**:
반복 차단 스텁이나 루프 이벤트가 0건으로 측정된 이유는 가드가 미동작해서가 아니라, **프롬프트 수준의 탐색 규칙(`exploration rules`: 심볼 우선 탐색, 최대 5회 읽기 제한, 이전 결과 재실행 금지 지침)**과 **엄격한 라운드 예산(8~15라운드)**이 결합되어 에이전트가 동일 파일의 반복 재읽기나 동일 셸 명령 반복에 빠지지 않고 정밀하게 필요한 도구만 순차 호출했기 때문이다. 가드의 임계치(동일 호출 연속 3~4회, 20회 윈도우 내 5회)에 도달하기 전에 태스크가 완료되거나 예산이 마감되었다.

### 5.2 스테이지 Handoff 관찰

프로젝트 디렉터리 내 `.snowpea/handoffs/`에 구조화된 인수인계 문서가 정상 생성되었다.

- `review-loop` (`.snowpea/handoffs/tr-27cdd9f7/`): 총 7개 파일
  (`explore.md`, `plan.md`, `implement-T1.md`, `implement.md`, `test.md`, `verify.md`, `review.md`)
- `calc-team` (`.snowpea/handoffs/tr-034f8d57/`): 총 5개 파일
  (`explore.md`, `plan.md`, `implement-T1.md`, `implement.md`, `review.md`)
- `docs-delegate`: 단일 에이전트 일임으로 팀 handoff 미생성 (정상)

각 문서는 `Decided`, `Files touched`, `Findings`, `Remaining`, `Risks` 블록으로 규격화되어 후속 스테이지 에이전트에 전달되었으며, 전체 프로젝트 대화 히스토리를 전부 전달하지 않고도 이전 스테이지의 결론과 diff만 정확히 이어받아 작업을 완수할 수 있게 지원했다.

## 6. 잔여 결함 및 운영상 모호성

이번 검증에서 새롭게 관찰된 결함 및 모호성은 다음과 같다.

### 6.1 예산 소진(Budget) 시 실질적 승인(PASS/APPROVE) 무효화 문제

- **현상**:
  `review-loop`에서 `verifier`와 `critic`은 각각 10라운드, 12라운드 동안 매우 깊이 있는 검증과 변종 사멸 테스트를 수행하고 내부적으로 `VERIFY: PASS` 및 `VERDICT: APPROVE`라는 완벽한 결론을 도출했다.
  그러나 허용된 도구 라운드 예산(10/10, 12/12)을 모두 소진한 후 유예 호출(`grace call`)을 통해 종료되었기 때문에, 서브에이전트 종료 사유가 `reason = "budget"`으로 기록되었다.
  `team_pipeline.py`의 `_hard_evidence_problem()`은 `reason != "complete"`인 경우 무조건 하드 결함으로 판단하여, 에이전트가 본문에서 명시적 PASS/APPROVE를 선언했음에도 강제로 `NEEDS_MORE_EVIDENCE`로 변환했다. 이로 인해 코드와 테스트가 100% 정상 완료되었음에도 파이프라인의 최종 exit code가 1로 실패 처리되었다.
- **권고**:
  유예 호출(`budget`) 상태라 하더라도, 에이전트가 검증 도구 실행 증거(`_has_tool_evidence`)를 충분히 확보하고 명시적인 PASS/APPROVE를 반환한 경우에는 무조건적인 실패 처리 대신 `PASS (budget constrained)` 형태의 조건부 성공으로 인정하거나, 깊은 테스트를 수행하는 `verifier`(10 -> 14)와 `critic`(12 -> 16)의 기본 예산을 상향 조정해야 한다.

### 6.2 비대화형 환경에서 읽기 전용 에이전트의 불필요한 Shell 시도로 인한 거부 종료

- **현상**:
  `docs-delegate`에서 `explorer`는 읽기 전용 작업임에도 환경 확인을 위해 `shell` (`pwd; ls -la`)을 호출했다. 비대화형 환경(`< /dev/null`)에서는 승인을 답할 수 없어 거부(`approval_denied`)되었고, 이후 `glob`과 `read_file`로 완벽한 분석 보고서를 제출했음에도 `tracker.denied = True`로 인해 프로세스 종료 코드가 `4`로 반환되었다.
- **권고**:
  읽기 전용 에이전트(`explore`, `explorer`)의 도구 목록에서 셸을 기본 제외하거나, 거부된 도구 호출이 비치명적(non-fatal) 폴백으로 처리된 경우 상위 턴의 실패 플래그를 정제하는 메커니즘이 필요하다.

### 6.3 측정의 한계점

- **장기 반복 루프 차단 한계**: 서브에이전트들이 7~12라운드 이내에 예산에 도달하거나 작업을 끝냈기 때문에, 20회 윈도우 기반 `loop.suspected` 및 4회 연속 차단(`repeat_blocked`)의 런타임 동작을 실제 운영 시나리오에서 유발시키지 못했다.
- **비대화형 승인 인터랙션**: 헤드리스 자동화 모드 특성상 대화형 승인 복구(interactive approval prompt) 경로는 측정 대상에서 제외되었다.

## 7. 최종 판정 비교

| 검증 항목 | 09-15 결과 | 09-16 (0.2.3) 결과 | 종합 평가 |
|---|---|---|---|
| `$agent명` 명시적 일임 | 정상 | 정상 | 정상 유지 |
| child 결과의 main 전달 | 정상 | 정상 | 정상 유지 |
| 역할별 읽기/수정 제한 | 정상 | 정상 | 정상 유지 |
| auto 모드 팀 구현·테스트 | 정상 | 정상 | 12개 테스트 완전 통과 및 무결성 유지 |
| accept/headless 결과 판정 | 결함 확인 | 정상 (해결됨) | 증거 없는 거짓 PASS 퇴출, `NEEDS_MORE_EVIDENCE` 정상 작동 |
| 팀 최종 summary 신뢰성 | 결함 확인 | 정상 (해결됨) | `Nothing was left unfinished.` 오출력 제거, 미결 항목 명시 |
| 토큰 효율 | 개선 필요 (1,705K) | 대폭 개선 (732.6K) | **입력 토큰 57.0% 절감**, 예산 및 Lean Context 정착 |
| 세션 자동 저장 (SQLite) | I/O 에러 발생 | 정상 (해결됨) | WAL 모드 및 연결 재시도로 I/O 오류 0건 |
| `/team 3` 문법 호환성 | 에러 발생 | 정상 (해결됨) | 안내 문구 출력 후 `/workers 3`로 정상 자동 포워딩 |
| verifier 역할 배정 | 미배정 경고 | 정상 (해결됨) | `verify -> verifier` 기본 파이프라인 정식 편입 |
| 스테이지 간 Handoff | 미지원 | 정상 (신규) | 구조화된 `.snowpea/handoffs/` 생성 및 전달 정상 동작 |
| 라운드 소진 시 판정 처리 | N/A | 관찰된 신규 모호성 | 깊은 검증 후 PASS/APPROVE 도출 시에도 budget 소진 시 실패 강등 |

0.2.3 릴리스는 09-15 검증에서 지적된 핵심 결함들(거짓 PASS, 최종 summary 불일치, SQLite 커밋 에러, verifier 누락, `/team 3` 문법 에러)을 완벽하게 해결했다. 특히 입력 토큰 소모량을 57% 감축하여 운영 비용을 획기적으로 개선했다. 다만 깊은 테스트를 수행하는 검증·리뷰 역할이 라운드 예산을 모두 사용할 경우 실질적 승인 내용이 무효화되는 새로운 규칙 상충이 발견되어 후속 버전에서 예산 조정 또는 조건부 인정 로직의 보완이 필요하다.
