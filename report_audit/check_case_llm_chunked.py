#!/usr/bin/env python3
"""청크 누적 읽기 + 번호 붙인 근거 인용.

문서의 모든 단위에 미리 번호를 붙인다: 표 셀 T#, 분기 값 Q#, 뉴스 N#, 월말 주가 P#, 컨센서스 C#, 전망 F#, 서술 문장 S#.
DART 공시 값은 D# 로 원장(ledger)에 먼저 들어간다(외부 접지). 자료 항목(T·Q·N·P·C·F)은 LLM 없이 결정적으로 파싱해 원장에 넣는다.

LLM 은 서술 청크(절 단위)를 한 번에 하나씩 읽으며, 문장마다
  - 뒷받침하는 원장 항목 id (supports),
  - 어긋나는 원장 항목 id 와 이유·유형 (conflicts),
  - 원장에 없는 새 주장(new_facts, 다음 청크부터 K# 로 원장에 추가)
을 JSON 으로 돌려준다. 문장은 다시 쓰지 않고 id 로만 가리키므로 표시가 원문에 정확히 붙고, 모든 판정이 id 로 추적된다.

사용: python3 -m report_audit.check_case_llm_chunked --only DL [--names a,b] [--limit N] [--workers 1]
      [--llm-base-url http://127.0.0.1:30000/v1] [--llm-model Qwen3.8-27B-NVFP4]
"""
from __future__ import annotations

import os

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.case_preprocess import normalize_numbers  # noqa: E402
from report_audit.check_case_logic import NEWS_RE, PRICE_RE, related_entities  # noqa: E402
from report_audit.check_case_reports import parse_consensus, parse_forecast, parse_quarters, parse_table  # noqa: E402
from report_audit.dart_crosscheck import FULL_ITEMS, dart_all, dart_fin, get_amounts, get_full_amount, ni_bases  # noqa: E402
from report_audit.llm import chat_json as _chat_json, last_usage  # noqa: E402


def chat_json(base_url, model, messages, max_tokens=4000):
    """JSON 파싱 실패(잘린 출력·구분자 오류·문자열 안 따옴표)면 점점 짧게 쓰라는 지시를 붙여 최대 2회 재시도한다."""
    hints = ["\n\n(주의: 직전 출력이 올바른 JSON 이 아니었다. why/summary 는 60자 이내로 짧게, 문자열 안에 큰따옴표를 쓰지 말고, 유효한 JSON 하나만 출력하라.)",
             "\n\n(주의: 두 번 연속 JSON 이 깨졌다. 항목을 핵심 6개 이하로 줄이고 모든 문자열은 40자 이내, 따옴표·줄바꿈 없이, 유효한 JSON 하나만 출력하라.)"]
    last = None
    for attempt in range(3):
        msgs = messages if attempt == 0 else messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + hints[attempt - 1]}]
        try:
            return _chat_json(base_url, model, msgs, max_tokens=max_tokens)
        except (json.JSONDecodeError, ValueError) as ex:
            last = ex
    raise last


from report_audit.render_case_report_view import _crosscheck_record, _dart_quarters  # noqa: E402

BASE = Path(os.environ.get("CASE_REPORTS_DIR", REPO / "data/case_reports"))
OUT = REPO / "runs/report_audit/steps"; OUT.mkdir(parents=True, exist_ok=True)
MD_ROW_RE = re.compile(r"^\|\s*(\d{4})-(\d{2})-(\d{2})\s*\|\s*(-?[\d,.]+|NaN)\s*\|")
CONS_RE = re.compile(r"^\s*\d+\s+\d{4}-\d{2}-\d{2}\s+-?[\d,]+(?:\.\d+)?\s*$")
SENT_SPLIT = re.compile(r"(?<=[.다음됨함임요])\s*\n|(?<=[가-힣)\]])\.\s+|(?<=\d\.)\s{2,}")


def fmt_bn(v: float) -> str:
    """억 단위 값. 1조 이상이면 조 단위를 병기한다 (LLM 의 억↔조 환산 실수 방지)."""
    return f"{v:,.1f}억" + (f"({v / 1e4:,.2f}조)" if abs(v) >= 1e4 else "")


