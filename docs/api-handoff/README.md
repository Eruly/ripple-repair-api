# RippleRepair API handoff

재무 리서치 Markdown을 보내면 영업이익 전망의 자릿수 오류와 그 값을 재인용한 문장을
교정해 `corrected_markdown`으로 반환하는 API입니다. 서버의 원본 파일은 쓰거나 덮어쓰지 않습니다.

## 빠른 시작

```bash
uv sync
uv run uvicorn web_app.main:app --host 0.0.0.0 --port 8200
```

- 대화형 API 문서: `http://localhost:8200/docs`
- Markdown 파일 교정 Web UI: `http://localhost:8200/forecast-correction`
- OpenAPI 원문: `http://localhost:8200/openapi.json`
- 상태 점검: `GET http://localhost:8200/api/status`

> 현재 애플리케이션은 API 키 인증을 강제하지 않습니다. 외부 공유 시에는 사설망·VPN
> 또는 API gateway의 인증과 TLS 뒤에 배치하세요. 요청·응답에 보고서 원문이 포함되므로
> 본문 로그 마스킹과 보존 기간도 사전에 정해야 합니다.

## 비개발자 데모: Markdown 파일로 실행

1. `http://localhost:8200/forecast-correction`을 엽니다.
2. `.md`, `.markdown`, `.txt` 파일을 선택하거나 Markdown 본문을 붙여넣습니다.
3. 컨센서스는 비워 자동 추출합니다. `Fast`는 전체 atom/교정 범위를 유지하면서 다국어 embedding으로 관계 후보 pair를 줄이고, `LLM 정밀`은 전체 후보를 사용합니다.
4. 원문/교정본과 검토 항목을 확인한 뒤 교정 Markdown을 복사하거나 다운로드합니다.

파일 선택은 브라우저의 로컬 읽기이며 별도 파일 업로드 API를 거치지 않습니다. 교정 요청에는
읽은 Markdown 텍스트가 JSON으로 포함되고, UI는 응답의 `corrected_markdown`만 내려받습니다.
선택한 로컬 파일과 서버 원본은 수정하지 않습니다.

외부 Quick Tunnel에서는 30B reasoning 모델의 FactReasoner LLM 요청이 오래 걸릴 수 있습니다.
Web UI와 개발자 API의 기본 `graph_mode`는 `llm`이며, `fast`를 지정해도
FactReasoner 판단·교정·검토는 LLM으로 실행됩니다.

Web UI에서 `LLM 정밀`을 선택하면 비동기 job API를 사용합니다. 제출 응답은 즉시 `202`와
`job_id`를 반환하고, UI가 상태를 polling한 뒤 완료된 기존 교정 결과를 렌더링합니다.
고강도 reasoning으로 JSON이 잘리지 않도록 개별 atom 판정·문장 교정·최종 승인 검토에는
4096 token 출력 예산을 사용합니다. Muse Glimmer는 llama.cpp의 `n_gpu_layers=all` 및 Flash
Attention 설정으로 GPU에서 실행되며, 긴 문서는 수십 초~수분이 걸릴 수 있어 Web UI polling
창은 job 보관 시간에 맞춘 60분입니다.

각 FactReasoner 호출은 `response_format=json_schema`로 역할별 응답 형식(`atom_judgment`,
`rewrite`, `propagation`, `approval`)을 전달합니다. 서버가 schema 모드를 지원하지 않으면
`json_object`로 한 번 재시도하며, 파서는 코드펜스·앞뒤 설명·잘못된 백슬래시를 제거하고
완전한 JSON object만 추출합니다. 필수 필드가 없거나 `finish_reason=length`로 잘린 응답은
한 번 더 compact JSON으로 요청하고, 그래도 실패하면 해당 교정을 적용하지 않고
`factreasoner.manual_review`에 남깁니다.

최종 승인 검토는 전체 원문/교정본을 다시 보내지 않습니다. 실제로 변경된
`original_text → corrected_text` 쌍과 각 변경 주변의 제한된 문맥, 관련 atom 근거만 비교하며,
적용된 변경이 없으면 검토 LLM 호출을 생략합니다.

