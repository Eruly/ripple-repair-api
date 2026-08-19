# ripple-repair-api

영업이익 전망 Markdown 교정 API입니다. 연구 모노레포 `hallucination_agent`에서
`/correct` 스택만 추출했습니다.

## 범위

포함: FastAPI `/api/forecasts/operating-profit/*`, FactReasoner graph/judge/cascade,
Web UI `/forecast-correction`, `docs/api-handoff/`.

제외: ADK 에이전트, DART/OCR/Calculator/Logic/Ontology 스킬, OpenKB, 5단계 verify SSE,
`runs/`, `data/`, 벤치마크 코퍼스.

## 실행

```bash
uv sync
uv run uvicorn web_app.main:app --reload --port 8200
```

시크릿은 `.env`에만 두고 Git에 올리지 마세요.
