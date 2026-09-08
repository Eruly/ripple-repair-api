#!/usr/bin/env python3
"""LLM 생성 기업 분석 보고서(CASE_REPORTS_DIR) 한 건을 검증 표시 HTML 로 렌더링.

세 층을 표시한다.
- 표 셀·분기 영업이익·전망: DART 공시와 대조 (dart_crosscheck 결과 + 캐시된 DART 응답). 일치 / 재작성 일치 / 지배주주 기준 일치 /
  10ⁿ배 단위 오류 / 불일치 / DART 미제공.
- 코멘트 숫자: 표·도출값·분기·전망·컨센서스와 대조 (기존).
- 원자료의 자릿수: 7자리 이상인 콤마 없는 정수(예: 21870000000.0)는 끝의 .0 을 떼고 천 단위 구분자를 넣어 표시한다.
"""
from __future__ import annotations

import glob
import html
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.check_case_reports import derived, parse_consensus, parse_forecast, parse_quarters, parse_table, specific_target  # noqa: E402
from report_audit.dart_crosscheck import ACCOUNTS, FULL_ITEMS, dart_all, dart_fin, get_amounts, get_full_amount, get_prior, ni_bases, resolve2  # noqa: E402
from report_audit.case_preprocess import normalize_numbers  # noqa: E402
from report_audit.numfmt import find_numbers  # noqa: E402
STATUS_KO = {"match": "DART 당기 값과 일치", "match_restated": "이듬해 보고서의 전기(재작성) 값과 일치", "match_controlling": "지배기업 소유주 귀속 순이익과 일치",
             "unit_error": "10ⁿ배 단위 오류", "mismatch": "DART 와 불일치", "no_account": "DART 단일계정 API 에 계정 없음", "not_provided": "DART 단일계정 API 미제공(매출총이익)"}
STATUS_CLASS = {"match": "fact+id", "match_restated": "fact+id", "match_controlling": "fact+id", "unit_error": "latent", "mismatch": "conflict"}


def fmt_raw_numbers(line: str) -> str:
    """전처리(case_preprocess.normalize_numbers)와 같은 규칙. 본문은 이미 정규화되어 있으므로 사실상 항등."""
    return normalize_numbers(line)[0]


_XC_CACHE: dict = {}


def _crosscheck_record(name: str) -> dict | None:
    """교차 검증 결과에서 이름으로 레코드를 찾는다. 기본 501건 파일과 다른 폴더용 태그 파일을 모두 보며, 최신 파일이 우선."""
    files = sorted(glob.glob(str(REPO / "runs/report_audit/steps/dart_crosscheck_*.json")), key=lambda f: Path(f).stat().st_mtime, reverse=True)
    for f in files:
        if f not in _XC_CACHE:
            _XC_CACHE[f] = {r["name"]: r for r in json.load(open(f, encoding="utf-8"))["reports"]}
        if name in _XC_CACHE[f]:
            return _XC_CACHE[f][name]
    return None


def _dart_quarters(code: str, years: list[str]) -> dict[str, float]:
    out = {}
    for y in years:
        yi = int(y)
        q1 = get_amounts(dart_fin(code, yi, "11013"), "영업이익", False)
        h1 = get_amounts(dart_fin(code, yi, "11012"), "영업이익", True)
        n9 = get_amounts(dart_fin(code, yi, "11014"), "영업이익", True) if yi <= 2025 else None
        fy = get_amounts(dart_fin(code, yi, "11011"), "영업이익", False) if yi <= 2025 else None
        if q1 is not None: out[f"{y}/Q1"] = q1
        if h1 is not None and q1 is not None: out[f"{y}/Q2"] = h1 - q1
        if n9 is not None and h1 is not None: out[f"{y}/Q3"] = n9 - h1
        if fy is not None and n9 is not None: out[f"{y}/Q4"] = fy - n9
    return out


def _mark(text: str, grade: str, why: str) -> str:
    return f'<mark class="v-{grade}" data-t="{html.escape(why)}">{html.escape(text)}</mark>'


