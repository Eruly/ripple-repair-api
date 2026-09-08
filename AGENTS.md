# ripple-repair-api

영업이익 전망 Markdown 교정 API입니다. 연구 모노레포 `hallucination_agent`에서
`/correct` 스택만 추출했습니다.

## 범위

포함: FastAPI `/api/forecasts/operating-profit/*`, FactReasoner graph/judge/cascade,
Web UI `/forecast-correction`, `docs/api-handoff/`.
`report_audit/` 패키지와 `POST /api/report-audit` (DART 대조·청크 누적 읽기·결론 추론 감사·교정, `docs/report-audit/GUIDE.md`).
`report_audit` 는 표준 라이브러리만 쓰고 `python3 -m report_audit.<module>` 로 실행한다. 결과는 `runs/report_audit/` (gitignore), DART 캐시는 `data/dart/` (gitignore).

제외: ADK 에이전트, DART/OCR/Calculator/Logic/Ontology 스킬, OpenKB, 5단계 verify SSE,
`runs/`, `data/`, 벤치마크 코퍼스.

## 실행

```bash
uv sync
uv run uvicorn web_app.main:app --reload --port 8200
```

시크릿은 `.env`에만 두고 Git에 올리지 마세요.
