#!/usr/bin/env python3
"""영업이익 전망 보고서 한 건(또는 여러 건)을 끝까지 감사하는 원샷 실행기.

단계 (GUIDE.md 참조):
  1. 전처리        지수 표기·콤마 없는 큰 수·nan 정규화 (case_preprocess)
  2. DART 대조     표 셀·분기 영업이익을 DART 공시와 셀 단위 대조, 표 단위 확정 (dart_crosscheck)
  3. 청크 누적 읽기 번호 붙인 원장(D/T/Q/N/P/C/F/E/R/S)에 근거·충돌 인용, 결론(전망) 추론 감사 (check_case_llm_chunked)
  4. 교정          자료 층 결정적 정정, 충돌 문장 최소 수정 + 근거 전파, 결론 수정 제안 (revise_case_sentences)
  5. 렌더          문서별 "문장 번호·인용 표시 + 결론 감사" HTML (render_llm_chunked_view)

사용:
  python3 -m report_audit.run_report_audit path/to/보고서.md [more.md ...] [--out-dir DIR] [--tag TAG]
         [--llm-base-url http://127.0.0.1:30000/v1] [--llm-model dfischermittwald/Qwen3.8-27B-NVFP4-DFlash2] [--no-llm]
  --no-llm 이면 1·2 단계와 결정적 정정만 수행한다 (LLM 서버가 없을 때).
결과: DIR/<tag>/result.json (문서별 원장·판정·수정), DIR/<tag>/report.html (표시 페이지), 표준출력에 요약.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.view_css import CSS  # noqa: E402

DEFAULT_MODEL = os.environ.get("REPORT_AUDIT_MODEL") or os.environ.get("OPENAI_MODEL") or "Qwen/Qwen3.8-27B-FP8"
DEFAULT_BASE_URL = os.environ.get("REPORT_AUDIT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "http://127.0.0.1:30000/v1"
OUT_RESULTS = REPO / "runs/report_audit/steps"; OUT_RESULTS.mkdir(parents=True, exist_ok=True)


def _run(cmd: list[str], log: Path) -> None:
    with open(log, "a", encoding="utf-8") as lf:
        lf.write("$ " + " ".join(cmd) + "\n"); lf.flush()
        r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(REPO), env={**os.environ, "REPORT_AUDIT_LLM_THINKING": os.environ.get("REPORT_AUDIT_LLM_THINKING", "0")})
    if r.returncode != 0:
        raise RuntimeError(f"step failed ({r.returncode}): {' '.join(cmd[:3])} — see {log}")


def _latest(pattern: str) -> Path | None:
    files = sorted(OUT_RESULTS.glob(pattern))
    return files[-1] if files else None


def audit_report(paths: list[Path], out_dir: Path, tag: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL, use_llm: bool = True) -> dict:
    """여러 문서를 한 번에 감사한다. 반환: {"summary": ..., "reports": [...], "html": 경로, "json": 경로}"""
    t0 = time.time()
    work = out_dir / tag; work.mkdir(parents=True, exist_ok=True)
    src = work / "src"; src.mkdir(exist_ok=True)
    names = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            raise FileNotFoundError(p)
        dst = src / (p.stem + ".md")
        shutil.copy(p, dst); names.append(p.stem)
    log = work / "run.log"; log.write_text("", encoding="utf-8")
    only = ",".join(names)
    # 2. DART 대조 (전처리는 각 단계가 읽을 때 normalize_numbers 로 수행)
    _run([sys.executable, "-m", "report_audit.dart_crosscheck", "--base", str(src), "--only", only, "--out-tag", tag], log)
    xc = json.load(open(_latest(f"dart_crosscheck_{tag}_*.json"), encoding="utf-8"))
    result: dict = {"tag": tag, "documents": names, "steps": ["preprocess", "dart_crosscheck"], "reports": []}
    recs: dict[str, dict] = {}
    if use_llm:
        # 3. 청크 누적 읽기 + 결론 감사
        _run([sys.executable, "-m", "report_audit.check_case_llm_chunked", "--base", str(src), "--names", only, "--llm-base-url", base_url, "--llm-model", model, "--out-tag", f"extra_{tag}"], log)
        ch = json.load(open(_latest(f"case_llm_chunked_extra_{tag}_*.json"), encoding="utf-8"))
        recs = {r["name"]: r for r in ch["reports"]}
        # 4. 교정 (문장 수정 + 근거 전파 + 결론 수정 제안)
        _run([sys.executable, "-m", "report_audit.revise_case_sentences", "--names", only, "--llm-base-url", base_url, "--llm-model", model], log)
        rv = json.load(open(_latest("case_llm_revisions_*.json"), encoding="utf-8"))
        for r in rv["reports"]:
            if r["name"] in recs:
                recs[r["name"]]["revisions"] = r
        result["steps"] += ["chunked_reading", "conclusion_audit", "revisions", "propagation", "conclusion_fix"]
    # 5. 렌더
    from report_audit.render_llm_chunked_view import render_report
    xrec = {r["name"]: r for r in xc["reports"]}
    parts = ['<title>영업이익 전망 보고서 LLM 감사</title>', f'<style>{CSS}</style><div class="tip" id="tip"></div><div class="wrap">',
             '<div class="eyebrow">Report audit · DART-grounded · LLM</div><h1>영업이익 전망 보고서 LLM 감사</h1>',
             f'<p class="lede">{html.escape(", ".join(names))} — 표·분기·전망의 DART 대조, 번호 붙인 원장 인용, 결론 추론 감사, 교정 제안. 마우스를 올리면 근거가 보인다.</p>']
    for nm in names:
        x = xrec.get(nm, {})
        cells = x.get("cells", {}); qs = x.get("quarters", {})
        summ = {"corp_code": x.get("corp_code"), "unit": x.get("unit_status"), "table_cells": {k: sum(1 for v in cells.values() if v.split(":")[0] == k) for k in ("match", "match_restated", "match_controlling", "match_other_basis", "mismatch", "unit_error")},
                "quarters": {k: sum(1 for v in qs.values() if v == k) for k in ("match", "mismatch", "unit_error")}}
        rec = recs.get(nm)
        if not x.get("corp_code"):
            result.setdefault("warnings", []).append(f"{nm}: DART 회사 매핑 실패 — 파일 이름이 DART 회사명(또는 scripts/check_case_forecasts.py ALIAS)과 맞아야 자료 대조가 된다")
        if rec:
            a = rec.get("conclusion_audit") or {}; rv_ = rec.get("revisions") or {}; cf = rv_.get("conclusion_fix") or {}
            summ.update({"sentences": rec.get("n_sentences"), "supported_refs": rec.get("n_supported_refs"),
                         "narrative_conflicts": sum(1 for c in rec["conflicts"] if c.get("layer") == "narrative"), "data_conflicts": rec.get("n_data_conflicts"),
                         "verdict": a.get("verdict"), "verdict_llm": a.get("verdict_llm"), "downgrade_reason": a.get("downgrade_reason"), "top_issue": a.get("top_issue"),
                         "reason_types": a.get("reason_types"), "revisions": {k: sum(1 for y in rv_.get("revisions", []) if y["status"] == k) for k in ("accepted", "propagated", "held", "unchanged")},
                         "data_fixes": len(rv_.get("data_fixes", [])), "conclusion_fix": {"status": cf.get("status"), "revised_forecast": cf.get("revised_forecast"), "derivation": cf.get("derivation")} if cf else None})
            parts.append(f'<h3 style="margin-top:18px">{html.escape(nm)} — 문장 번호·인용 표시 + 결론 감사</h3>')
            parts.append(render_report(rec))
        else:
            parts.append(f'<h3 style="margin-top:18px">{html.escape(nm)} — DART 대조만 (LLM 미사용)</h3><p class="note">{html.escape(str(summ))}</p>')
        result["reports"].append({"name": nm, "summary": summ, "crosscheck": x, **({"record": rec} if rec else {})})
    parts.append('</div><script>const tip=document.getElementById("tip");document.querySelectorAll("[data-t]").forEach(m=>{m.addEventListener("mousemove",e=>{tip.textContent=m.dataset.t;tip.style.opacity=1;tip.style.left=e.clientX+"px";tip.style.top=e.clientY+"px";});m.addEventListener("mouseleave",()=>tip.style.opacity=0);});</script>')
    html_path = work / "report.html"; html_path.write_text("\n".join(parts), encoding="utf-8")
    result["summary"] = {"documents": len(names), "seconds": round(time.time() - t0, 1), "llm": use_llm, "model": model if use_llm else None}
    def _rel(x: Path) -> str:  # 결과 파일 안에는 저장소 기준 상대 경로만 남긴다
        try:
            return str(x.resolve().relative_to(REPO))
        except ValueError:
            return str(x)
    result["html"] = _rel(html_path); result["json"] = _rel(work / "result.json")
    json.dump(result, open(work / "result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+"); ap.add_argument("--out-dir", default=str(REPO / "runs/report_audit"))
    ap.add_argument("--tag", default=None); ap.add_argument("--llm-base-url", default=DEFAULT_BASE_URL); ap.add_argument("--llm-model", default=DEFAULT_MODEL)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    tag = args.tag or time.strftime("run_%Y%m%d_%H%M%S")
    res = audit_report([Path(p) for p in args.paths], Path(args.out_dir), tag, args.llm_base_url, args.llm_model, not args.no_llm)
    for r in res["reports"]:
        s = r["summary"]
        print(f"[{r['name']}] 표 단위: {s.get('unit')} | 표 셀 {s['table_cells']} | 분기 {s['quarters']}")
        if "verdict" in s:
            print(f"    문장 {s['sentences']} · 근거 인용 {s['supported_refs']} · 서술 충돌 {s['narrative_conflicts']} · 자료 충돌 {s['data_conflicts']}")
            print(f"    결론 감사: {s['verdict']}" + (f" (LLM {s['verdict_llm']} → 강등: {s['downgrade_reason']})" if s.get("verdict_llm") else "") + f" | 결함 유형 {s.get('reason_types')}")
            print(f"    교정: 자료 정정 {s['data_fixes']} · 문장 수정 {s['revisions']} · 결론 수정 {s['conclusion_fix'] and s['conclusion_fix']['status']} {s['conclusion_fix'] and s['conclusion_fix']['revised_forecast']}")
    for w in res.get("warnings", []):
        print("경고:", w)
    print(f"HTML: {REPO / res['html']}\nJSON: {REPO / res['json']}\n{res['summary']}")


if __name__ == "__main__":
    main()