`97조1467억원`처럼 조원과 억원이 결합된 금액은 단일 atom으로 처리합니다. 반대로
`49%2063억원`처럼 손상된 단위 표현은 자동 교정하지 않고 `manual_review`로 남깁니다.

컨센서스 추출과 배율 계산은 결정론적이고, 그 결과를 포함한 FactReasoner atom 판단·문장 교정은
LLM이 수행합니다. `/correct` 순서는 scale 블록 → 같은 금액 재인용(literal ripple) →
FactReasoner cascade/atom → 산술 가드(`상반기+하반기=연간`) → LLM 재검토입니다.
재검토는 톤·근거용이며, 합이 맞는 scale 블록과 이미 적용된 atom/cascade를 산술 이유로
되돌리지 않습니다. 합이 깨진 scale만 원문으로 롤백합니다. 잔차 > 25% 또는 재검토 거부는
`manual_review`로 남기되 제안 Markdown은 유지합니다.
`review_applied_corrections: false`는 호환용으로 검토를 끌 수 있지만 기본값(`true`)을 권장합니다.

자동 forecast correction의 FactReasoner는 atom/문장마다 개별 호출하지 않습니다. Fact Atom Graph
partition당 최대 128개 정도의 atom을 한 사실 판단 turn으로 묶고, `correct` 판정된 atom만 다시
partition 단위 교정 turn으로 보냅니다.
각 turn은 id-indexed JSON을 반환하므로 일부 결과가 누락되면 해당 atom만 수동 검토로 남습니다.
이때 묶음은 단순한 입력 순서가 아니라 Fact Atom Graph의 연결 component/tree와 같은 source
chunk를 우선합니다. 큰 tree는 BFS 순서로 최대 128개 partition으로 분할하고, 직렬화 payload가
길면 같은 tree 안에서만 context 예산에 맞춰 추가 분할합니다. 서로 연결되지 않은 tree는 같은
turn에 섞지 않습니다. 각 partition에는 경계 연속성을 위해 첫 atom의 직전 부모 1개와 마지막
atom의 직후 자식 1개를 있으면 추가합니다. 이 두 atom은 `is_boundary_context=true`인
참조 전용 노드라서 fact-check turn의 비교 기준으로만 사용되고 자동 교정에서는 제외됩니다.
graph edge의 부모/자식이 충돌하면 LLM은 하위/자식 atom을 문제 후보로 판단하며, 경계로 추가된
노드의 문제는 자동 적용하지 않습니다.
독립 `/api/fact-graph/judge-atom`과 `/api/fact-graph/propagate-correction` 계약은 호환성을 위해
기존 단일 호출 방식으로 유지됩니다.

## 원격 서버에서 계속 실행하기

현재 저장소의 `ops/systemd/`에 사용자 systemd unit 예시가 있습니다. 설치 후에는 FastAPI와
Cloudflare 터널이 각각 실패 시 자동 재시작되고, 사용자 `linger`가 켜져 있으면 로그아웃 뒤에도
서비스가 유지됩니다.

```bash
mkdir -p ~/.config/systemd/user
cp ops/systemd/ripple-repair-webui.service ~/.config/systemd/user/
cp ops/systemd/ripple-repair-cloudflared.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ripple-repair-webui.service ripple-repair-cloudflared.service
systemctl --user status ripple-repair-webui.service ripple-repair-cloudflared.service
```

Quick Tunnel은 프로세스가 다시 시작될 때 접속 URL이 바뀔 수 있습니다. 고정 도메인이 필요하면
Cloudflare Named Tunnel로 전환하세요.

## 권장: Markdown 입력 → 교정 Markdown 반환

`POST /api/forecasts/operating-profit/correct`가 일반 연동용 단일 진입점입니다.
`consensus_won`은 필요할 때만 원 단위로 덮어쓰며, 보통은 컨센서스 표를 포함한
`markdown_text`만 보냅니다. `graph_mode` 기본값은 `llm`입니다.