def _table_line_html(line: str, years: list[str] | None, rec: dict | None, code: str | None, facts) -> tuple[str, list[str] | None, Counter]:
    """표 한 줄. 헤더 줄이면 연도 목록을 돌려주고, 데이터 줄이면 셀에 DART 표시를 붙인다."""
    cnt = Counter()
    cells = line.split("|")
    if years is None:
        yrs = [c.strip() for c in cells if re.fullmatch(r"\s*20\d\d\s*", c)]
        if yrs:
            return html.escape(fmt_raw_numbers(line)), yrs, cnt
        return html.escape(fmt_raw_numbers(line)), None, cnt
    item = cells[1].strip() if len(cells) > 1 else ""
    if item not in facts or re.fullmatch(r"\|?\s*-+.*", line):
        return html.escape(fmt_raw_numbers(line)), years, cnt
    out = []
    ycols = [c for c in cells[2:] if c.strip() != ""]
    yi = 0
    for idx, c in enumerate(cells):
        if idx < 2:
            out.append(html.escape(c)); continue
        if c.strip() == "" and idx == len(cells) - 1:
            out.append(html.escape(c)); continue
        y = years[yi] if yi < len(years) else None; yi += 1
        shown = fmt_raw_numbers(c)
        m = re.search(r"-?[\d,]+(?:\.\d+)?", shown)
        if not (m and y and rec and code):
            out.append(html.escape(shown)); continue
        status = rec["cells"].get(f"{item}@{y}")
        if status is None or status in ("not_provided",):
            why = STATUS_KO.get(status or "", "DART 대조 없음")
            out.append(html.escape(shown[:m.start()]) + f'<mark class="v-none" data-t="{html.escape(why)}">{html.escape(m.group(0))}</mark>' + html.escape(shown[m.end():])); continue
        status_key = status.split(":")[0]
        if item in FULL_ITEMS:
            dv = get_full_amount(dart_all(code, int(y), "11011"), item); src = "전체계정"
        else:
            dv = get_amounts(dart_fin(code, int(y), "11011"), item, False); src = "연결"
        why = STATUS_KO.get(status_key, status)
        if dv is not None:
            why += f" — DART {y} 사업보고서({src}) {item} = {dv:,.1f}억"
            v = facts[item].get(y)
            if status_key == "unit_error" and v and dv:
                k = round(math.log10(abs(v / dv))); why += f" (보고서 값은 {10**k:,}배)"
            if status_key == "match_controlling":
                cni = ni_bases(dart_all(code, int(y), "11011")).get("controlling")
                if cni is not None: why += f", 지배주주 귀속 = {cni:,.1f}억"
            if status_key == "match_restated":
                alt = get_full_amount(dart_all(code, int(y) + 1, "11011"), item, "frmtrm_amount") if item in FULL_ITEMS else get_prior(dart_fin(code, int(y) + 1, "11011"), item, "frmtrm_amount")
                if alt is not None: why += f", {int(y)+1} 보고서 전기 값 = {alt:,.1f}억"
        grade = STATUS_CLASS.get(status_key, "none"); cnt[status_key] += 1
        out.append(html.escape(shown[:m.start()]) + _mark(m.group(0), grade, why) + html.escape(shown[m.end():]))
    return "|".join(out), years, cnt


