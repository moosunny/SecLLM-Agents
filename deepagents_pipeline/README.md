# deepagents_pipeline

deepagents 기반 fix-suggestion 파이프라인의 실험 하네스 (완전형 안, 설계 §9).
설계 문서: `../docs/deepagents_pipeline_design.md`. 메인 프로젝트와 격리된 uv
프로젝트이며, 접점은 ① `repo_paths.py`의 verify_fix 경로 import ② eval 러너의
격리 venv 인터프리터 기동뿐이다.

외부 공유용 파이프라인 개요: [`docs/pipeline_overview.md`](docs/pipeline_overview.md)

## 실행

```bash
cd deepagents_pipeline && uv sync          # deepagents==0.7.4 정확 핀
uv run pytest -q                           # LLM-free 테스트 (126건)

# 배치 러너 (eval 러너가 이 형태로 기동)
.venv/bin/python -m deepagents_pipeline.runner \
  --input ../tests/experiment_inputs/java/test.jsonl \
  --output out.jsonl --indices 0,1 \
  --base-url https://api.anthropic.com/v1 \
  --model-override claude-haiku-4-5-20251001
```

API key는 요청 `APIKEY` > `LITELLM_MASTER_KEY` 환경변수 > `"EMPTY"` 순.

## 모듈

| 모듈 | 역할 | 설계 근거 |
|---|---|---|
| `contracts.py` | `agents.schema` wire-format 미러 + 오류 계약 (E102/E103/E104/E202/E301/E999) | §5 |
| `verifier.py` | `check_candidate` 래핑, `{code, stage, message}` 분류 + 영역 에코 | §6 |
| `prompts.py` | fix-suggester.md 도메인 규칙 이관 + read_file 페이지네이션 + edit_code 규칙 | §10, §4 |
| `harness.py` | `create_deep_agent` + StateBackend 가상FS + `edit_code`/`submit_fix` + diff 후처리 + 구조적 종료 | §9 개정 |
| `models.py` | registry 미러 모델 팩토리 (openai-only, 샘플링 카브아웃, reasoning 보존) | §3, §5.1 |
| `runner.py` | JSONL 배치 진입점, 레코드 단위 오류 강등, §12(b) trace 출력 | §12 |

## 알려진 편차 (의도적 보류)

- **편집 인터페이스가 설계 §9와 다름**: 설계는 `write_file`(candidate.json) 기반이나,
  2026-08-10 A/B(동일 5건: deepseek in −10%/out −32%/wall −65%, qwen 4/5→3/5 악화)
  결과를 고지한 뒤 사용자 결정으로 `edit_code(old_string, new_string)` + diff 후처리를
  기본값으로 교체했다. codeModifications는 `mods_from_diff`가 생성(`MERGE_GAP` 미만 갭
  병합 → V201 구조적 배제), 연속 매칭 실패는 `MAX_CONSECUTIVE_EDIT_FAILURES=5`에서
  종료. 설계 문서는 미개정 — §12 매트릭스 해석 시 arm 간 편집 인터페이스 차이를
  처치 변수로 계상해야 한다.

- **multi-site 정책이 꺼져 있고 `MERGE_GAP`이 5임 (설계 §6과 다름, 2026-08-25)**:
  설계는 `MIN_MULTI_SITE_GAP=30`을 VerifierTool 하드 검증으로 두고 `MERGE_GAP`을 거기
  맞췄으나, 30줄 병합이 hunk 사이의 미변경 줄까지 delete+add 스팬에 흡수해 315건 중
  64건에서 555줄을 "수정과 무관한 삭제"로 표기했다(적용 결과는 동일 — 표기 문제).
  `MERGE_GAP=5`로 낮추고 `verify_candidate`는 `enforce_multi_site_policy=False`로
  호출한다(V202/V203 비활성, 나머지 스키마·구조·적용가드·문법 검증은 유지). 실측
  delete 스팬 총합 1009 → 590줄, 검증 실패·적용 불일치 0/315. 공백뿐인 조각은 add로
  표현할 수 없어(V108) 인접 equal 줄 하나만 붙여 넓힌다.