외부 클라이언트가 `llm`을 오래 기다리기 어려우면 다음 비동기 계약을 사용하세요.

```bash
curl -X POST "$BASE_URL/api/forecasts/operating-profit/correct/async" \
  -H 'Content-Type: application/json' \
  -d @docs/api-handoff/examples/correct-markdown-request.json
```

- `POST /api/forecasts/operating-profit/correct/async` → `202 {job_id, status_url, status}`
- `GET /api/forecasts/operating-profit/correct/jobs/{job_id}` → `queued | running | completed | failed`
- `completed` polling 응답의 `result`가 동기 `/correct` 응답과 동일합니다.
- job 결과는 단일 Web 프로세스 메모리에 최대 1시간 보관되며, 프로세스 재시작 시 사라집니다.

```bash
curl -X POST "$BASE_URL/api/forecasts/operating-profit/correct" \
  -H 'Content-Type: application/json' \
  -d @docs/api-handoff/examples/correct-markdown-request.json
```

처리 순서는 다음과 같습니다.

1. Markdown의 컨센서스 구간에서 날짜·대상 연도·단위를 해석합니다.
2. 컨센서스와 `상방/중간/하방` 각 셀을 비교해 확정 가능한 10의 거듭제곱 오류만 고칩니다.
3. 상반기·하반기·연간 블록이 모두 있으면 `상반기 + 하반기 = 연간`도 재검증합니다.
4. 원본 Markdown의 FactReasoner graph를 LLM으로 만들고, scale 변경과 관련된 atom을 LLM이 판정합니다.
5. FactReasoner corrector LLM이 대상·상류·하류 문장을 교정합니다.
6. 적용 후보 전체를 승인 전용 LLM이 재검토하고, 승인된 결과만 반환 Markdown에 병합합니다.
7. LLM 실패·거부·근거 부족 항목은 `factreasoner.manual_review`에 남깁니다.

주요 반환값은 다음과 같습니다.

| 필드 | 의미 |
|---|---|
| `corrected_markdown` | 호출자가 저장할 수 있는 최종 교정 Markdown |
| `scale_correction.corrections` | 연도·기간·셀별 배율과 전후 값 |
| `scale_correction.review_items` | 컨센서스 부재, 연도/단위 충돌, 불명확한 배율 등 |
| `factreasoner.scale_interventions` | scale 변경이 매핑된 FactReasoner pin |
| `factreasoner.cascades` | pin별 상·하류 전파 trace |
| `factreasoner.literal_ripple_corrections` | 같은 금액의 안전한 재인용 교정 |
| `factreasoner.applied_corrections` | 최종 Markdown에 자동 반영된 모든 FactReasoner/ripple 변경 |
| `factreasoner.llm_fact_judgments` | FactReasoner LLM의 사실성/의심 atom 판단 |
| `factreasoner.manual_review` | 자동 반영하지 않은 제안과 사유 |
| `factreasoner.rereview` | 자동 적용 직후 재검토한 건수·통과·되돌림 통계. 합이 맞으면 `advisory_reject` |
| `factreasoner.arithmetic_guard` | `상반기+하반기=연간` 검증. `lock=true`면 재검토가 scale 블록을 되돌리지 않음 |
| `stats.manual_review_required` | 상세 검토 목록 또는 graph 오류가 하나라도 있는지 |

## 컨센서스 자동 추출 계약

`consensus_won`을 생략하면 섹션 번호와 관계없이 제목에 `최근 컨센서스 전망치 분석`이
포함된 블록을 찾습니다. 각 블록 안에서 ISO 날짜(`YYYY-MM-DD`)가 가장 최신인 영업이익
값을 사용합니다.

