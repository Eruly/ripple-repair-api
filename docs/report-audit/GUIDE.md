# 영업이익 전망 보고서 LLM 감사 — 재현 안내 (`report_audit`)

LLM이 쓴 영업이익 전망 보고서(마크다운)를 받아 **DART 공시로 자료를 대조하고, 번호 붙인 원장을 인용하며 문장 단위로 근거·충돌을 표시하고, 결론(전망) 추론을 감사하고, 잘못된 문장·결론을 근거와 함께 고치는** 절차를 한 번에 실행하는 방법이다. 코드는 `report_audit/` 패키지(표준 라이브러리만)이고, FastAPI 앱에는 `POST /api/report-audit` 로 붙어 있다.

- 한 줄 실행(CLI): `uv run python -m report_audit.run_report_audit 보고서.md`
- FastAPI: `uv run uvicorn web_app.main:app --port 8200` 후 `POST /api/report-audit`
- 의존성 없는 HTTP 서버: `python3 -m report_audit.audit_api --port 8787` 후 `POST /audit`
- 예시 입력·출력: [`example/SNT모티브.md`](example/SNT모티브.md) → [`example/report.html`](example/report.html), [`example/result.json`](example/result.json)

---

## 1. 준비

### 1.1 DART Open API
- https://opendart.fss.or.kr 에서 키를 발급받아 `.env` 에 `DART_API_KEY=...` 로 둔다 (환경변수로 줘도 된다).
- 상장사 고유번호 목록을 한 번 만든다 (gitignore 대상):
  ```bash
  uv run python -m report_audit.dart_corp_codes     # data/dart/corpCode.zip → data/dart/corp_codes_listed.json (약 3,989개)
  ```
- DART 응답은 `data/dart/fin/{corp_code}_{year}_{reprt}.json` 에 캐시된다. 처음 보는 회사는 연도·보고서별로 20~30회 호출하고, 그 뒤로는 0회다.
- 회사 코드 파일이 없으면 파이프라인은 멈추지 않고 "DART 회사 매핑 실패" 경고를 내며 자료 대조 없이 진행한다.

### 1.2 LLM
청크 누적 읽기·결론 감사·문장 수정은 OpenAI 호환 채팅 API 를 쓴다. 기본은 `.env` 의 `OPENAI_BASE_URL` / `OPENAI_MODEL` 이고, 따로 두려면 `REPORT_AUDIT_BASE_URL` / `REPORT_AUDIT_MODEL`. 127.0.0.1 서버는 키 없이 호출한다.
- 판정 프롬프트는 JSON 만 받으므로 사고(thinking) 출력을 끈다 (`REPORT_AUDIT_LLM_THINKING=0`, 기본값).
- 로컬 SGLang 예시 기동 스크립트: `scripts/serve_sglang.sh` (Qwen3.8‑27B NVFP4, 32GB 카드 기준 옵션; 경로는 `SGLANG_ROOT` 등 환경변수로 바꾼다). 32k 미만 컨텍스트에서도 청크 단위로 동작한다.
- 개발에 쓴 모델: Qwen3.8‑27B (NVFP4, SGLang 0.5.18). 다른 모델은 `--llm-model` 로 바꾸면 된다.

### 1.3 입력 문서 형식
마크다운 한 파일이며 **파일 이름(확장자 제외)이 회사명**이다 (`SNT모티브.md`; `삼성전자_case4.md` 처럼 `_case\d+`/`_v\d+` 접미사는 무시). 약칭은 `report_audit/check_case_forecasts.py` 의 `ALIAS` 에 추가한다. 절 번호는 달라도 되고 내용으로 자료 종류를 판별한다.