- **V401 메시지에 파스 에러 위치·문맥 첨부 (2026-08-26, verifier.py 어댑터 한정)**:
  `check_candidate`는 에러 개수 차이만 보고해 모델이 같은 V401로 반복 실패했다.
  5-run 재구성(39개 V401 시도)에서 missing 노드의 기대 토큰(`;`, `}`, `identifier`)이
  버려지고 24~36줄 try ERROR의 끝 문맥이 없던 것이 원인이라, 수정본을 판정과 같은
  문법으로 재파싱해 줄번호순 최대 3곳(누락 토큰 / ERROR 첫 토큰 텍스트)과 ±3줄
  문맥(긴 span은 시작·끝 분리, 적용 영역과 중복 시 생략, 총 40줄 상한)을 message에
  덧붙인다. `verify_fix.py`는 무변경, 판정 불변, 보강 실패 시 fail-open.
- **도구 호출 없는 턴 재촉 (2026-08-27, `HarnessControlMiddleware.after_model`)**: create_agent는
  tool_calls가 빈 AIMessage를 최종 답변으로 보고 종료한다. 27b train 6057건의 agent_stop
  8건이 전부 이 경로(빈 텍스트·새어 나온 `<tool_call>` 토큰·퇴화 출력)였고 4건은 체커 표준
  수정이 작업본에 이미 있었다. 미제출·시도 잔여 상태에서 그런 응답이 오면 HumanMessage로
  submit_fix를 재촉하고 `jump_to="model"`로 되돌린다(`MAX_NO_TOOL_NUDGES=2`, 초과 시
  기존대로 agent_stop). 하네스 합성 종료·PASS·예산 소진·편집 실패 상한은 재촉하지 않는다.
  trace에 `nudges` 계측 추가. **주의**: after_model 훅은 턴당 그래프 스텝을 2→3으로 늘린다 —
  `MAX_AGENT_STEPS`를 50 고정으로 두면 recursion_limit이 17턴에서 걸린다(08-27 전수 실행에서
  발견, 두 모델 모두 recursion_limit 전부 17턴). 지금은 `MAX_MODEL_TURNS(25) ×
  GRAPH_STEPS_PER_TURN(3)`으로 계산하며 회귀 테스트가 25턴을 고정한다.
- **언어 범위가 java 전용에서 `SUPPORTED_LANGUAGES={java, javascript, typescript, go, c, cpp, python}`로 확장 (2026-09-16,
  설계 §6(c)와 다름)**: `harness.run_fix_with_trace` 입구 게이트가 `chunking.resolve_declared_language`
  (모르는 확장자·이름/확장자 불일치는 E103)와 위 집합으로 판정한다. 루프·도구·검증기는
  무변경 — `verify_fix._GRAMMAR_MODULES`가 이미 언어별 파서를 갖고 있어 V401은 해당 언어
  문법으로 판정된다. 타겟 청크 코드펜스도 하드코딩 ```java에서 정규화 언어명으로 바뀌었다.
  근거: `train/{javascript,go}/sft_training_set_20260904.jsonl`(1494/1252건) 전건 계약·청킹
  통과, A100 qwen3.8-27b 스모크 각 1건 PASS. TypeScript/C/C++는 2026-09-15
  평가 코퍼스 실행을 위해 동일 실행·검증 경로를 활성화했다. Python도 2026-09-16
  동일 경로를 활성화했으며, 실제 모델을 통한 코퍼스 생성 검증은 별도로 필요하다.
- **line-ending 보존 없음**: 프로덕션 `convert_to_line_ending` 미적용. 현 코퍼스
  0/343 CRLF — CRLF 소스 지원 시 추가 필요.
- **contract-drift 자동 테스트 없음**: `contracts.py`는 손 미러. `agents.schema`
  import는 격리 venv에서 pydantic 버전 우연 일치에 의존하므로 의도적으로 제외.
- **오류 envelope의 timings는 0**: 프로덕션과 동일 동작.
- **guards import 결합**: `repo_paths.py` docstring 참조 — `agents/` 패키지 루트에
  서드파티 의존성이 생기면 파일 단위 로드로 전환.