- Markdown 표의 `Date/날짜`와 `영업이익` 열을 우선 사용합니다.
- 2열 `Date + Amount/금액`, dataframe/plain-text 행, 쉼표·소수·과학적 표기법을 지원합니다.
- `Amount (원)`, `Amount (억원)`, `영업이익 (조원)`처럼 열 제목에 선언된 단위를 원으로 환산합니다.
- 원/억원 열이 함께 있으면 환산값을 교차검증하고, 불일치하면 자동 교정을 보류합니다.
- 다중 열 표에서 매출액을 영업이익으로 추측하지 않습니다.
- 컨센서스 제목이나 설명에 명시된 대상 연도를 다음 전망 heading보다 우선합니다.
- 2026년 컨센서스를 2027년 전망에 재사용하지 않습니다. 대응 근거가 없으면 `review_items`로 보냅니다.
- 영업손실은 음수로 입력·추출할 수 있습니다. `consensus_won: 0`은 HTTP 400입니다.

다년도 문서에서는 최상위 `consensus_won`만 보지 말고 각
`corrections[].consensus_won`, `consensus_date`, `consensus_extraction.blocks`를 함께 확인하세요.

## 숫자 교정 기준(결정론적 단계)

상방·중간·하방 각 셀을 컨센서스와 개별 비교합니다.

- 5배 이상 차이나는 셀만 배율 오류 후보입니다.
- `0.001/0.01/0.1/10/100/1000` 중 컨센서스에 가장 가까운 배율을 고릅니다.
  (보정 후 잔차 상한은 두지 않으며, 품질은 LLM 재검토가 담당합니다.)
- 이미 합리적 범위인 셀은 그대로 둡니다.
- 모든 셀의 판정이 명확하고 최종 `상방 ≥ 중간 ≥ 하방`이 유지될 때만 변경합니다.
- 흑자와 손실의 부호가 다르면 자동 배율 교정을 하지 않습니다.
- 단일 연간 값의 큰 격차를 확정하지 못하면 조용히 통과시키지 않고 `review_items`로 보냅니다.
- 컨센서스로 연간 블록을 고정할 수 있을 때만 `상반기 + 하반기 = 연간`의 유일한 블록/셀 교정을 적용합니다.

`corrections[].changes`에는 실제로 바뀐 행만 들어갑니다. `factors`는 label별 배율이고,
변경 배율이 모두 같을 때만 공통 `factor`가 숫자이며 서로 다르면 `null`입니다. 숫자 토큰 외의
단위, 쉼표 스타일, trailing Markdown, CRLF는 보존합니다.

## FactReasoner 모드와 자동 적용 경계

- `graph_mode: "llm"`(기본): LLM FactReasoner graph·atom 판정·corrector·최종 검토를 실행합니다.
- `graph_mode: "fast"`: `nli_mode=fast`를 기본 파생해 다국어 embedding gate를 적용합니다.
  전체 문서 atom 추출·교정 후보·cascade·최종 검토 범위는 `llm`과 같습니다.
- `nli_mode: "all_pairs" | "fast"`: 관계 후보 비용 축입니다. 명시하면 `graph_mode` 파생값보다
  우선합니다. embedding 실패 시 `all_pairs`로 자동 복귀합니다.

FactReasoner graph/judge/cascade는 `FACTREASONER_BASE_URL`, `FACTREASONER_MODEL` 및 선택적
`FACTREASONER_API_KEY`를 우선 사용합니다. 이 변수가 없을 때만 공통 `OPENAI_*` 설정으로
fallback합니다. 따라서 프로젝트 `.env`의 `FACTREASONER_BASE_URL`과 `FACTREASONER_MODEL`
설정이 실제 교정 API에도 적용됩니다.