| 자료 | 인식 방식 | 원장 접두 |
|---|---|---|
| 연간 재무 표 (`(단위: 억 원)` + 마크다운 표, 항목 × 연도) | 행 파싱, 셀 단위(`42.99조 원`) 허용, 단위가 없으면 DART 로 확정 | T# |
| 분기 영업이익 (`[{'Date': '2021/Q1', '영업이익': 2.632e10}, …]`) | 리스트 리터럴 (콤마·지수 표기 정규화 후) | Q# |
| 뉴스 (`[YYYY-MM-DD] 제목`, `날짜 \| 제목`) | 발행일 포함 | N# |
| 주가 (월별 표 또는 리스트) | 마크다운 표/인덱스 행 | P# |
| 컨센서스 (`Date \| Amount` 헤더 표) | 헤더로 주가와 구분 | C# |
| 전망 (`2026년 상반기 전망: 749,000,000,000` 등; 시나리오형이면 중간 값) | 구조 무관 파싱 | F# |
| 계열사·관계사 언급 | 회사명 사전 + DART 연결 매출 비교 | E# |

나머지 문장은 S# 로 번호가 붙는다. DART 사실은 D#, 파생 점검은 R#, 새 주장은 K# 다.

---

## 2. 한 번에 실행 (CLI)

```bash
uv run python -m report_audit.run_report_audit docs/report-audit/example/SNT모티브.md
# 여러 문서:            ... a.md b.md c.md --tag mybatch
# LLM 없이 자료 대조만: ... a.md --no-llm
# 다른 LLM:             ... a.md --llm-base-url https://api.example/v1 --llm-model gpt-x   (키는 OPENAI_API_KEY)
```

출력 (`runs/report_audit/<tag>/`, gitignore):
- `report.html` — 문서별 **문장 번호·인용 표시 + 결론 감사** 페이지 (4절 참조).
- `result.json` — 문서별 `summary`, DART 대조 `crosscheck`, 전체 `record` (원장 `ledger`, 청크별 판정 `chunks`, `conflicts`, `conclusion_audit`, `revisions`).
- `run.log` — 단계 명령과 표준출력. 단계별 원본 결과는 `runs/report_audit/steps/` 에 남는다.

표준출력 예 (SNT모티브):
```
[SNT모티브] 표 단위: 표기 '억원' 확인 | 표 셀 {'match': 21, ..., 'mismatch': 4, 'unit_error': 0} | 분기 {'match': 21, ...}
    문장 53 · 근거 인용 110 · 서술 충돌 4 · 자료 충돌 4
    결론 감사: unsupported | 결함 유형 {'conflict': 3, 'invalid_inference': 2, 'non_sequitur': 2, 'ungrounded': 0}
    교정: 자료 정정 4 · 문장 수정 {'accepted': 1, ...} · 결론 수정 accepted {'H1': 460.4, 'H2': 550.0, 'FY': 1010.4}
```
소요 시간: DART 대조 수 초(캐시 있으면), LLM 단계 문서당 약 4~15분(27B 로컬, 문장 50~120개). LLM 호출은 청크 판정 1회 + 결론 감사 1회 + 수정 문장마다 1회 + 결론 수정 1회이고, JSON 실패 시 최대 3회 재시도한다.

## 3. API 로 실행

### 3.1 FastAPI (`web_app.main:app`)
```bash
uv run uvicorn web_app.main:app --host 0.0.0.0 --port 8200
```
```bash
curl -s -X POST http://127.0.0.1:8200/api/report-audit -H 'Content-Type: application/json' \
     -d "$(python3 -c 'import json,sys;print(json.dumps({"name":"SNT모티브","markdown_text":open(sys.argv[1]).read(),"llm":True}))' docs/report-audit/example/SNT모티브.md)"
```
응답:
```json
{"tag": "api_20260908_170102_SNT모티브",
 "summary": {"documents": 1, "seconds": 226.7, "llm": true, "model": "..."},
 "warnings": [],
 "reports": [{"name": "SNT모티브", "summary": {"corp_code": "00398792", "unit": "표기 '억원' 확인", "table_cells": {...}, "quarters": {...},
              "sentences": 53, "supported_refs": 110, "narrative_conflicts": 4, "data_conflicts": 4,
              "verdict": "unsupported", "verdict_llm": null, "downgrade_reason": null, "reason_types": {...},
              "revisions": {...}, "data_fixes": 4, "conclusion_fix": {"status": "accepted", "revised_forecast": {"H1": 460.4, "H2": 550.0, "FY": 1010.4}, "derivation": "..."}}}],
 "html_url": "/api/report-audit/api_.../report.html", "json_url": "/api/report-audit/api_.../result.json", "log_url": "/api/report-audit/api_.../run.log"}
```
`html_url` 을 브라우저로 열면 표시 페이지가 나온다. `"llm": false` 면 전처리·DART 대조만 한다. 감사는 서버 안에서 직렬화된다(로컬 LLM 동시 1개 가정). OpenAPI 문서는 `/docs` 의 `report-audit` 태그.