def _quarter_line_html(line: str, rec: dict | None, dq: dict[str, float]) -> tuple[str, Counter]:
    """분기 리스트 줄: 값의 자릿수를 표시하고 DART 도출 분기값과 대조 표시."""
    cnt = Counter(); out = []; p0 = 0
    for m in re.finditer(r"'Date':\s*'(\d{4}/Q[1-4])',\s*'영업이익':\s*(-?[\d,]+(?:\.\d+)?)", line):
        q, raw = m.group(1), m.group(2)
        v = float(raw.replace(",", "")); shown = f"{int(round(v)):,}"
        out.append(html.escape(line[p0:m.start(2)]))
        status = (rec or {}).get("quarters", {}).get(q)
        dv = dq.get(q)
        if status and dv is not None:
            why = f"{STATUS_KO.get(status, status)} — DART 도출 {q} 영업이익 = {dv:,.1f}억 (Q1=1분기보고서, Q2=반기 누적−Q1, Q3=3분기 누적−반기, Q4=연간−3분기 누적); 보고서 {v/1e8:,.1f}억"
            grade = STATUS_CLASS.get(status, "none"); cnt[status] += 1
            out.append(_mark(shown, grade, why))
        else:
            out.append(f'<mark class="v-none" data-t="DART 분기 자료 없음 (2026 반기 미공시 등)">{html.escape(shown)}</mark>')
        p0 = m.end(2)
    out.append(html.escape(line[p0:]))
    return "".join(out), cnt


