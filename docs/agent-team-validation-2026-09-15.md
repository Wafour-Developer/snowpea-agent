# Snowpea 에이전트·팀 운영 실제 검증 보고서

- 검증일: 2026-09-15~16 (Asia/Seoul)
- 검증 대상: Snowpea `0.2.2`, protocol `1.5.0`
- 기본 모델: `qwen38-flash-next` (`local` provider)
- 목적: 실제 사용자 설정을 사용해 에이전트 명시 일임, 팀 파이프라인, 구현·테스트·리뷰 handoff를 검증하고 운영상 모호성과 결함을 식별한다.

## 1. 검증 환경

실제 `~/.snowpea` 설정과 실행 중인 daemon을 사용했다. 설정상 기본 팀 roster와 stage mapping은 다음과 같았다.

```text
roster: architect, critic, executor, explorer, test-engineer, verifier

explore   -> explorer
plan      -> architect
implement -> executor
test      -> test-engineer
review    -> critic
```

`agents.models`는 비어 있어 모든 에이전트가 기본 모델을 상속했다. `agents.max_concurrent`는 3이었다.

현재 검색되는 에이전트는 다음과 같았다.

```text
default, architect, critic, executor, explore, explorer,
reviewer, test-engineer, verifier,
csat-item-writer-sonnet, korean-english-item-writer,
openai, qwen-tts, stock-expert
```

마지막 다섯 에이전트는 `~/.claude/agents`에서 발견됐지만 CLI 결과에서는 `source: project`로 표시됐다.

## 2. 자동화 계약 테스트

다음 에이전트·팀 관련 테스트를 실행했다.

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
```

결과:

```text
168 passed, 1 warning
```

경고는 non-async 테스트에 `asyncio` marker가 붙은 테스트 코드 문제였다.

## 3. 실제 테스트 프로젝트

서로 다른 운영 목적을 확인하기 위해 다음 세 프로젝트를 생성했다.

```text
/tmp/snowpea-agent-e2e-20260915/
├── calc-team
├── docs-delegate
└── review-loop
```

### 3.1 `calc-team`: 기본 팀 구현 파이프라인

#### 목적

- explorer → architect → executor → test-engineer → critic 순서 확인
- 코드 수정과 테스트 실행 확인
- 비대화형 `accept` 모드의 승인 처리 확인

#### 결과

팀은 잘못된 나눗셈 구현을 수정했다. 외부에서 다시 실행한 결과는 다음과 같다.

```text
9 passed in 0.01s
```

그러나 비대화형 `accept` 모드에서는 shell 승인을 응답할 client가 없어 test와 review 도구 호출이 거부됐다. 그럼에도 최종 요약은 다음처럼 표시됐다.

```text
tests: TESTS: PASS
review: NO_VERDICT
Nothing was left unfinished.
```

이는 실제 증거와 반대되는 결과다.

#### 확인된 원인

`core/snowpea_core/agent/team_pipeline.py`에서 다음 판정이 사용된다.

```python
run.tests = TESTS_PASS if result.ok and TESTS_FAIL not in text.upper() else TESTS_FAIL
```

따라서 delegate 호출 자체가 성공하고 응답에 `TESTS: FAIL`이 없으면, 빈 응답도 PASS가 된다.

review는 명시적 verdict가 없어도 `NO_VERDICT`로 반환된 뒤 파이프라인이 종료된다. 이후 `unfinished`가 비어 있으면 `Nothing was left unfinished.`가 출력된다.

### 3.2 `docs-delegate`: `$agent명` 명시적 일임

#### 목적

- `$explorer ...` 단축 명령 확인
- 읽기 전용 agent의 역할 준수 확인
- main agent로 결과가 다시 전달되는지 확인

#### 결과

- `$explorer`가 `delegate_task(agent="explorer")`로 정상 변환됐다.
- explorer는 README와 구현 파일을 읽고 누락된 동작을 정확히 보고했다.
- explorer는 파일을 수정하지 않았다.
- main agent가 결과를 받은 뒤 파일과 git 상태를 다시 확인했다.

따라서 명시적 agent 선택, child-to-main 결과 전달, 역할 제한은 정상 동작했다.

#### 발견된 문제

작은 파일 두 개를 조사하는 데 explorer가 약 72K input token을 사용했다. 또한 한국어 응답에 일본어·중국어 표현이 일부 혼입됐다.

최종 응답을 저장하는 중 다음 오류가 발생해 turn이 실패했다.

```text
sqlite3.OperationalError: disk I/O error
```

로그상 실패 경로:

```text
agent/loop.py
  -> session/manager.py:emit
  -> session/store.py:append_event
  -> session/store.py:_execute
  -> sqlite connection commit
