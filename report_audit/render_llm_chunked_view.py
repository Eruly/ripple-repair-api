#!/usr/bin/env python3
"""check_case_llm_chunked.py 결과 한 건을 HTML 조각으로 렌더링한다.

문장(S#)마다 LLM 이 준 supports(근거 id)·conflicts(충돌 id)를 표시로 바꾼다. 문장 앞에 S# 번호를 작게 붙이고,
뒷받침되는 문장은 녹색 밑줄, 충돌 문장은 붉은 테두리로 감싸며, 툴팁에 근거·충돌 원장 항목의 본문을 넣는다.
자료 청크(표·분기·뉴스·주가)는 접힌 상태로 보이고, 원장은 id 순 표로 붙인다.
"""
from __future__ import annotations

import html
import re
import json
import sys


def _diff_html(orig: str, revised: str) -> str:
    """원문과 수정문의 토큰 차이를 표시: 삭제는 취소선, 추가는 굵게."""
    import difflib
    a = re.findall(r"\S+|\s+", orig); b = re.findall(r"\S+|\s+", revised)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape("".join(b[j1:j2])))
        else:
            if i2 > i1:
                out.append(f'<s style="color:var(--conf)">{html.escape("".join(a[i1:i2]))}</s>')
            if j2 > j1:
                out.append(f'<b style="color:var(--fid)">{html.escape("".join(b[j1:j2]))}</b>')
    return "".join(out)


def render_audit(rep: dict, ledger: dict | None = None) -> str:
    """결론(전망) 추론 감사 블록."""
    ledger = ledger if ledger is not None else {f["id"]: f for f in rep.get("ledger", [])}
    a = rep.get("conclusion_audit")
    if not a or a.get("skipped"):
        return ""
    parts = []
    col = {"supported": "var(--fid)", "partially": "var(--narr)", "unsupported": "var(--conf)"}.get(a.get("verdict"), "var(--lat)")
    parts.append(f'<h4 style="margin:14px 0 6px">결론(전망) 추론 감사 — <span style="color:{col}">{html.escape(a.get("verdict",""))}</span></h4>')
    parts.append(f'<p class="note"><b>결론</b> {html.escape(a.get("conclusion",""))}<br><b>요약</b> {html.escape(a.get("summary",""))}<br><b>가장 큰 결함</b> {html.escape(a.get("top_issue",""))}</p>')
    rows = "".join(f'<tr><td>{html.escape(str(p["s"]))}</td><td style="color:{ {"yes":"var(--fid)","weak":"var(--narr)","no":"var(--conf)"}.get(p["supports_conclusion"],"var(--ink)") }">{html.escape(p["supports_conclusion"])}</td><td>{html.escape(", ".join(p["grounded"]) or "원장에 없음")}</td><td>{html.escape(p["why"][:160])}</td></tr>' for p in a.get("premises", []))
    rows += "".join(f'<tr><td>{html.escape(str(x["r"]))}</td><td style="color:{"var(--fid)" if x["consistent"] else "var(--conf)"}">{"정합" if x["consistent"] else "불일치"}</td><td>{html.escape(ledger.get(x["r"], {}).get("text", "")[:70])}</td><td>{html.escape(x["why"][:160])}</td></tr>' for x in a.get("arithmetic", []))
    parts.append('<div class="tw" style="overflow:auto"><table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr>' + "".join(f'<th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--rule)">{h}</th>' for h in ("전제·검산", "결론 뒷받침", "근거 id", "판정 이유")) + "</tr></thead><tbody>" + rows + "</tbody></table></div>")
    return "".join(parts)


def _unit_line(rep: dict) -> str:
    try:
        from report_audit.render_case_report_view import _crosscheck_record
        rec = _crosscheck_record(rep["name"]) or {}
    except Exception:  # noqa: BLE001
        rec = {}
    st = rec.get("unit_status")
    if not st:
        return ""
    col = "var(--fid)" if rec.get("unit_k") == 0 and not rec.get("unit_label_inferred") else ("var(--der)" if rec.get("unit_label_inferred") else "var(--conf)")
    return f'<div style="margin:4px 0 6px;padding:6px 10px;border-left:3px solid {col};background:var(--panel);font-size:13px"><b>표 단위</b> {html.escape(st)} <span style="color:var(--ink-3)">· DART 와 10ⁿ배 관계가 성립한 셀 {rec.get("unit_cells_used",0)}개 기준</span></div>'