def render_case(path: Path, max_chars: int = 16000) -> dict:
    text, norm_stats = normalize_numbers(path.read_text(encoding="utf-8", errors="replace"))  # 지수 표기·콤마 없는 큰 수 전처리
    facts, unit = parse_table(text)
    der = derived(facts)
    quarters = parse_quarters(text); forecast = parse_forecast(text); consensus = parse_consensus(text)
    rec = _crosscheck_record(path.stem)
    code = rec["corp_code"] if rec else None
    qyears = sorted({q.split("/")[0] for q in quarters})
    dq = _dart_quarters(code, qyears) if code else {}
    h1_actual = get_amounts(dart_fin(code, 2026, "11012"), "영업이익", True) if code else None
    targets = []
    for it, ys in facts.items():
        for y, v in ys.items():
            targets.append((f"{it} {y}", v, "krw"))
    for it, ys in der.items():
        for y, v in ys.items():
            targets.append((f"{it} {y}", v, "pct"))
    for q, v in quarters.items():
        targets.append((f"영업이익 {q}", v, "krw"))
    for k, v in forecast.items():
        targets.append((f"2026 전망 {k}", v, "krw"))
    for v in consensus:
        targets.append(("컨센서스", v, "krw"))
    body = text[:max_chars]
    is_price = lambda l: re.match(r"\s*\d+\s+\d{4}-\d{2}-\d{2}", l)
    is_forecast = lambda l: re.search(r"전망:\s*-?[\d,]+(?:\.\d+)?\s*(원)?$", l.strip())
    out_lines = []; marks_count = Counter(); dart_count = Counter(); years = None
    for line in body.split("\n"):
        if line.startswith("|"):
            h, years, c = _table_line_html(line, years, rec, code, facts); dart_count.update(c); out_lines.append(h); continue
        if "'Date'" in line:
            h, c = _quarter_line_html(line, rec, dq); dart_count.update(c); out_lines.append(h); continue
        if is_price(line) or re.match(r"\s*- \d{4}\.\d{2}\.\d{2}", line):
            out_lines.append(html.escape(line)); continue
        shown = fmt_raw_numbers(line)
        if is_forecast(line):
            m = re.search(r"-?[\d,]+(?:\.\d+)?", shown.split("전망:")[1])
            if m:
                off = shown.index("전망:") + len("전망:")
                s0, e0 = off + m.start(), off + m.end()
                v = float(m.group(0).replace(",", "")) / 1e8
                why = f"보고서의 2026 전망 {v:,.1f}억"
                grade = "der"
                if "상반기" in shown and h1_actual is not None:
                    err = (v - h1_actual) / abs(h1_actual) * 100 if h1_actual else float("nan")
                    why += f" — DART 2026 반기보고서 실제 영업이익 {h1_actual:,.1f}억, 전망 오차 {err:+.1f}%"
                    grade = "fact+id" if abs(err) <= 10 else ("der" if abs(err) <= 50 else "conflict")
                elif "상반기" in shown:
                    why += " — DART 2026 반기 실적 미공시"
                out_lines.append(html.escape(shown[:s0]) + _mark(m.group(0), grade, why) + html.escape(shown[e0:])); continue
        # 코멘트 숫자: 표·도출값·분기·전망·컨센서스와 대조
        pieces = []; p0 = 0
        for s, e, pn in find_numbers(shown):
            before = shown[max(0, s - 45):s]
            approx = "약" in shown[max(0, s - 6):s]
            if pn.kind == "krw":
                val = pn.value; dec = len(pn.text.split(".")[1].split("조")[0]) if "." in pn.text and "조" in pn.text else 0
                tol = 0.5 * (10 ** (4 - dec) if "조" in pn.text and "억" not in pn.text else 1.0)
                if approx: tol = max(tol, abs(val) * 0.01)
                kind = "krw"
            else:
                val = pn.value / 100.0; dec = len(pn.text.rstrip("%").split(".")[1]) if "." in pn.text else 0
                tol = 0.5 * 10 ** (-dec) / 100.0; kind = "pct"
            if val == 0 or s < p0:
                continue
            spec = specific_target(before, facts, der)
            if shown[e:e + 1] == "대":
                spec = None
            if spec and spec[2] == kind:
                lab, tv, _ = spec; step = tol * 2
                if abs(tv - val) <= tol or (0 <= (tv - val) * (1 if tv >= 0 else -1) < step * 1.001) or abs(abs(tv) - abs(val)) <= tol:
                    grade, why = "fact+id", f"표의 {lab} = {tv:,.1f}억" if kind == "krw" else f"표에서 계산한 {lab} = {tv*100:.1f}%"
                else:
                    unit_hit = next((k for k in (-8, -6, -4, -3, -2, -1, 1, 2, 3, 4, 6, 8) if tv and abs(val * 10 ** k - tv) <= max(tol * (10 ** k if k > 0 else 1), abs(tv) * 0.002)), None)
                    if unit_hit is not None:
                        grade, why = "latent", f"단위 라벨 불일치: 표의 {lab} = {tv:,.1f} 와 10^{unit_hit} 배 관계"
                    else:
                        grade, why = "conflict", f"불일치: 표의 {lab} = " + (f"{tv:,.1f}억" if kind == "krw" else f"{tv*100:.1f}%")
            else:
                c = [(lab, tv) for lab, tv, k in targets if k == kind and abs(tv - val) <= tol]
                if c:
                    grade, why = "fact", f"표·분기·전망·컨센서스 값과 일치: {c[0][0]}"
                else:
                    step = tol * 2
                    t2 = [(lab, tv) for lab, tv, k in targets if k == kind and 0 <= (tv - val) * (1 if tv >= 0 else -1) < step * 1.001]
                    if t2:
                        grade, why = "fact", f"절삭 표기로 일치: {t2[0][0]} = {t2[0][1]:,.1f}"
                    else:
                        grade, why = "none", "표·도출값·분기·전망·컨센서스 어디에도 없음 (뉴스·외부 전망·비재무 수치일 수 있음)"
            marks_count[grade] += 1
            pieces.append(html.escape(shown[p0:s])); pieces.append(_mark(shown[s:e], grade, why)); p0 = e
        pieces.append(html.escape(shown[p0:]))
        out_lines.append("".join(pieces))
    # 분기 합 검사
    qs = {}
    for q, v in quarters.items():
        qs.setdefault(q.split("/")[0], []).append(v)
    qchecks = []
    for y, vs in sorted(qs.items()):
        if len(vs) == 4 and y in facts.get("영업이익", {}):
            a = facts["영업이익"][y]; ssum = sum(vs); ok = abs(a - ssum) <= max(1.0, abs(a) * 0.01)
            qchecks.append((y, a, ssum, ok))
    return {"name": path.stem, "unit": unit, "html": "<br>".join(out_lines), "counts": dict(marks_count), "dart_counts": dict(dart_count), "qchecks": qchecks,
            "forecast": forecast, "truncated": len(text) > max_chars, "dart_mapped": code is not None, "norm_stats": norm_stats}


if __name__ == "__main__":
    r = render_case(Path(sys.argv[1]))
    print(r["name"], r["unit"], r["counts"], r["dart_counts"], r["qchecks"])
