"""FastAPI 라우터: 영업이익 전망 보고서 LLM 감사 (report_audit 패키지) 를 한 요청으로 실행한다.

POST /api/report-audit            {"name": "SNT모티브", "markdown_text": "...", "llm": true}
GET  /api/report-audit/{tag}/report.html   문장 번호·인용 표시 + 결론 감사 + 교정 제안 페이지
GET  /api/report-audit/{tag}/result.json   전체 결과 (원장·판정·수정)
GET  /api/report-audit/{tag}/run.log       단계별 로그
로컬 LLM 서버가 동시 요청 1개로 서비스되는 경우가 많아 감사는 프로세스 안에서 직렬화한다.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from report_audit.run_report_audit import DEFAULT_BASE_URL, DEFAULT_MODEL, audit_report

router = APIRouter(prefix="/api/report-audit", tags=["report-audit"])
OUT_DIR = Path(__file__).resolve().parents[1] / "runs/report_audit"
_LOCK = threading.Lock()
_TAG_RE = re.compile(r"^[\w\-]+$")


class ReportAuditRequest(BaseModel):
    markdown_text: str = Field(..., min_length=1, description="보고서 마크다운 본문")
    name: str = Field("report", description="회사명. DART 회사명(또는 ALIAS)과 맞아야 자료 대조가 된다")
    llm: bool = Field(True, description="False 면 전처리·DART 대조만 수행")
    llm_base_url: str | None = None
    llm_model: str | None = None


def _run(req: ReportAuditRequest) -> dict:
    safe = re.sub(r"[^\w가-힣\-]+", "_", req.name)[:60] or "report"
    tag = time.strftime("api_%Y%m%d_%H%M%S") + "_" + safe
    incoming = OUT_DIR / "_incoming"; incoming.mkdir(parents=True, exist_ok=True)
    src = incoming / f"{safe}.md"; src.write_text(req.markdown_text, encoding="utf-8")
    with _LOCK:
        res = audit_report([src], OUT_DIR, tag, req.llm_base_url or DEFAULT_BASE_URL, req.llm_model or DEFAULT_MODEL, req.llm)
    return {"tag": tag, "summary": res["summary"], "warnings": res.get("warnings", []),
            "reports": [{"name": r["name"], "summary": r["summary"]} for r in res["reports"]],
            "html_url": f"/api/report-audit/{tag}/report.html", "json_url": f"/api/report-audit/{tag}/result.json",
            "log_url": f"/api/report-audit/{tag}/run.log"}


@router.post("")
async def report_audit(req: ReportAuditRequest) -> dict:
    try:
        return await asyncio.to_thread(_run, req)
    except FileNotFoundError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except RuntimeError as ex:
        raise HTTPException(status_code=502, detail=str(ex))


def _artifact(tag: str, name: str, media_type: str) -> FileResponse:
    if not _TAG_RE.match(tag):
        raise HTTPException(status_code=400, detail="bad tag")
    f = OUT_DIR / tag / name
    if not f.exists():
        raise HTTPException(status_code=404, detail="no such audit")
    return FileResponse(str(f), media_type=media_type)


@router.get("/{tag}/report.html")
async def report_html(tag: str) -> FileResponse:
    return _artifact(tag, "report.html", "text/html; charset=utf-8")


@router.get("/{tag}/result.json")
async def report_json(tag: str) -> FileResponse:
    return _artifact(tag, "result.json", "application/json; charset=utf-8")


@router.get("/{tag}/run.log")
async def report_log(tag: str) -> FileResponse:
    return _artifact(tag, "run.log", "text/plain; charset=utf-8")
