"""report_audit — 영업이익 전망 보고서(마크다운) LLM 감사 파이프라인.

전처리 → DART 셀 대조 → 번호 붙인 원장 인용의 청크 누적 읽기 → 결론(전망) 추론 감사 → 교정·전파·결론 수정 제안 → HTML/JSON.
표준 라이브러리만 쓴다. 진입점: `python3 -m report_audit.run_report_audit 보고서.md`, `python3 -m report_audit.audit_api`,
FastAPI 는 `web_app.report_audit_router` (POST /api/report-audit). 절차 문서: docs/report-audit/GUIDE.md
"""