```

이후 실행한 `PRAGMA quick_check` 결과는 `ok`였고 디스크 공간도 충분했다. 영구 DB 손상보다는 일시적인 commit 또는 연결 복구 문제일 가능성이 높다.

### 3.3 `review-loop`: 전체 구현·테스트·리뷰

#### 목적

- `--mode auto`에서 전체 팀 파이프라인 확인
- 구현 작업 분할과 순차 handoff 확인
- test-engineer의 테스트 보강 확인
- critic의 최종 verdict 확인

#### 결과

다음 단계가 모두 실행됐다.

1. explorer가 현재 구현과 실패 원인을 조사했다.
2. architect가 세 개 작업으로 분할했다.
3. executor가 quantity와 discount 계산을 구현했다.
4. 별도 executor가 경계값 테스트를 작성했다.
5. test-engineer가 mutation 관점의 테스트를 추가했다.
6. critic이 변경을 검토해 `VERDICT: APPROVE`를 반환했다.

외부 재검증 결과:

```text
15 passed in 0.01s
```

파이프라인 exit code는 0이었다. 따라서 auto 모드에서 구현·테스트·리뷰 handoff는 기능적으로 정상이다.

#### 토큰 효율

작은 2파일 프로젝트에서 관찰된 대략적인 input token 사용량은 다음과 같다.

| 역할 | Input tokens |
|---|---:|
| explorer | 177K |
| architect | 94K |
| executor T1 | 129K |
| executor T2 | 223K |
| verification executor | 156K |
| test-engineer | 394K |
| critic | 532K |

결과 품질은 좋았지만 작업 크기에 비해 비용이 과도했다. 특히 critic과 test-engineer가 동일 파일과 shell 명령을 반복 확인했다.

## 4. 운영상 모호성

### 4.1 `/team 3` 명령 호환성

기존 형태인 다음 명령은 현재 usage 오류를 반환한다.

```text
/team 3 "task"
```

현재 기능은 다음처럼 분리돼 있다.

```text
/workers 3 "task"
/team "task"
/team <team-name> "task"
```

기존 사용자를 위해 alias 또는 구체적인 migration 안내가 필요하다.

### 4.2 중복돼 보이는 builtin 역할

다음 역할 쌍의 차이가 이름만으로는 명확하지 않다.

- `explore` / `explorer`
- `critic` / `reviewer`

자동 일임과 수동 agent 선택 시 역할 선택이 모호해진다. 하나를 alias로 처리하거나 설명과 권한 차이를 명확히 표시해야 한다.

### 4.3 사용되지 않는 verifier

`verifier`는 기본 team roster에 있지만 어떤 stage에도 배정되지 않는다. 실제 실행 시에도 다음 메시지가 출력됐다.

```text
not used by any stage: verifier
```

별도 `verify` stage를 만들거나 기본 roster에서 제거해야 한다.

### 4.4 외부 agent source 표시

`~/.claude/agents`에서 발견한 agent가 `project` source로 표시된다. 실제 출처를 `claude-global`, `global-import` 또는 절대 경로 형태로 표현해야 관리가 쉽다.

## 5. 개선 권고

### P0: 정확성과 데이터 안정성

1. **test verdict를 명시적으로 파싱한다.**
   - 정확한 `TESTS: PASS`가 있을 때만 PASS로 처리한다.
   - 빈 응답, 명령 실행 불가, 형식 불일치는 FAIL 또는 `NEEDS_MORE_EVIDENCE`로 처리한다.

2. **review verdict를 필수로 만든다.**
   - `APPROVE`만 성공으로 처리한다.
   - `NO_VERDICT`, 빈 응답, `NEEDS_MORE_EVIDENCE`는 unfinished로 기록한다.

3. **최종 요약과 실제 실행 상태를 일치시킨다.**
   - test/review 증거가 없으면 `Nothing was left unfinished.`를 출력하지 않는다.
   - stage 실패, approval 거부 및 process exit code를 최종 결과에 반영한다.

4. **SQLite event 저장을 복구 가능하게 만든다.**
   - 제한된 retry/backoff를 적용한다.
   - commit 실패 시 연결 재생성을 검토한다.
   - WAL 및 busy timeout 설정을 검토한다.
   - 저장 실패 때문에 이미 생성된 최종 모델 응답 전체가 사라지지 않도록 buffer/replay 경로를 둔다.

### P1: 운영 품질과 비용

5. **agent별 tool-round와 token budget 기본값을 제공한다.**
   - explorer는 읽기 전용 작업에 작은 round 제한을 적용한다.
   - 동일 명령이나 동일 파일 반복 접근을 감지한다.
   - budget 임계점에서 현재 증거를 요약하고 종료하도록 한다.

6. **기본 팀 stage 구성을 정리한다.**
   - `verify -> verifier` stage를 추가하거나 verifier를 기본 roster에서 제거한다.

7. **중복 역할을 정리한다.**
   - `explore`/`explorer`, `reviewer`/`critic`을 alias 처리하거나 책임 차이를 문서화한다.

8. **기존 `/team 3` 문법을 호환한다.**
   - `/workers 3`로 forwarding하거나 정확한 대체 명령을 제시한다.

9. **응답 언어 일관성을 검사한다.**
   - 한국어 세션의 최종 응답에서 불필요한 일본어·중국어 혼입을 정규화한다.

## 6. 최종 판정

| 검증 항목 | 결과 |
|---|---|
| `$agent명` 명시적 일임 | 정상 |
| child 결과의 main 전달 | 정상 |
| 역할별 읽기/수정 제한 | 정상 |
| auto 모드 팀 구현·테스트·리뷰 | 정상 |
| accept/headless 결과 판정 | 결함 확인 |
| 팀 최종 summary 신뢰성 | 결함 확인 |
| 토큰 효율 | 개선 필요 |
| 세션 자동 저장 | 일시적 commit 실패 확인 |
| 자동 agent 선택의 명확성 | 개선 필요 |

기능의 핵심인 agent 일임과 팀 handoff는 동작한다. 그러나 실행되지 않은 테스트를 PASS로 처리할 수 있는 현재 판정 로직은 결과 신뢰성을 훼손하므로 가장 먼저 수정해야 한다. 그 다음으로 SQLite 저장 복구와 agent별 비용 제한을 적용하는 것이 적절하다.
