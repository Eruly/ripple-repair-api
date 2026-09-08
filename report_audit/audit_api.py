#!/usr/bin/env python3
"""보고서 감사 HTTP API (표준 라이브러리만). run_report_audit.audit_report 를 한 번의 요청으로 실행한다.

실행:  python3 -m report_audit.audit_api --port 8787 [--llm-base-url ...] [--llm-model ...]
요청:
  POST /audit            본문 = 마크다운 텍스트 (Content-Type: text/markdown; 헤더 X-Report-Name 으로 이름 지정, 기본 report)
  POST /audit            본문 = JSON {"name": "DL", "text": "...마크다운...", "llm": true}
  POST /audit?llm=0      LLM 없이 전처리·DART 대조만
응답: JSON {"tag", "summary", "reports": [{name, summary}], "html_url": "/audits/<tag>/report.html", "json_url": "/audits/<tag>/result.json"}
  GET  /audits/<tag>/report.html   표시 페이지 (문장 번호·인용 표시 + 결론 감사 + 교정 제안)
  GET  /audits/<tag>/result.json   전체 결과 (원장·판정·수정)
  GET  /health
예:
  curl -s -X POST http://127.0.0.1:8787/audit -H 'Content-Type: text/markdown' -H 'X-Report-Name: SNT모티브' --data-binary @SNT모티브.md | python3 -m json.tool
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.run_report_audit import DEFAULT_BASE_URL, DEFAULT_MODEL, audit_report  # noqa: E402

OUT_DIR = REPO / "runs/report_audit"
LOCK = threading.Lock()  # 로컬 LLM 서버는 동시 요청 1개로 서비스되므로 감사도 직렬화
CFG = {"base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._json(200, {"ok": True, "llm": CFG})
        m = re.match(r"^/audits/([\w\-]+)/(report\.html|result\.json|run\.log)$", urllib.parse.unquote(u.path))
        if not m:
            return self._json(404, {"error": "not found"})
        f = OUT_DIR / m.group(1) / m.group(2)
        if not f.exists():
            return self._json(404, {"error": "no such audit"})
        ctype = {"report.html": "text/html; charset=utf-8", "result.json": "application/json; charset=utf-8", "run.log": "text/plain; charset=utf-8"}[m.group(2)]
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path != "/audit":
            return self._json(404, {"error": "POST /audit only"})
        q = urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length", "0")); raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        use_llm = q.get("llm", ["1"])[0] not in ("0", "false")
        if "json" in ctype:
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as ex:
                return self._json(400, {"error": f"bad json: {ex}"})
            name = str(body.get("name") or "report"); text = str(body.get("text") or ""); use_llm = bool(body.get("llm", use_llm))
        else:
            name = self.headers.get("X-Report-Name") or "report"; text = raw.decode("utf-8", errors="replace")
        name = urllib.parse.unquote(name)
        try:  # http.server 는 헤더를 latin-1 로 읽으므로 UTF-8 한글 이름을 되살린다
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        if not text.strip():
            return self._json(400, {"error": "empty report text"})
        safe = re.sub(r"[^\w가-힣\-]+", "_", name)[:60] or "report"
        tag = time.strftime("api_%Y%m%d_%H%M%S") + "_" + safe
        tmp = OUT_DIR / "_incoming"; tmp.mkdir(parents=True, exist_ok=True)
        src = tmp / f"{safe}.md"; src.write_text(text, encoding="utf-8")
        with LOCK:
            try:
                res = audit_report([src], OUT_DIR, tag, CFG["base_url"], CFG["model"], use_llm)
            except Exception as ex:  # noqa: BLE001
                return self._json(500, {"error": f"{type(ex).__name__}: {ex}", "log": f"/audits/{tag}/run.log"})
        self._json(200, {"tag": tag, "summary": res["summary"], "warnings": res.get("warnings", []), "reports": [{"name": r["name"], "summary": r["summary"]} for r in res["reports"]],
                         "html_url": f"/audits/{tag}/report.html", "json_url": f"/audits/{tag}/result.json", "log_url": f"/audits/{tag}/run.log"})

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--llm-base-url", default=DEFAULT_BASE_URL); ap.add_argument("--llm-model", default=DEFAULT_MODEL)
    args = ap.parse_args(); CFG["base_url"] = args.llm_base_url; CFG["model"] = args.llm_model
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"audit api on http://{args.host}:{args.port}  (POST /audit, GET /audits/<tag>/report.html)", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