### 3.2 의존성 없는 서버 (`report_audit.audit_api`)
```bash
python3 -m report_audit.audit_api --port 8787
curl -s -X POST http://127.0.0.1:8787/audit -H 'Content-Type: text/markdown' -H 'X-Report-Name: SNT모티브' --data-binary @docs/report-audit/example/SNT모티브.md
# JSON 본문: -H 'Content-Type: application/json' -d '{"name":"...","text":"...","llm":true}';  LLM 없이: POST /audit?llm=0
# 결과: GET /audits/<tag>/report.html | result.json | run.log
```

---

## 4. 단계와 결과 읽는 법

| # | 단계 | 모듈 (수동 실행) | 무엇을 하는가 |
|---|---|---|---|
| 1 | 전처리 | `report_audit.case_preprocess` (`normalize_numbers`) | `8.275000e+10` → `82,750,000,000`, 7자리 이상 정수·`.0` → 천단위 구분, `nan` → NaN. 각 단계가 읽을 때 자동 적용. |
| 2 | DART 셀 대조 | `python -m report_audit.dart_crosscheck --base DIR --only 이름 --out-tag 태그` | 표 셀(매출액·매출총이익·영업이익·판관비·당기순이익)·분기 영업이익을 DART `fnlttSinglAcnt`/`fnlttSinglAcntAll` 과 셀 단위로 대조. **DART 금액은 항상 원**이며 문서 단위로 배율을 추정하지 않는다. 분기는 Q1/H1−Q1/9M−H1/FY−9M. 표 단위는 셀별 10^k 비율의 최빈값으로 확정. |
| 3 | 청크 누적 읽기 + 결론 감사 | `python -m report_audit.check_case_llm_chunked --base DIR --names 이름 --out-tag extra_태그` | 원장(D/T/Q/N/P/C/F/E/R)을 결정적으로 만든 뒤 절 단위 청크를 LLM 이 읽으며 문장(S#)마다 `supports`/`conflicts`/`causal` 을 원장 번호로 인용. 결론 절의 전망은 R# 점검(H1+H2=FY, H1−Q1 로 Q2 역산, 2025 대비 성장률, 컨센서스 이탈)과 전제별 판정으로 감사. `--audit-only` 는 감사만 다시 한다. |
| 4 | 교정 + 전파 + 결론 수정 제안 | `python -m report_audit.revise_case_sentences --names 이름` | 자료 층은 DART 값으로 결정적 정정. 충돌 문장은 LLM 이 최소 수정하고 수정문의 숫자가 원장·파생값(마진·성장률·반기 합)에 접지되는지 재검사(안 되면 `held`). 수정으로 사라진 수치를 쓰는 문장·같은 충돌을 공유하는 문장에 **함께 수정**을 전파. 결론은 작성 시점 사실만으로 다시 도출(사후 실현값 제외, 단위 오류 후보 /10^k). `--conclusion-only` 로 결론 제안만 다시 만든다. |
| 5 | 렌더 | `report_audit.render_llm_chunked_view` (`render_report`) | 문서별 HTML 블록 + 공용 CSS(`view_css`). |

### 4.1 셀 분류 (2단계)
| 분류 | 뜻 |
|---|---|
| `match` | DART 와 1% 이내 |
| `match_restated` | 다음 연도 보고서의 전기/전전기 재작성 값과 일치 |
| `match_controlling` | 지배주주 순이익(당기 또는 전기)과 일치 — 당기순이익 행의 대표 원천 오류 |
| `match_other_basis` | 계속영업·포괄손익·별도 기준과 일치 |
| `unit_error` | 부호 같고 \|log10 비율 − n\| < 0.01 (10ⁿ 배) |
| `mismatch` | 위 어디에도 안 맞음 |

### 4.2 문장 표시 (3단계)
- 초록 밑줄: 원장에 근거 있음(마우스를 올리면 인용 번호와 원문). 빨강: 충돌. 노랑: 인과 주장(사후 뉴스로 앞선 결과를 설명하면 시간 역전으로 승격).
- 표 행 옆 상태: `DART 일치`, `재작성 일치`, `지배주주 순이익 일치`, `10ⁿ 배 오차`, `불일치`; 단위 줄 `표기 '억원' 확인 / 미기재 → DART 로 원 확정 / 표기≠실제`.

### 4.3 결론 감사 (3단계, 결론 절 아래)
- 전망 수치 줄이 판정 색으로 표시되고 억/조 환산과 R# 배지가 붙는다.
- 판정 `supported` / `partially_supported` / `unsupported`. LLM 판정 뒤에 **결정적 강등**이 적용된다(Q2 역산이 Q1 의 ±300% 를 벗어남, 연간 성장률 >300% 또는 <−80%). 강등되면 `verdict_llm` 과 `downgrade_reason` 을 함께 보여 준다.
- 전제별 `supports_conclusion` yes/weak/no 와 결함 유형 `conflict`(자료와 충돌) / `invalid_inference`(전제는 맞지만 추론이 틀림) / `non_sequitur`(논리적 비약) / `ungrounded`(근거 없음).
- 결론 수정 제안: 작성 시점 사실만으로 다시 도출한 H1/H2/FY, 도출식, 점검(H1+H2=FY, H1≥Q1, 사후 실현값 미사용), 함께 고칠 전제 문장.

### 4.4 교정 표시 (4단계)
`→ 수정 제안`(accepted), `⇢ 함께 수정`(propagated, 어느 수정에서 전파됐는지 표시), `보류`(held: 접지 실패 사유), 자료 정정 표.

---

## 5. 배치 실행

여러 보고서 폴더를 한 번에 돌릴 때는 `CASE_REPORTS_DIR=폴더` 를 두고 `--base` 없이 각 모듈을 실행한다.
```bash
export CASE_REPORTS_DIR=/path/to/reports            # *.md, 파일 이름 = 회사명
uv run python -m report_audit.dart_crosscheck                       # 전체 셀 대조
uv run python -m report_audit.check_case_logic                      # 규칙 기반 논리 검사 (분기 패턴·시간 역행·법인 혼동)
uv run python -m report_audit.check_case_llm_chunked --names A,B,C --out-tag batch   # 청크 읽기 (문서당 수 분)
uv run python -m report_audit.check_case_llm_chunked --audit-only --names A,B,C --out-tag audit
uv run python -m report_audit.revise_case_sentences --names A,B,C
```
참고 결과(2026-09, LLM 생성 보고서 501건): 500건 회사 매핑; 표 셀 match 7,082 / restated 195 / controlling 160 / other 12 / mismatch 1,189 / unit_error 1,338; 분기 6,996 / 71 / 1; 단위 미기재 표 262건을 DART 로 확정; 당기순이익 행은 검사 가능한 354건 중 262건(74%)에서 원천 데이터 오류(지배주주 순이익 혼동 등). 47건 결론 감사: supported 16 / partially 19 / unsupported 11.

## 6. 한계
- DART 에 없는 값(주가·뉴스·컨센서스·비상장 계열사)은 자료 층에서 대조하지 못하고 문장 층의 인용·충돌 판정만 받는다.
- 27B 로컬 모델은 JSON 이 깨질 때가 있어 3회 재시도한다. 실패하면 `conclusion_audit.error` 가 남고 감사 블록이 비어 표시된다.
- 강등 규칙은 R# 점검이 계산 가능한 경우(전망 3개 값과 전년 실적)에만 작동한다.
- 문장 수정은 최소 수정이며 문서 전체를 다시 쓰지 않는다. `held` 는 사람이 봐야 한다.
- 요청·응답에 보고서 원문이 들어간다. 외부 노출 시 인증·TLS 뒤에 두고 로그 보존 기준을 정한다.