동일 원본 Markdown의 LLM graph는 API 프로세스 메모리에서 1시간 재사용합니다. `/correct` 응답은
`factreasoner.graph_cache`, `/api/fact-graph-preview`는 `graph_cache`에 cache hit와 age를 남깁니다.
`FACTREASONER_GRAPH_MODEL`, `FACTREASONER_JUDGE_MODEL`,
`FACTREASONER_CORRECTION_MODEL`, `FACTREASONER_REVIEW_MODEL`은 graph 추출·판단·교정·승인
재검토 모델을 각각 분리하며, 비어 있으면 모두 `FACTREASONER_MODEL`로 fallback합니다.
DeepSeek SGLang ChatML 서버에는 `FACTREASONER_STOP_SEQUENCES=<｜end▁of▁sentence｜>`를 지정해
assistant turn이 JSON 뒤에 계속 생성되지 않도록 합니다.
Fast mode는 기본적으로 `Qwen/Qwen3-Embedding-0.6B`를 sentence-transformers로 상주시킵니다.
현재 8202 서비스는 RTX 5090 GPU 0(`CUDA_VISIBLE_DEVICES=0`, device `cuda:0`)을 사용합니다.
사전 다운로드된 운영 캐시는 `FACTREASONER_EMBEDDING_LOCAL_FILES_ONLY=1`로 고정합니다. 모델 상태는
`GET /api/factreasoner/embedding/status`, pair/cache/phase 계측은 응답의
`factreasoner.nli_stats`에서 확인합니다. 관계·의심 판정 SQLite 캐시는 기본적으로
`.cache/factreasoner/nli.sqlite3`에 저장됩니다.

`/correct`가 자동 병합하는 FactReasoner 변경은 atom 판단·구조/anchor 경계를 통과해야 합니다.
최종 LLM 재검토는 전체 변경 집합을 검토하지만, 산술 가드가 통과한 scale 블록과 이미 적용된
ripple/cascade/atom을 함부로 되돌리지 않습니다. 현재 남아 있는 deterministic 조건은
Markdown 파괴를 막는 구조/anchor 경계와 `상반기+하반기=연간` 가드입니다.

- 영업이익 atom과 연도·시나리오·원래 금액이 유일하게 대응함
- 제한된 후보 수(`max_factreasoner_candidates`, 기본 10, 범위 1~10) 안에 있음
- 현재 Markdown에 교정 대상이 정확히 한 번 존재함
- 교정 전후 줄 수와 heading/table/code fence/list 구조가 같음
- 교정문이 원문보다 과도하게 짧지 않음
- cascade 변경은 기존 값에서 확정 pin 값으로의 동일 금액 치환과 일치함

새 계산값이나 정성 문구를 만든 전파 제안, 중복 anchor, 충돌 제안, `premise_proposals`는 자동
병합하지 않습니다. 서버 파일은 어떤 모드에서도 쓰지 않습니다.

## API 선택 가이드

| API | 용도 |
|---|---|
| `POST /api/forecasts/operating-profit/correct` | 권장: Markdown 입력 → 교정 Markdown + 전체 trace |
| `POST /api/forecasts/operating-profit/scale-correction` | 디버그: 결정론 배율 제안은 항상 반환, LLM 재검토는 advisory |
| `POST /api/forecasts/operating-profit/correction-preview` | scale 수정본의 graph를 보고 후속 호출을 직접 제어할 때 |
| `POST /api/fact-graph-preview` | 임의 Markdown의 FactReasoner graph 생성 |
| `POST /api/fact-graph/judge-atom` | 선택 atom 하나의 교정안 생성 |
| `POST /api/fact-graph/propagate-correction` | 승인한 atom 값을 상·하류 문장에 전파하는 제안 생성 |

고급 검토형 흐름은 `examples/factreasoner-workflow.md`에 있습니다. 독립
`/propagate-correction` 응답은 제안이므로 호출자가 `affected`, `needs_manual`, `reason`을
검토하고 승인한 뒤 저장해야 합니다.

## 오류 처리와 운영 기준

- 잘못된 입력: HTTP 400 또는 Pydantic 422
- LLM/외부 서비스 미가동: HTTP 200일 수 있으므로 `factreasoner.online`, `error`,
  `stats.manual_review_required`를 확인
- API 실패를 ‘교정 불필요’로 해석하지 말 것
- 원문 저장은 호출 시스템의 승인·감사 로그 단계에서 수행할 것

## 전달 구성

- `OVERVIEW.md`: 비개발자·의사결정자용 한 장 설명자료
- 이 문서: 개발자용 계약과 운영 경계
- `examples/`: 실행 가능한 JSON과 고급 호출 흐름
- `ripple-repair-api.postman_collection.json`: Postman import 컬렉션
- `presentation/outline.md`: 5장 설명자료 초안