def render_report(rep: dict, max_ledger_rows: int = 80) -> str:
    ledger = {f["id"]: f for f in rep.get("ledger", [])}
    unit_done = False
    parts = [f'<div class="meta">LLM 청크 누적 읽기 — 문장 {rep.get("n_sentences", 0)}개 · LLM 호출 {rep.get("n_llm_calls", 0)}회 · 원장 항목 {len(ledger)}개 · 근거 인용 {rep.get("n_supported_refs", 0)}건 · 충돌 {len(rep.get("conflicts", []))}건 · {rep.get("seconds", 0):.0f}초</div>']
    body = []
    revs = {r["s"]: r for r in (rep.get("revisions") or {}).get("revisions", [])}
    dfix = {f["ref"]: f for f in (rep.get("revisions") or {}).get("data_fixes", [])}
    audit = rep.get("conclusion_audit") or {}
    prem = {p["s"]: p for p in audit.get("premises", [])} if audit and not audit.get("skipped") else {}
    last_section = rep["chunks"][-1]["section"] if rep.get("chunks") else None
    for ch in rep["chunks"]:
        if ch.get("data"):
            d = ch["data"]; data_html = html.escape(d[:1500]) + ("…" if len(d) > 1500 else "")
            if d.startswith("|") and not unit_done:
                body.append(_unit_line(rep)); unit_done = True
            body.append(f'<details style="margin:6px 0"><summary style="cursor:pointer;color:var(--ink-2)">{ch["section"]}절 자료 — 표 T#·분기 Q#·뉴스 N#·주가 P# 로 원장에 입력됨</summary><pre style="white-space:pre-wrap;font-size:12px;margin:6px 0">{data_html}</pre></details>')
            sec_fixes = [f for fid, f in dfix.items() if ledger.get(fid, {}).get("source", "").startswith(f"{ch['section']}절")]
            if sec_fixes:
                rows = "".join(f'<tr><td>{html.escape(f["cell"])}</td><td style="text-align:right">{f["reported"]:,.1f}억</td><td style="text-align:right;color:var(--fid)"><b>{f["corrected"]:,.1f}억</b></td><td style="color:var(--ink-2)">{html.escape(f["why"])}</td></tr>' for f in sec_fixes)
                body.append('<details style="margin:2px 0 8px"><summary style="cursor:pointer;color:var(--conf)">정정 제안 ' + str(len(sec_fixes)) + '건 — 표·분기 셀을 DART 값으로 (결정적)</summary><div class="tw" style="overflow:auto"><table style="border-collapse:collapse;width:100%;font-size:12.5px"><thead><tr><th style="text-align:left;padding:4px 8px">셀</th><th style="text-align:right;padding:4px 8px">보고서</th><th style="text-align:right;padding:4px 8px">정정</th><th style="text-align:left;padding:4px 8px">근거</th></tr></thead><tbody>' + rows + '</tbody></table></div></details>')
        ann = {a["s"]: a for a in ch.get("annotations", [])}
        lines = []
        verdict_inserted = False

        def verdict_block() -> str:
            col = {"supported": "var(--fid)", "partially": "var(--narr)", "unsupported": "var(--conf)"}.get(audit.get("verdict"), "var(--lat)")
            dg = f' <span style="color:var(--ink-3);font-size:12.5px">(LLM 판정 {html.escape(audit.get("verdict_llm",""))} → 검산으로 강등: {html.escape(audit.get("downgrade_reason",""))})</span>' if audit.get("verdict_llm") else ""
            out = [f'<div style="margin:8px 0;padding:8px 10px;border-left:3px solid {col};background:var(--panel)"><b>결론 감사 판정: <span style="color:{col}">{html.escape(audit.get("verdict",""))}</span></b>{dg} — {html.escape(audit.get("summary",""))}<br><span style="color:var(--ink-2)">가장 큰 결함: {html.escape(audit.get("top_issue",""))}</span>']
            for x in audit.get("arithmetic", []):
                rt = ledger.get(x["r"], {}).get("text", "")
                cls = "v-fact+id" if x["consistent"] else "v-conflict"
                out.append(f'<br><sup style="font-size:9px;color:var(--ink-3)">{html.escape(str(x["r"]))}</sup> <mark class="{cls}" data-t="{html.escape(x["why"])}">{html.escape(rt)}</mark>')
            out.append('<br><span style="color:var(--ink-3);font-size:12px">아래 이유 문장에는 전제 판정(녹색 뒷받침 · 주황 약함 · 붉은 테두리 뒷받침하지 않음)과 결함 유형(원장과 충돌 · 잘못된 추론 · 논리적 비약 · 근거 없음)이 붙어 있다.</span>')
            rts = audit.get("reason_types") or {}
            if any(rts.values()):
                out.append('<br><span style="color:var(--ink-2);font-size:12.5px">결함 유형 집계: ' + " · ".join(f"{ {'conflict':'원장과 충돌','invalid_inference':'잘못된 추론','non_sequitur':'논리적 비약','ungrounded':'근거 없음'}[k] } {v}" for k, v in rts.items() if v) + "</span>")
            cf = (rep.get("revisions") or {}).get("conclusion_fix") or {}
            if cf and not cf.get("error") and cf.get("status") in ("accepted", "held", "keep"):
                rf = cf.get("revised_forecast") or {}
                colf = {"accepted": "var(--fid)", "held": "var(--narr)", "keep": "var(--ink-2)"}[cf["status"]]
                if cf["status"] == "keep":
                    out.append(f'<div style="margin-top:8px;padding:6px 10px;border-left:3px solid {colf}"><b>결론 수정 제안:</b> 결론 유지 — {html.escape(cf.get("note",""))}</div>')
                else:
                    fmt = lambda v: "–" if v is None else (f"{v:,.1f}억" + (f" ({v/1e4:,.2f}조)" if abs(v) >= 1e4 else ""))  # noqa: E731
                    unit = cf.get("unit_fix_candidate")
                    out.append(f'<div style="margin-top:8px;padding:8px 10px;border-left:3px solid {colf};background:var(--bg)"><b style="color:{colf}">→ 결론 수정 제안</b>'
                               + (f' <span style="color:var(--ink-3)">(보류: {"; ".join(c["detail"] for c in cf.get("checks",[]) if not c["ok"])})</span>' if cf["status"] == "held" else "")
                               + f'<br>상반기 <b>{fmt(rf.get("H1"))}</b> · 하반기 <b>{fmt(rf.get("H2"))}</b> · 연간 <b>{fmt(rf.get("FY"))}</b>'
                               + (f'<br><span style="color:var(--conf)">단위 오류 후보:</span> {html.escape(unit["why"])}' if unit else "")
                               + f'<br><span style="color:var(--ink-2)">유도: {html.escape(cf.get("derivation",""))}</span>'
                               + f'<br><span style="color:var(--ink-3);font-size:12px">근거 {html.escape(", ".join(cf.get("basis",[])))} · 검산 {" · ".join(("✓ " if c["ok"] else "✗ ") + c["name"] for c in cf.get("checks",[]))}</span>')
                    for e in cf.get("premise_edits", []):
                        if e["action"] == "remove":
                            out.append(f'<br><span style="color:var(--conf)">{html.escape(e["s"])} 삭제</span> <s style="color:var(--ink-3)">{html.escape(e["original"][:120])}</s> <span style="color:var(--ink-3)">— {html.escape(e["why"][:100])}</span>')
                        elif e.get("revised"):
                            tagc = "var(--fid)" if e["status"] == "accepted" else "var(--narr)"
                            out.append(f'<br><span style="color:{tagc}">{html.escape(e["s"])} 수정{"" if e["status"]=="accepted" else " (보류)"}</span> {_diff_html(e["original"], e["revised"])}')
                    out.append("</div>")
            out.append("</div>")
            return "".join(out)

        sents_ = ch["sentences"]
        is_fc = lambda t: bool(re.search(r"\d{4}년\s*(상반기|하반기|영업이익)\s*전망", t))  # noqa: E731
        # 결론 절의 전망 수치 줄: 감사 판정 색으로 수치를 표시하고, 그 수치에 해당하는 검산 R# 배지를 붙인다
        pending_fc = {"which": None}

        def forecast_line(text: str, which_override: str | None = None) -> str:
            m = re.search(r"(-?[\d,]+(?:\.\d+)?)\s*(원)?\s*$", text)
            if not m:
                # 시나리오형 헤더("- 2026년 상반기 전망 (1분기 + 2분기 전망):") — 다음 '중간:' 줄에 표시를 넘긴다
                pending_fc["which"] = "H1" if "상반기" in text else ("H2" if "하반기" in text else "FY")
                return f'<span style="color:var(--ink-2)">{html.escape(text)}</span>'
            which = which_override or ("H1" if "상반기" in text else ("H2" if "하반기" in text else "FY"))
            v_won = float(m.group(1).replace(",", "")); v_bn = v_won / 1e8
            conv = f"{v_bn:,.1f}억" + (f" ({v_bn/1e4:,.2f}조)" if abs(v_bn) >= 1e4 else "")
            verdict = audit.get("verdict", "")
            cls = {"supported": "v-fact+id", "partially": "v-narr", "unsupported": "v-conflict"}.get(verdict, "v-none")
            arith = {x["r"]: x for x in audit.get("arithmetic", [])}
            badges = []
            for fid, f in ledger.items():
                if not fid.startswith("R"):
                    continue
                t = f["text"]
                mine = ((which == "FY" and ("+ 하반기" in t or "연간 전망" in t or "컨센서스" in t)) or
                        (which == "H1" and ("2026/Q1" in t or "2026 상반기 전망" in t or "사후 실현" in t)) or
                        (which == "H2" and "하반기" in t and "+ 하반기" not in t))
                if not mine:
                    continue
                a = arith.get(fid)
                posthoc = t.startswith("사후 실현")
                if posthoc:
                    col, sym = "var(--der)", "사후"
                elif a is None:
                    col, sym = "var(--ink-3)", "·"
                else:
                    col, sym = ("var(--fid)", "✓") if a["consistent"] else ("var(--conf)", "✗")
                tip = t + (f" — {a['why']}" if a else "")
                badges.append(f'<sup style="font-size:10px;color:{col};cursor:help" data-t="{html.escape(tip)}">{fid} {sym}</sup>')
            label = html.escape(text[:m.start()]); num = html.escape(m.group(0).strip())
            vt = {"supported": "감사 판정 supported: 전제가 접지되고 결론을 뒷받침", "partially": "감사 판정 partially: 일부 전제 미접지 또는 약함", "unsupported": "감사 판정 unsupported: 핵심 전제 없음 또는 결론과 어긋남"}.get(verdict, "감사 없음")
            return (f'<span style="color:var(--ink-2)">{label}</span><mark class="{cls}" data-t="{html.escape(vt + " | " + (audit.get("summary") or ""))}"><b>{num}</b></mark>'
                    f' <span style="color:var(--ink-3);font-size:12.5px">= {conv}</span> ' + " ".join(badges))

        for si, s in enumerate(sents_):
            txt = html.escape(s["text"])
            # 시나리오형 전망의 '중간:' 줄은 문장 번호가 붙어 있어도 결론 수치로 표시한다
            if ch["section"] == last_section and prem and pending_fc["which"] and re.match(r"^[+\-*\s]*중간\s*[:：]\s*-?[\d,]+", s["text"]):
                lines.append(forecast_line(s["text"], pending_fc["which"])); pending_fc["which"] = None
                nxt = sents_[si + 1]["text"] if si + 1 < len(sents_) else ""
                if not verdict_inserted and not re.search(r"전망", nxt) and not re.match(r"^[+\-*\s]*(상방|하방|중간)", nxt):
                    lines.append(verdict_block()); verdict_inserted = True
                continue
            if not s["id"]:
                if ch["section"] == last_section and prem and is_fc(s["text"]):
                    lines.append(forecast_line(s["text"]))
                elif ch["section"] == last_section and prem and pending_fc["which"] and re.match(r"^[+\-*\s]*중간\s*[:：]\s*-?[\d,]+", s["text"]):
                    lines.append(forecast_line(s["text"], pending_fc["which"])); pending_fc["which"] = None
                    nxt = sents_[si + 1]["text"] if si + 1 < len(sents_) else ""
                    if not verdict_inserted and not re.search(r"전망", nxt) and not re.match(r"^[+\-*\s]*(상방|하방|중간)", nxt):
                        lines.append(verdict_block()); verdict_inserted = True
                else:
                    lines.append(f'<span style="color:var(--ink-2)">{txt}</span>')
                # 결론 절: 전망 수치 줄(상반기·하반기·연간)의 마지막 줄 바로 아래에 감사 판정을 넣는다
                nxt = sents_[si + 1]["text"] if si + 1 < len(sents_) else ""
                if ch["section"] == last_section and prem and not verdict_inserted and is_fc(s["text"]) and not is_fc(nxt):
                    lines.append(verdict_block()); verdict_inserted = True
                continue
            a = ann.get(s["id"]); tag = f'<sup style="font-size:9px;color:var(--ink-3)">{s["id"]}</sup> '
            rv_any = revs.get(s["id"])
            prop_html = ""
            if rv_any and rv_any.get("status") in ("propagated", "held") and rv_any.get("propagated_from") and rv_any.get("revised") and rv_any["revised"] != s["text"].strip():
                col = "var(--der)" if rv_any["status"] == "propagated" else "var(--narr)"
                lab = ("함께 수정 (근거 문장 " + ", ".join(rv_any["propagated_from"]) + " 의 교정에 따라)") if rv_any["status"] == "propagated" else ("함께 수정 (보류: 원장에 없는 숫자 " + ", ".join(rv_any.get("ungrounded_numbers", [])) + ")")
                prop_html = f'<div style="margin:2px 0 6px 18px;padding:6px 10px;border-left:3px solid {col};background:var(--panel);font-size:13.5px"><span style="color:{col};font-weight:600">⇢ {html.escape(lab)}</span> {_diff_html(s["text"], rv_any["revised"])}<br><span style="color:var(--ink-3);font-size:12px">{html.escape(rv_any.get("change",""))} · 근거 {html.escape(", ".join(rv_any.get("basis", [])))}</span></div>'
            pv = prem.get(s["id"]) if ch["section"] == last_section else None
            if pv:
                # 결론 절: 전제 판정(결론 뒷받침 yes/weak/no)을 문장 위에 바로 표시
                cls = {"yes": "v-fact+id", "weak": "v-narr", "no": "v-conflict"}.get(pv["supports_conclusion"], "v-none")
                rt = {"conflict": "원장과 충돌", "invalid_inference": "잘못된 추론", "non_sequitur": "논리적 비약", "ungrounded": "근거 없음"}.get(pv.get("reason_type", ""), "")
                lab = {"yes": "전제 뒷받침", "weak": "전제 약함", "no": "뒷받침하지 않음"}.get(pv["supports_conclusion"], "전제") + (f" · {rt}" if rt and pv["supports_conclusion"] != "yes" else "")
                gtip = "; ".join(f"{i} {ledger[i]['text']}" for i in pv["grounded"] if i in ledger) or "원장에 근거 없음"
                lines.append(tag + f'<mark class="{cls}" data-t="{html.escape(lab + " — " + pv["why"] + " | 근거 " + gtip)}">{txt}<sup style="font-size:9px">{html.escape(lab)} {html.escape(",".join(pv["grounded"]))}</sup></mark>')
                if prop_html: lines.append(prop_html)
                continue
            if not a:
                lines.append(tag + txt)
                if prop_html: lines.append(prop_html)
                continue
            sup_tip = "; ".join(f"{i} {ledger[i]['text']}" for i in a["supports"] if i in ledger)
            con_tip = "; ".join(f"{c['ref']} {ledger.get(c['ref'], {}).get('text', '')} — {c['why']}" for c in a["conflicts"])
            if a["conflicts"]:
                refs = ",".join(c["ref"] for c in a["conflicts"]); types = ",".join(sorted({c["type"] for c in a["conflicts"]}))
                lines.append(tag + f'<mark class="v-conflict" data-t="{html.escape("충돌 [" + types + "] " + con_tip + (" | 근거 " + sup_tip if sup_tip else ""))}">{txt}<sup style="font-size:9px">충돌 {html.escape(refs)}</sup></mark>')
                rv = revs.get(s["id"])
                if rv and rv.get("status") in ("accepted", "held") and rv.get("revised"):
                    col = "var(--fid)" if rv["status"] == "accepted" else "var(--narr)"
                    lab = "수정 제안" if rv["status"] == "accepted" else "수정 제안 (보류: 원장에 없는 숫자 " + ", ".join(rv.get("ungrounded_numbers", [])) + ")"
                    lines.append(f'<div style="margin:2px 0 6px 18px;padding:6px 10px;border-left:3px solid {col};background:var(--panel);font-size:13.5px"><span style="color:{col};font-weight:600">→ {html.escape(lab)}</span> {_diff_html(s["text"], rv["revised"])}<br><span style="color:var(--ink-3);font-size:12px">{html.escape(rv.get("change",""))} · 근거 {html.escape(", ".join(rv.get("basis", [])))}</span></div>')
                elif rv and rv.get("status") == "unchanged" and rv.get("note"):
                    lines.append(f'<div style="margin:2px 0 6px 18px;color:var(--ink-3);font-size:12.5px">→ 수정 보류: {html.escape(rv["note"])}</div>')
            else:
                refs = ",".join(a["supports"])
                lines.append(tag + f'<mark class="v-fact+id" data-t="{html.escape("근거 " + sup_tip)}">{txt}<sup style="font-size:9px">{html.escape(refs)}</sup></mark>')
                if prop_html: lines.append(prop_html)
        if ch.get("error"):
            lines.append(f'<span style="color:var(--conf)">[LLM 오류: {html.escape(str(ch["error"])[:80])}]</span>')
        if ch["section"] == last_section and prem and not verdict_inserted:
            lines.append(verdict_block())
        body.append('<div style="margin:4px 0">' + "<br>".join(lines) + "</div>")
    parts.append('<div class="doc">' + "".join(body) + "</div>")
    if rep.get("conflicts"):
        rows = "".join(f'<tr><td>{html.escape(str(c.get("s","")))}</td><td>{html.escape(str(c.get("type","")))}</td><td>{html.escape(str(c.get("sentence",""))[:110])}</td><td>{html.escape(str(c.get("ref","")))} {html.escape(ledger.get(c.get("ref",""), {}).get("text", "")[:90])}</td><td>{html.escape(str(c.get("why","")))}</td></tr>' for c in rep["conflicts"])
        parts.append('<div class="tw" style="overflow:auto;margin-top:8px"><table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr>' + "".join(f'<th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--rule)">{h}</th>' for h in ("문장", "유형", "주장", "충돌 원장 항목", "이유")) + "</tr></thead><tbody>" + rows + "</tbody></table></div>")
    if prem:
        parts.append('<p class="note">결론 절(마지막 절)의 문장에는 결론 감사의 전제 판정이 함께 표시된다: 녹색 = 전제가 결론을 뒷받침, 주황 = 약함, 붉은 테두리 = 뒷받침하지 않음. 절 끝의 R# 줄이 검산 사실이다.</p>')
    else:
        parts.append(render_audit(rep, ledger))
    facts = rep.get("ledger", [])[:max_ledger_rows]
    rows = "".join(f'<tr><td style="white-space:nowrap">{html.escape(f["id"])}</td><td style="white-space:nowrap">{html.escape(f["source"])}</td><td>{html.escape(f["text"][:200])}</td></tr>' for f in facts)
    parts.append(f'<details style="margin-top:8px"><summary style="cursor:pointer;color:var(--ink-2)">원장 {len(rep.get("ledger", []))}개 항목 (D DART → T 표 → Q 분기 → N 뉴스 → P 주가 → C 컨센서스 → F 전망 → K 앞 청크의 주장)</summary><div class="tw" style="overflow:auto"><table style="border-collapse:collapse;width:100%;font-size:12.5px"><tbody>{rows}</tbody></table></div></details>')
    return "".join(parts)


if __name__ == "__main__":
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(render_report(d["reports"][0])[:3000])