class Ledger:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.counts: dict[str, int] = {}

    def add(self, prefix: str, text: str, source: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        fid = f"{prefix}{self.counts[prefix]}"
        self.items.append({"id": fid, "text": text, "source": source})
        return fid

    def render(self) -> str:
        return "\n".join(f"{f['id']} {f['text']}" for f in self.items)

    def ids(self) -> set[str]:
        return {f["id"] for f in self.items}


# ---------------------------------------------------------------- 문서 단위 번호 매기기
def split_sections(text: str) -> list[dict]:
    out = []
    for sec in re.split(r"\n(?=[1-9]\. )", "\n" + text):
        sec = sec.strip("\n")
        if not sec.strip():
            continue
        m = re.match(r"([1-9])\. ", sec); num = m.group(1) if m else "0"
        data, narr = [], []
        for l in sec.splitlines():
            if l.startswith("|") or "'Date'" in l or PRICE_RE.match(l) or NEWS_RE.match(l) or re.match(r"^\s*-\s*Date\s+(Stock|Amount)", l) or CONS_RE.match(l):
                data.append(l)
            else:
                narr.append(l)
        out.append({"section": num, "data": "\n".join(data), "narrative_lines": narr})
    return out


def number_sentences(lines: list[str], counter: list[int]) -> list[dict]:
    """서술 줄들을 문장으로 나누고 S# 를 붙인다. 헤더·템플릿 줄은 줄 단위로 먼저 걸러 id 없이 통과시킨다."""
    sents = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        if re.match(r"^(\d\.\s|<출력|-\s*\[|-\s*주어진|-\s*이유\s*:?$|\(중간 생략\)|작성일|-\s*\d{4}년\s*(상반기|하반기|영업이익)\s*전망)", raw) or raw.endswith(":") or len(raw) < 8:
            sents.append({"id": None, "text": raw}); continue
        for p in SENT_SPLIT.split(raw):
            p = p.strip()
            if not p:
                continue
            if len(p) < 8:
                sents.append({"id": None, "text": p}); continue
            counter[0] += 1
            sents.append({"id": f"S{counter[0]}", "text": p})
    return sents


def seed_dart(ledger: Ledger, name: str, facts, quarters) -> str | None:
    rec = _crosscheck_record(name); code = rec["corp_code"] if rec else None
    if not code:
        return None
    years = sorted({y for ys in facts.values() for y in ys if y.isdigit() and int(y) <= 2025})
    for y in years:
        d = dart_fin(code, int(y), "11011")
        for it in ("매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"):
            v = get_amounts(d, it, False)
            if v is not None:
                ledger.add("D", f"DART {y} 사업보고서(연결) {it} {fmt_bn(v)}", "DART")
        need_full = [it for it in FULL_ITEMS if it in facts] or ["매출총이익"]
        d_all = dart_all(code, int(y), "11011")
        for it in need_full:
            v = get_full_amount(d_all, it)
            if v is not None:
                ledger.add("D", f"DART {y} 사업보고서(연결, 전체계정) {it} {v:,.1f}억", "DART")
        if "당기순이익" in facts:
            cni = ni_bases(d_all).get("controlling")
            if cni is not None:
                ledger.add("D", f"DART {y} 지배기업 소유주 귀속 당기순이익 {cni:,.1f}억", "DART")
    dq = _dart_quarters(code, sorted({q.split('/')[0] for q in quarters})) if quarters else {}
    for q, v in sorted(dq.items()):
        ledger.add("D", f"DART 도출 {q} 영업이익 {v:,.1f}억", "DART")
    h1 = get_amounts(dart_fin(code, 2026, "11012"), "영업이익", True)
    if h1 is not None:
        ledger.add("D", f"DART 2026 반기보고서(연결) 상반기 영업이익 {h1:,.1f}억 (실제; 보고서 작성일 이후 공개된 사후 실현값)", "DART")
    return code


STATUS_KO = {"match": "DART 일치", "match_restated": "DART 재작성 값과 일치", "match_controlling": "DART 지배주주 귀속 순이익과 일치", "match_other_basis": "DART 다른 순이익 기준과 일치",
             "unit_error": "DART 와 10ⁿ배 차이(단위 오류)", "mismatch": "DART 와 불일치", "no_account": "DART 계정 없음", "not_provided": "DART 미제공"}


def seed_section_data(ledger: Ledger, sec: dict, text_all: str, facts, unit, rec: dict | None, data_conflicts: list) -> None:
    d = sec["data"]
    if not d:
        return
    if d.startswith("|"):
        cells = (rec or {}).get("cells", {})
        for it, ys in facts.items():
            for y, v in sorted(ys.items()):
                if v is not None and v == v:
                    st = cells.get(f"{it}@{y}", ""); st_key = st.split(":")[0]
                    note = f" — {STATUS_KO.get(st_key, '')}" if st_key in STATUS_KO else ""
                    fid = ledger.add("T", f"보고서 표 {it} {y} = {fmt_bn(v)} (표 단위 표기 '{unit}', 억원 환산){note}", f"{sec['section']}절 표")
                    if st_key in ("unit_error", "mismatch"):
                        data_conflicts.append({"ref": fid, "type": "unit" if st_key == "unit_error" else "value", "why": STATUS_KO[st_key], "s": fid, "section": sec["section"],
                                               "sentence": f"표 {it} {y} = {v:,.1f}억", "layer": "data"})
    if "'Date'" in d:
        qst = (rec or {}).get("quarters", {})
        for q, v in sorted(parse_quarters(d).items()):
            st = qst.get(q, ""); note = f" — {STATUS_KO.get(st, '')}" if st in STATUS_KO else ""
            fid = ledger.add("Q", f"보고서 분기 영업이익 {q} = {v:,.1f}억{note}", f"{sec['section']}절 분기 자료")
            if st in ("unit_error", "mismatch"):
                data_conflicts.append({"ref": fid, "type": "unit" if st == "unit_error" else "value", "why": STATUS_KO[st], "s": fid, "section": sec["section"], "sentence": f"분기 영업이익 {q} = {v:,.1f}억", "layer": "data"})
    for l in d.splitlines():
        m = NEWS_RE.match(l)
        if m:
            ledger.add("N", f"뉴스 {m.group(1)}-{m.group(2)}-{m.group(3)}(보도일): {(m.group(5) or '')[:100]}", f"{sec['section']}절 뉴스")
    rows = [(m.group(1), m.group(2), m.group(4)) for l in d.splitlines() for m in [PRICE_RE.match(l) or MD_ROW_RE.match(l)] if m and m.group(4) != "NaN"]
    if rows and re.search(r"Date\s*\|?\s*Amount", d):  # 컨센서스 표 (Date Amount)
        for y, mo, p in rows[-8:]:
            try:
                ledger.add("C", f"컨센서스 영업이익 전망 {y}-{mo} = {fmt_bn(float(p.replace(',', '')) / 1e8)}", f"{sec['section']}절 컨센서스")
            except ValueError:
                continue
    elif rows:  # 주가 표 (Date Stock Price)
        for y, mo, p in rows[-36:]:
            try:
                ptxt = f"{int(float(p.replace(',', ''))):,}"
            except ValueError:
                ptxt = p
            ledger.add("P", f"월말 주가 {y}-{mo} = {ptxt}원", f"{sec['section']}절 주가")


def parse_consensus_any(text: str) -> list[float]:
    """'Date Amount' 표를 문서 어디서든 찾아 컨센서스 값(억)을 읽는다 (절 번호에 의존하지 않음)."""
    m = re.search(r"Date\s*\|?\s*Amount[^\n]*\n((?:[^\n]*\n){0,80})", text)
    if not m:
        return []
    out = []
    for l in m.group(1).splitlines():
        mm = PRICE_RE.match(l) or MD_ROW_RE.match(l)
        if mm and mm.group(4) != "NaN":
            try:
                out.append(float(mm.group(4).replace(",", "")) / 1e8)
            except ValueError:
                pass
        elif re.match(r"^\s*[1-9]\. ", l):
            break
    return out


def seed_entities(ledger: Ledger, name: str, text: str, code: str | None, rows_codes) -> None:
    """뉴스에 나오는 같은 접두 사명의 관계사와 보고서 회사의 DART 연결 매출을 비교해 연결 대상 여부를 원장에 적는다."""
    if not code:
        return
    own = None
    for y in (2024, 2023):
        own = get_amounts(dart_fin(code, y, "11011"), "매출액", False)
        if own:
            own_year = y; break
    if not own:
        return
    for rname, rcode in related_entities(text, name, rows_codes):
        rev = get_amounts(dart_fin(rcode, own_year, "11011"), "매출액", False)
        if rev is None:
            continue
        rel = "연결 매출이 더 크므로 보고서 회사의 연결 대상이 아니다(지분법 관계사)" if rev > own * 1.05 else "연결 자회사일 수 있다"
        ledger.add("E", f"관계사 {rname}: DART {own_year} 연결 매출 {rev:,.0f}억 vs {name} {own:,.0f}억 → {rel}", "DART")


def parse_forecast_any(text: str) -> dict[str, float]:
    """'전망: 값' 형식이 없으면 시나리오형('- … 전망:' 다음 줄들의 '중간: 값')에서 중간 시나리오를 결론 값으로 읽는다."""
    fc = parse_forecast(text)
    if fc:
        return fc
    out = {}
    for k, head in (("H1", r"상반기 전망"), ("H2", r"하반기 전망"), ("FY", r"영업이익 전망")):
        for m in re.finditer(head + r"[^\n]*\n((?:[^\n]*\n){0,4})", text):
            mm = re.search(r"중간\s*[:：]\s*(-?[\d,]+(?:\.\d+)?)", m.group(1))
            if mm:
                out[k] = float(mm.group(1).replace(",", "")) / 1e8; break
    return out


def seed_forecast(ledger: Ledger, text: str) -> None:
    for k, v in parse_forecast_any(text).items():
        lab = {"H1": "2026 상반기", "H2": "2026 하반기", "FY": "2026 연간"}[k]
        ledger.add("F", f"보고서 전망 {lab} 영업이익 {fmt_bn(v)}", "6절 전망")


# ---------------------------------------------------------------- LLM 판정
def judge_chunk(ledger: Ledger, sec: dict, sents: list[dict], base_url: str, model: str) -> dict:
    numbered = [s for s in sents if s["id"]]
    if not numbered or sum(len(s["text"]) for s in numbered) < 40:
        return {"annotations": [], "new_facts": [], "skipped": True}
    chunk_text = "\n".join(f"{s['id']}: {s['text']}" for s in numbered)
    task = (
        "당신은 재무 보고서를 한 청크씩 읽으며 검토하는 도구다. [원장] 은 지금까지 확정된 사실이다: D# DART 공시, T# 보고서 표 셀, Q# 보고서 분기 값, "
        "N# 뉴스(보도일), P# 월말 주가, C# 컨센서스, F# 보고서 전망, K# 앞 청크가 주장한 사실.\n"
        "[이번 청크] 의 문장(S#)마다 판정하라:\n"
        " - supports: 그 문장의 수치·비교·인과·분기 상태 주장을 뒷받침하는 원장 id 목록 (값이 일치하거나 계산이 맞는 경우만).\n"
        " - conflicts: 어긋나는 원장 id 와 유형·이유. 기준: value(값이 다름; 절삭·반올림·'약' 은 허용), unit(10ⁿ배), arithmetic(비율·합·증감률 계산 불일치), "
        "quarter_pattern(분기의 적자/흑자/전환/최대/최저/급변 주장이 Q#·D# 와 다름), temporal(원인으로 든 N# 의 보도일이 설명 대상 기간 종료보다 뒤; "
        "또는 N분기 실적 공개 전(그 분기 종료 이전) 달의 P# 주가 움직임을 그 실적으로 설명), entity(연결 매출이 보고서 회사보다 큰 관계사는 연결 대상이 아닌데 "
        "그 회사의 수주·매출을 자사 매출·영업이익 동인으로 서술), other.\n"
        " - causal: 문장이 어떤 결과(기간)를 어떤 원인(N#·K#·실적)으로 설명하면 {\"effect_period\": \"YYYY-Qn|YYYY-Hn|YYYY-MM|YYYY\", \"cause_refs\": [\"N#\"], \"cause_after_effect\": true|false} 를 적는다. "
        "원인 N# 의 보도일이 effect_period 의 마지막 날보다 뒤이면 cause_after_effect=true 이고 conflicts 에 temporal 로도 넣어야 한다. "
        "주가 움직임(P#)을 어떤 분기 실적으로 설명하는데 그 달이 그 분기 종료 이전이면 역시 temporal.\n"
        " - E# 이 '연결 대상이 아니다' 라고 적힌 관계사의 수주·백로그·매출을 보고서 회사의 매출·마진·백로그 동인으로 쓰면 entity 충돌(ref=E#).\n"
        " - 원장에 근거가 없으면 supports·conflicts 를 비운다. 뉴스 내용 자체의 진위는 판정하지 않는다.\n"
        "이번 청크가 새로 주장하는 사실(원장에 없는 값·사건·전망)은 new_facts 에 {\"text\": 짧은 문장, \"from\": S#} 로 적어라.\n"
        "N분기 실적은 분기 종료 후 약 1~1.5개월 뒤 공개된다. 반드시 JSON 만 출력: "
        '{"annotations": [{"s": "S#", "supports": ["T3","D2"], "conflicts": [{"ref": "Q7", "type": "quarter_pattern", "why": "한 줄"}], "causal": {"effect_period": "2025-Q4", "cause_refs": ["N20"], "cause_after_effect": true}}], '
        '"new_facts": [{"text": "...", "from": "S#"}]}'
    )
    messages = [
        {"role": "system", "content": "재무 보고서 정합성 검토 도구. 주어진 원장만 근거로 삼고 반드시 JSON 만 출력한다."},
        {"role": "user", "content": f"{task}\n\n[원장]\n{ledger.render()}\n\n[이번 청크] ({sec['section']}절)\n{chunk_text}"},
    ]
    t0 = time.time()
    try:
        resp = chat_json(base_url, model, messages, max_tokens=4000); err = None
    except Exception as ex:  # noqa: BLE001
        resp, err = {}, f"{type(ex).__name__}: {ex}"
    ann = resp.get("annotations") if isinstance(resp, dict) and isinstance(resp.get("annotations"), list) else []
    nf = resp.get("new_facts") if isinstance(resp, dict) and isinstance(resp.get("new_facts"), list) else []
    valid = ledger.ids(); sids = {s["id"] for s in numbered}
    clean = []
    for a in ann:
        if not isinstance(a, dict) or a.get("s") not in sids:
            continue
        sup = [x for x in (a.get("supports") or []) if isinstance(x, str) and x in valid]
        con = [{"ref": c.get("ref"), "type": str(c.get("type", "other")), "why": str(c.get("why", ""))[:160]} for c in (a.get("conflicts") or [])
               if isinstance(c, dict) and c.get("ref") in valid]
        causal = a.get("causal") if isinstance(a.get("causal"), dict) else None
        # LLM 이 causal 에서 '원인이 결과보다 뒤' 라고 판정했는데 conflicts 에 temporal 을 빠뜨렸으면 결정적으로 승격한다
        sent_text = next((x["text"] for x in numbered if x["id"] == a.get("s")), "")
        # 승격 조건: 문장에 과거 인과 표현이 있고 기대·전망·예상 같은 선행 기대 표현이 없을 때만 (배치 검토에서 기대감 문장이 오탐의 대부분이었다)
        past_cause = re.search(r"기인|원인|때문|따른|영향으로|반영된 결과|반영된 것|초래|야기", sent_text)
        anticipation = re.search(r"기대|전망|예상|확실시|가능성|될 것|할 것|프리미엄", sent_text)
        if causal and causal.get("cause_after_effect") is True and past_cause and not anticipation and not any(c["type"] == "temporal" for c in con):
            refs = [x for x in (causal.get("cause_refs") or []) if isinstance(x, str) and x in valid]
            if refs:
                con.append({"ref": refs[0], "type": "temporal", "why": f"원인 {refs[0]} 이 설명 대상 기간 {causal.get('effect_period')} 종료보다 뒤 (causal 판정에서 승격)"})
        if sup or con or causal:
            clean.append({"s": a["s"], "supports": sup, "conflicts": con, **({"causal": causal} if causal else {})})
    new_facts = [{"text": str(x.get("text", ""))[:160], "from": x.get("from")} for x in nf if isinstance(x, dict) and x.get("text")][:12]
    return {"annotations": clean, "new_facts": new_facts, "error": err, "seconds": round(time.time() - t0, 1), "usage": last_usage(), "ledger_size": len(ledger.items)}



# ---------------------------------------------------------------- 결론 추론 감사
def derived_forecast_facts(ledger: Ledger, text: str, quarters: dict, code: str | None) -> list[str]:
    """전망 결론을 검산하는 도출 사실 R#: 상반기+하반기=연간, 상반기−Q1 = 함축된 Q2, 2025 실적 대비 성장률, 컨센서스 괴리, DART 반기 실적 대비."""
    fc = parse_forecast_any(text); out = []
    if not fc:
        return out
    h1, h2, fy = fc.get("H1"), fc.get("H2"), fc.get("FY")
    if h1 is not None and h2 is not None and fy is not None:
        out.append(ledger.add("R", f"검산: 상반기 전망 {h1:,.1f} + 하반기 전망 {h2:,.1f} = {h1 + h2:,.1f}억 vs 연간 전망 {fy:,.1f}억 → {'일치' if abs(h1 + h2 - fy) <= max(1, abs(fy) * 0.01) else '불일치'}", "검산"))
    q1 = quarters.get("2026/Q1")
    if h1 is not None and q1 is not None:
        out.append(ledger.add("R", f"검산: 상반기 전망 {h1:,.1f} − 2026/Q1 실적 {q1:,.1f} = 함축된 2026/Q2 {h1 - q1:,.1f}억 (Q1 대비 {((h1 - q1) / q1 - 1) * 100:+.1f}%)" if q1 else f"검산: 상반기 전망 {h1:,.1f}, Q1 실적 0", "검산"))
    yr = {}
    for q, v in quarters.items():
        y = q.split("/")[0]; yr.setdefault(y, []).append(v)
    if "2025" in yr and len(yr["2025"]) == 4 and fy is not None:
        s25 = sum(yr["2025"]); out.append(ledger.add("R", f"검산: 2025 분기 합 {s25:,.1f}억 → 연간 전망 {fy:,.1f}억은 {((fy / s25) - 1) * 100:+.1f}%" if s25 else "검산: 2025 분기 합 0", "검산"))
        h1_25 = sum(yr["2025"][:2]) if len(yr["2025"]) >= 2 else None
        if h1 is not None and h1_25:
            out.append(ledger.add("R", f"검산: 2025 상반기 분기 합 {h1_25:,.1f}억 → 2026 상반기 전망 {h1:,.1f}억은 {((h1 / h1_25) - 1) * 100:+.1f}%", "검산"))
    cons = parse_consensus_any(text)
    if cons and fy is not None and cons[-1]:
        out.append(ledger.add("R", f"검산: 최신 컨센서스 {cons[-1]:,.1f}억 대비 연간 전망 {fy:,.1f}억은 {((fy / cons[-1]) - 1) * 100:+.1f}%", "검산"))
    if code and h1 is not None:
        act = get_amounts(dart_fin(code, 2026, "11012"), "영업이익", True)
        if act is not None:
            out.append(ledger.add("R", f"사후 실현(보고서 작성일 이후 공개, 추론 결함 아님): DART 2026 반기 실제 영업이익 {act:,.1f}억 vs 상반기 전망 {h1:,.1f}억 → 전망 오차 {((h1 - act) / abs(act)) * 100:+.1f}%" if act else "사후 실현: DART 반기 실제 0", "검산"))
    return out


def deterministic_downgrade(ledger, verdict: str) -> dict | None:
    """검산 R# 만으로도 결론이 서지 않는 경우(함축 Q2 가 Q1 대비 ±300% 밖, 연간 전망이 2025 대비 +300% 초과 또는 -80% 미만)
    LLM 판정을 unsupported 로 내린다. LLM 이 supported 를 냈어도 수치가 이를 허락하지 않는다."""
    items = ledger.items if hasattr(ledger, "items") and not isinstance(ledger, dict) else ledger
    reasons = []
    for f in items:
        t = f["text"]
        m = re.search(r"함축된 2026/Q2 (-?[\d,]+(?:\.\d+)?)억 \(Q1 대비 ([+-][\d,]+(?:\.\d+)?)%\)", t)
        if m and abs(float(m.group(2).replace(",", ""))) > 300:
            reasons.append(f"함축된 Q2 가 Q1 대비 {m.group(2)}% ({f['id']})")
        m = re.search(r"2025 분기 합 [\d,.]+억 → 연간 전망 [\d,.]+억은 ([+-][\d,]+(?:\.\d+)?)%", t)
        if m:
            g = float(m.group(1).replace(",", ""))
            if g > 300 or g < -80:
                reasons.append(f"연간 전망이 2025 대비 {m.group(1)}% ({f['id']})")
    if not reasons or verdict == "unsupported":
        return None
    return {"verdict": "unsupported", "why": "검산 강등: " + "; ".join(reasons)}


def audit_conclusion(ledger: Ledger, sents: list[dict], base_url: str, model: str, company: str = "", written: str = "") -> dict:
    """결론 절(전망과 그 이유)의 추론 사슬을 감사한다: 전제의 접지, 전제→결론의 타당성, 검산(R#)과의 정합."""
    numbered = [s for s in sents if s["id"]]
    if not numbered:
        return {"skipped": True}
    chunk_text = "\n".join(f"{s['id']}: {s['text']}" for s in numbered)
    task = (
        f"당신은 재무 보고서의 결론 절을 감사하는 도구다. 보고서 대상 회사는 '{company}' 이고 작성일은 {written or '미상'} 이다. "
        "작성일 이후에 공개된 값('사후 실현' 으로 표시된 D#/R#)은 작성 시점에 알 수 없었으므로 추론 결함의 근거로 쓰지 말고, 전망 정확도 참고로만 언급한다. [원장] 에는 DART 공시 D#, 표 T#, 분기 Q#, 뉴스 N#, 주가 P#, 컨센서스 C#, 보고서 전망 F#, 관계사 E#, 앞 절의 주장 K#, "
        "그리고 결론을 검산한 도출 사실 R# 가 있다. [결론 절] 은 전망 수치와 그 이유(전제)들이다.\n"
        "다음을 판정하라.\n"
        "1) premises: 이유 문장(S#)마다 — 사실 주장이면 뒷받침 id(grounded) 또는 '원장에 없음'; 그 전제가 결론 수치의 방향·크기를 실제로 뒷받침하는지(supports_conclusion: yes|weak|no), 이유.\n"
        "   supports_conclusion 이 weak 나 no 이면 결함 유형 reason_type 을 하나 고른다: "
        "conflict = 전제의 사실 주장이 원장(D#·Q#·T#·N#·P#·C#·R#)과 모순; "
        "invalid_inference = 사실은 맞지만 추론이 잘못됨(연결 대상이 아닌 관계사 E# 의 실적을 자사 실적으로, 결론 기간보다 뒤인 사건을 원인으로, 계산·단위 오류, 일회성을 반복 가정); "
        "non_sequitur = 사실도 추론도 틀리지 않았지만 전제가 결론의 방향·크기와 무관하거나 불충분(정성적 서술만으로 정량 결론, 리스크를 열거하고 증익 결론); "
        "ungrounded = 원장에 근거가 없어 판정할 수 없는 주장. yes 이면 reason_type 은 빈 문자열.\n"
        "2) arithmetic: R# 검산 항목마다 결론과 정합하는지와 이유 문장과의 정합(예: '보수적' 이라면서 컨센서스보다 높음, 'Q1 수준 유지' 라면서 함축 Q2 가 Q1 의 2배).\n"
        "3) verdict: supported(전제가 접지되고 결론을 뒷받침) | partially(일부 전제 미접지 또는 약함) | unsupported(핵심 전제가 없거나 결론과 어긋남) 과 한 줄 요약, 그리고 가장 큰 결함(top_issue).\n"
        '반드시 JSON 만 출력: {"conclusion": "결론 요약", "premises": [{"s": "S#", "grounded": ["N3","R1"], "supports_conclusion": "yes|weak|no", "reason_type": "conflict|invalid_inference|non_sequitur|ungrounded|", "why": "한 줄"}], '
        '"arithmetic": [{"r": "R#", "consistent": true|false, "why": "한 줄"}], "verdict": "supported|partially|unsupported", "summary": "한 줄", "top_issue": "한 줄"}'
    )
    messages = [
        {"role": "system", "content": "재무 보고서 결론 감사 도구. 주어진 원장만 근거로 삼고 반드시 JSON 만 출력한다."},
        {"role": "user", "content": f"{task}\n\n[원장]\n{ledger.render()}\n\n[결론 절]\n{chunk_text}"},
    ]
    t0 = time.time()
    try:
        resp = chat_json(base_url, model, messages, max_tokens=3000); err = None
    except Exception as ex:  # noqa: BLE001
        resp, err = {}, f"{type(ex).__name__}: {ex}"
    if not isinstance(resp, dict):
        resp = {}
    valid = ledger.ids(); sids = {s["id"] for s in numbered}
    prem = [{"s": p.get("s"), "grounded": [g for g in (p.get("grounded") or []) if isinstance(g, str) and g in valid], "supports_conclusion": str(p.get("supports_conclusion", "")),
             "reason_type": str(p.get("reason_type", "")) if str(p.get("reason_type", "")) in ("conflict", "invalid_inference", "non_sequitur", "ungrounded") else ("" if str(p.get("supports_conclusion", "")) == "yes" else ("ungrounded" if not p.get("grounded") else "non_sequitur")),
             "why": str(p.get("why", ""))[:200]}
            for p in (resp.get("premises") or []) if isinstance(p, dict) and p.get("s") in sids]
    arith = [{"r": a.get("r"), "consistent": bool(a.get("consistent")), "why": str(a.get("why", ""))[:200]} for a in (resp.get("arithmetic") or []) if isinstance(a, dict) and a.get("r") in valid]
    verdict = str(resp.get("verdict", ""))[:20]
    override = deterministic_downgrade(ledger, verdict)
    if override:
        verdict_llm, verdict = verdict, override["verdict"]
    return {"conclusion": str(resp.get("conclusion", ""))[:300], "premises": prem, "arithmetic": arith, "verdict": verdict,
            **({"verdict_llm": verdict_llm, "downgrade_reason": override["why"]} if override else {}),
            "summary": str(resp.get("summary", ""))[:300], "top_issue": str(resp.get("top_issue", ""))[:300], "error": err, "seconds": round(time.time() - t0, 1),
            "n_premises": len(prem), "n_ungrounded": sum(1 for p in prem if not p["grounded"]), "n_not_supporting": sum(1 for p in prem if p["supports_conclusion"] == "no"),
            "reason_types": {k: sum(1 for p in prem if p.get("reason_type") == k) for k in ("conflict", "invalid_inference", "non_sequitur", "ungrounded")}}

_CODES = REPO / "data/dart/corp_codes_listed.json"  # python3 -m report_audit.dart_corp_codes 로 생성
ROWS_CODES = json.load(open(_CODES, encoding="utf-8")) if _CODES.exists() else []


def process(path: Path, base_url: str, model: str, audit_only: bool = False) -> dict:
    text, _ = normalize_numbers(path.read_text(encoding="utf-8", errors="replace"))
    facts, unit = parse_table(text); quarters = parse_quarters(text)
    rec = _crosscheck_record(path.stem)
    ledger = Ledger(); code = seed_dart(ledger, path.stem, facts, quarters)
    seed_entities(ledger, path.stem, text, code, ROWS_CODES)
    secs = split_sections(text); counter = [0]; chunks = []; t0 = time.time(); data_conflicts: list = []
    seed_forecast(ledger, text)  # 전망 값은 6절에 있지만 앞 절(2·5절)에서 언급되므로 먼저 넣는다
    last_sec = secs[-1]["section"] if secs else None
    audit = None
    for sec in secs:
        seed_section_data(ledger, sec, text, facts, unit, rec, data_conflicts)
        sents = number_sentences(sec["narrative_lines"], counter)
        if sec["section"] == last_sec:
            derived_forecast_facts(ledger, text, quarters, code)  # 결론 검산 R# 를 원장에
        r = judge_chunk(ledger, sec, sents, base_url, model) if not audit_only else {"annotations": [], "new_facts": [], "skipped": True}
        if sec["section"] == last_sec:
            wm = re.search(r"작성일\s*[:：]\s*(\d{4}[.\-]\d{2}[.\-]\d{2})", text)
            audit = audit_conclusion(ledger, sents, base_url, model, company=path.stem, written=wm.group(1) if wm else "")
        for nf in r.get("new_facts", []):
            ledger.add("K", f"{nf['text']} (출처 {nf.get('from')})", f"{sec['section']}절 서술")
        chunks.append({"section": sec["section"], "data": sec["data"], "sentences": sents, **r})
    conflicts = [dict(c, s=a["s"], section=ch["section"], layer="narrative", sentence=next((s["text"] for s in ch["sentences"] if s["id"] == a["s"]), ""))
                 for ch in chunks for a in ch.get("annotations", []) for c in a["conflicts"]] + data_conflicts
    supports = sum(len(a["supports"]) for ch in chunks for a in ch.get("annotations", []))
    n_sent = sum(1 for ch in chunks for s in ch["sentences"] if s["id"])
    return {"name": path.stem, "dart_mapped": code is not None, "chunks": chunks, "ledger": ledger.items, "conflicts": conflicts, "n_data_conflicts": len(data_conflicts), "conclusion_audit": audit,
            "n_sentences": n_sent, "n_supported_refs": supports, "n_llm_calls": sum(1 for ch in chunks if not ch.get("skipped")) + (1 if audit and not audit.get("skipped") else 0),
            "seconds": round(time.time() - t0, 1), "errors": sum(1 for ch in chunks if ch.get("error"))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only"); ap.add_argument("--names"); ap.add_argument("--limit", type=int); ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:30000/v1")
    ap.add_argument("--llm-model", default="Qwen3.8-27B-NVFP4")
    ap.add_argument("--out-tag", default="")
    ap.add_argument("--audit-only", action="store_true", help="청크 판정 없이 원장을 결정적으로 만들고 결론 감사만 수행")
    ap.add_argument("--base", default=None, help="보고서 폴더 (기본 CASE_REPORTS_DIR)")
    args = ap.parse_args()
    global BASE
    if args.base:
        BASE = Path(args.base)
    files = sorted(BASE.glob("*.md"))
    if args.only:
        files = [f for f in files if f.stem == args.only]
    if args.names:
        want = set(args.names.split(",")); files = [f for f in files if f.stem in want]
    if args.limit:
        files = files[: args.limit]
    ts = time.strftime("%Y%m%d_%H%M%S"); OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"case_llm_chunked_{args.out_tag + '_' if args.out_tag else ''}{ts}.json"
    results = []; t0 = time.time()

    def save():
        from collections import Counter
        types = Counter(c.get("type", "?") for r_ in results for c in r_["conflicts"])
        audits = [r_["conclusion_audit"] for r_ in results if r_.get("conclusion_audit") and not r_["conclusion_audit"].get("skipped")]
        verdicts = Counter(a.get("verdict", "") for a in audits)
        summ = {"reports": len(results), "conclusion_audits": len(audits), "verdicts": dict(verdicts),
                "premises": sum(a.get("n_premises", 0) for a in audits), "premises_ungrounded": sum(a.get("n_ungrounded", 0) for a in audits),
                "premises_not_supporting": sum(a.get("n_not_supporting", 0) for a in audits), "arith_inconsistent": sum(1 for a in audits for x in a.get("arithmetic", []) if not x["consistent"]), "reports_with_conflicts": sum(1 for r_ in results if r_["conflicts"]), "conflicts": sum(len(r_["conflicts"]) for r_ in results),
                "conflicts_by_type": dict(types), "sentences": sum(r_["n_sentences"] for r_ in results), "supported_refs": sum(r_["n_supported_refs"] for r_ in results),
                "llm_calls": sum(r_["n_llm_calls"] for r_ in results), "errors": sum(r_["errors"] for r_ in results),
                "mean_seconds_per_report": round(sum(r_["seconds"] for r_ in results) / max(1, len(results)), 1), "elapsed_sec": round(time.time() - t0, 1), "model": args.llm_model}
        json.dump({"summary": summ, "reports": results}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return summ

    run = lambda f: process(f, args.llm_base_url, args.llm_model, args.audit_only)  # noqa: E731
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for i, r in enumerate(ex.map(run, files), 1):
                results.append(r); save(); print(f"{i}/{len(files)} {r['name']}: conflicts={len(r['conflicts'])} supports={r['n_supported_refs']} calls={r['n_llm_calls']} {r['seconds']}s", flush=True)
    else:
        for i, f in enumerate(files, 1):
            r = run(f); results.append(r); save()
            print(f"{i}/{len(files)} {r['name']}: conflicts={len(r['conflicts'])} supports={r['n_supported_refs']} calls={r['n_llm_calls']} {r['seconds']}s", flush=True)
    print(json.dumps(save(), ensure_ascii=False, indent=1)); print(out)


if __name__ == "__main__":
    main()
