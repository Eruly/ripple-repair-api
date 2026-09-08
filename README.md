# RippleRepair API

재무 리서치 Markdown을 보내면 영업이익 전망의 **자릿수 오류**와 그 값을 재인용한 문장을
교정해 `corrected_markdown`으로 반환합니다. 서버의 원본 파일은 쓰거나 덮어쓰지 않습니다.

이 저장소는 연구용 모노레포에서 **forecast correction API만** 추출한 GitHub용 패키지입니다.
DART/OCR/OpenKB 검증 파이프라인과 대용량 `runs/`·`data/`는 포함하지 않습니다.

## 빠른 시작

```bash
cp .env.example .env   # OPENAI_BASE_URL / OPENAI_MODEL 을 로컬 또는 호스티드 LLM에 맞추세요
uv sync
uv run uvicorn web_app.main:app --host 0.0.0.0 --port 8200
```

- Web UI: http://localhost:8200/forecast-correction
- OpenAPI: http://localhost:8200/docs
- 상태: `GET /api/status`

권장 호출:

```http
POST /api/forecasts/operating-profit/correct
Content-Type: application/json

{"markdown_text": "<컨센서스와 전망이 포함된 Markdown>"}
```

응답의 `corrected_markdown`이 최종본입니다. 긴 문서는
`POST /api/forecasts/operating-profit/correct/async` → `GET .../jobs/{job_id}` 를 쓰세요.

## 파이프라인

`/correct` 순서: **scale → literal ripple → FactReasoner cascade/atom → 산술 가드 → advisory LLM 재검토**.

- 컨센서스 추출과 배율 계산은 결정론적입니다.
- 같은 금액 재인용은 리터럴 ripple로 반영합니다.
- H1+H2=연간이 맞으면 재검토가 그 이유로 교정본을 되돌리지 않습니다.
- 합이 깨진 scale만 원문으로 롤백합니다. 잔차 > 25% 또는 재검토 거부는 `manual_review`로 남기되 제안 Markdown은 유지합니다.

자세한 계약은 [`docs/api-handoff/README.md`](docs/api-handoff/README.md)와
[`docs/api-handoff/OVERVIEW.md`](docs/api-handoff/OVERVIEW.md)를 보세요.
Postman 컬렉션: [`docs/api-handoff/ripple-repair-api.postman_collection.json`](docs/api-handoff/ripple-repair-api.postman_collection.json).

## 보고서 LLM 감사 (`report_audit`, `POST /api/report-audit`)

교정 API 와 별도로, 영업이익 전망 보고서 한 건을 **DART 공시 대조 → 번호 붙인 원장 인용의 청크 누적 읽기 → 결론(전망) 추론 감사 → 교정·근거 전파·결론 수정 제안 → HTML/JSON** 으로 한 번에 감사합니다. 표준 라이브러리만 쓰는 `report_audit/` 패키지이며 DART Open API 키(`DART_API_KEY`)가 필요합니다.

```bash
uv run python -m report_audit.dart_corp_codes                      # 최초 1회: 상장사 코드 목록
uv run python -m report_audit.run_report_audit docs/report-audit/example/SNT모티브.md   # CLI → runs/report_audit/<tag>/report.html
curl -s -X POST http://localhost:8200/api/report-audit -H 'Content-Type: application/json' \
     -d '{"name":"SNT모티브","markdown_text":"...","llm":true}'    # FastAPI → html_url / json_url
```

절차·결과 읽는 법·배치 실행은 [`docs/report-audit/GUIDE.md`](docs/report-audit/GUIDE.md), 예시 출력은 [`docs/report-audit/example/`](docs/report-audit/example/).

## 요구 사항

- Python 3.11+
- OpenAI-compatible Chat Completions 엔드포인트 (vLLM, llama.cpp, OpenAI 등)
- Qwen3.x / GLM 계열은 structured JSON을 위해 `FACTREASONER_ENABLE_THINKING`을 비워 두세요 (`enable_thinking=false`).

Fast NLI pair mining:

```bash
uv sync --extra fast
# .env: FACTREASONER_NLI_MODE=fast
```

## 테스트

```bash
uv sync --extra dev
uv run pytest -q
```

LLM이 없는 단위 테스트는 결정론 scale·JSON 계약·job progress·UI 계약, 그리고 `report_audit` 의 전처리·표 파싱·단위 판정·LLM 없는 원샷 실행·라우터 계약을 검증합니다.

## 보안

애플리케이션은 API 키 인증을 강제하지 않습니다. 외부 공유 시 사설망·VPN 또는
API gateway의 인증과 TLS 뒤에 두세요. 요청·응답에 보고서 원문이 포함됩니다.
`.env`는 커밋하지 마세요.

## 라이선스

MIT. 연구 원본 모노레포와 벤치마크 데이터는 이 패키지에 포함되지 않습니다.
