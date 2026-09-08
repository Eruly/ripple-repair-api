#!/usr/bin/env python3
"""LLM 생성 기업 분석 보고서(CASE_REPORTS_DIR, 501건) 정합성 검사.

보고서 구조: 1) 재무제표 요약 표 + 코멘트, 2) 분기별 영업이익 목록, 3) 뉴스, 4) 주가, 5) 컨센서스 표 + 코멘트, 6) 2026 영업이익 전망(상·하반기·연간) + 이유.
검사:
  T. 표 파싱(단위 원/억/조 자동 환산) → 사실(억원). 도출값: 이익률 3종, 부채비율, 연도별 YoY.
  Q. 분기 영업이익 합 = 연간 영업이익 (표) — 내부 일관성.
  F. 6절 상반기+하반기 = 연간; 요약 CSV 의 '중간' = 연간/1e8 — 문서 간 일관성.
  N. 코멘트(1·5·6절)의 숫자 주장을 표·도출값·컨센서스 표·전망값에 정렬: 검증 / 근접 불일치(±5% 안이지만 표기 오차 밖 → 환각 의심) / 미정렬.
사용: python3 -m report_audit.check_case_reports --dir $CASE_REPORTS_DIR/..  (files/*.md 가 있는 폴더의 상위)
"""
from __future__ import annotations

import os

import argparse
import ast
import csv
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.numfmt import find_numbers  # noqa: E402

UNIT = {"원": 1e-8, "억 원": 1.0, "억원": 1.0, "조 원": 1e4, "조원": 1e4, "백만 원": 1e-2, "백만원": 1e-2, "천 원": 1e-5, "천원": 1e-5, "십억 원": 10.0, "십억원": 10.0}
ITEMS = ["매출액", "매출총이익", "매출원가", "판매비와관리비", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"]


def parse_num(s: str) -> float | None:
    s = s.strip().replace(",", "")
    if s in ("", "N/A", "-", "—", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_UNIT_RE = re.compile(r"(조\s?원|십억\s?원|억\s?원|백만\s?원|천\s?원|원)")


def parse_table(text: str) -> tuple[dict[str, dict[str, float]], str | None]:
    sec = text.split("\n2. ")[0]
    unit = None
    m = re.search(r"단위\s*[:：]?\s*" + _UNIT_RE.pattern, sec)
    if m:
        unit = m.group(1).replace(" ", "")
    else:
        m = re.search(r"항목\s*\(?[^|\n]*?" + _UNIT_RE.pattern, sec)
        if m:
            unit = m.group(1).replace(" ", "")
    scale = UNIT.get(unit or "", None)
    facts: dict[str, dict[str, float]] = {}
    cell_unit_rows: set = set()
    header = None
    for line in sec.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            if cells and (cells[0].startswith(("항목", "구분", "지표", "项目")) or cells[0] == "") and any(re.fullmatch(r"20\d\d(\(E\)|E)?", c) for c in cells[1:]):
                header = [re.sub(r"\(E\)|E", "", c) for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        item = cells[0].replace(" ", "")
        for it in ITEMS:
            if it in item or (it == "자본총계" and "자본" in item and "총" in item):
                item = it
                break
        else:
            continue
        for y, c in zip(header[1:], cells[1:]):
            # 셀 자체에 단위가 붙은 표("42.99조 원", "1,234억 원"): 억원으로 바로 환산하고 뒤의 표 배율은 적용하지 않는다
            mc = re.fullmatch(r"\s*(-?[\d,]+(?:\.\d+)?)\s*(조|십억|억|천만|백만|천)\s*원?\s*(\(.*\))?\s*", c)
            if mc:
                v = parse_num(mc.group(1))
                if v is not None and re.fullmatch(r"20\d\d", y):
                    facts.setdefault(item, {})[y] = v * {"조": 1e4, "십억": 10.0, "억": 1.0, "천만": 0.1, "백만": 1e-2, "천": 1e-5}[mc.group(2)]
                    cell_unit_rows.add((item, y))
                continue
            v = parse_num(c)
            if v is not None and re.fullmatch(r"20\d\d", y):
                facts.setdefault(item, {})[y] = v
    if cell_unit_rows:
        n_all = sum(len(ys) for ys in facts.values())
        if len(cell_unit_rows) >= n_all * 0.8:
            # 표 전체가 셀 단위 표기: 이미 억원이므로 배율 1, 단위는 '셀 표기'
            unit = unit or "셀표기"; scale = 1.0
        else:
            # 일부만 셀 단위: 그 셀들은 표 배율을 되돌려 둔다 (뒤에서 scale 을 곱하므로)
            for it, y in cell_unit_rows:
                facts[it][y] /= (scale if scale else 1.0)
    # 단위 미기재: 매출액 크기로 추정 (원: >1e9, 백만원: >1e5, 억원: 그 외)
    if scale is None and facts:
        mag = max((abs(v) for ys in facts.values() for v in ys.values()), default=0)
        scale = 1e-8 if mag > 1e9 else (1e-2 if mag > 2e5 else 1.0)
        unit = (unit or {1e-8: "원", 1e-2: "백만원", 1.0: "억원"}[scale]) + "(추정)"
    for it in facts:
        for y in facts[it]:
            facts[it][y] *= scale
    return facts, unit


def derived(facts: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    d: dict[str, dict[str, float]] = {}
    def ratio(name, num, den):
        for y in facts.get(num, {}):
            if y in facts.get(den, {}) and facts[den][y]:
                d.setdefault(name, {})[y] = facts[num][y] / facts[den][y]
    ratio("영업이익률", "영업이익", "매출액"); ratio("순이익률", "당기순이익", "매출액"); ratio("매출총이익률", "매출총이익", "매출액")
    if "부채총계" in facts and "자산총계" in facts:
        for y in facts["부채총계"]:
            if y in facts["자산총계"] and facts["자산총계"][y] - facts["부채총계"][y]:
                d.setdefault("부채비율", {})[y] = facts["부채총계"][y] / (facts["자산총계"][y] - facts["부채총계"][y])
    for it in ITEMS:
        ys = sorted(facts.get(it, {}))
        for a, b in zip(ys, ys[1:]):
            if int(b) - int(a) == 1 and facts[it][a]:
                d.setdefault(f"{it} YoY", {})[b] = (facts[it][b] - facts[it][a]) / abs(facts[it][a])
    return d


def parse_quarters(text: str) -> dict[str, float]:
    m = re.search(r"\[\{'Date'.*?\}\]", text, re.S)
    if not m:
        return {}
    try:
        rows = ast.literal_eval(re.sub(r"(?<=\d),(?=\d{3})", "", m.group(0)))  # 전처리된 사본의 천 단위 구분자 허용
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue  # 잘린 목록의 '...' 등
        v = r.get("영업이익")
        if isinstance(v, (int, float)) and v == v and "Date" in r:
            out[str(r["Date"])] = float(v) / 1e8  # 원 → 억원
    return out


def parse_forecast(text: str) -> dict[str, float]:
    out = {}
    for k, pat in (("H1", r"상반기 전망:\s*(-?[\d,\.]+)"), ("H2", r"하반기 전망:\s*(-?[\d,\.]+)"), ("FY", r"영업이익 전망:\s*(-?[\d,\.]+)")):
        m = re.search(pat, text)
        if m:
            v = parse_num(m.group(1))
            if v is not None:
                out[k] = v / 1e8
    return out


def parse_consensus(text: str) -> list[float]:
    sec = re.search(r"\n5\. .*?(?=\n6\. |\Z)", text, re.S)
    if not sec:
        return []
    return [float(x.replace(",", "")) / 1e8 for x in re.findall(r"\d{4}-\d{2}-\d{2}\s+(-?[\d.]+e[+-]?\d+|-?\d[\d,]*(?:\.\d+)?)", sec.group(0))]


def narrative_sections(text: str) -> str:
    parts = []
    for n in ("1", "5", "6"):
        m = re.search(rf"\n{n}\. .*?(?=\n[2-7]\. |\Z)", "\n" + text, re.S)
        if m:
            seg = m.group(0)
            # 표·리스트 원자료 제외
            seg = "\n".join(l for l in seg.splitlines() if not l.startswith("|") and "'Date'" not in l and not re.match(r"\s*\d+\s+\d{4}-\d{2}-\d{2}", l) and not re.search(r"전망:\s*-?[\d,]+$", l.strip()))
            parts.append(seg)
    return "\n".join(parts)


KW = [("영업이익률", "영업이익률"), ("순이익률", "순이익률"), ("매출총이익률", "매출총이익률"), ("부채비율", "부채비율"),
      ("매출총이익", "매출총이익"), ("영업이익", "영업이익"), ("당기순이익", "당기순이익"), ("순이익", "당기순이익"), ("매출액", "매출액"), ("매출", "매출액"),
      ("자산총계", "자산총계"), ("부채총계", "부채총계"), ("자본총계", "자본총계")]


def specific_target(ctx_before: str, facts, der):
    """문맥(숫자 앞 45자)에서 숫자에 가장 가까운 지표 키워드와 회계연도를 찾아 (라벨, 값, 종류) 를 돌려준다.
    날짜("2025년 6월"), 컨센서스·전망·증감·대비 문맥, 매출원가·매출채권 등 표에 없는 항목은 제외한다."""
    if re.search(r"컨센서스|전망|추정|예상|증가|감소|늘|줄|대비|성장|확대|축소|분기|Q[1-4]|상반기|하반기|현금흐름|EBITDA|시가총액|주가|배당|대$", ctx_before[-25:]):
        return None
    years = [(m.start(), m.group(1)) for m in re.finditer(r"(20\d\d)년(?!\s*\d+월)", ctx_before)]
    if not years:
        return None
    year = years[-1][1]
    best = None
    for kw, it in KW:
        for m in re.finditer(re.escape(kw), ctx_before):
            if kw == "매출" and re.match(r"매출(원가|채권|총이익|액)", ctx_before[m.start():m.start() + 4]):
                continue
            if best is None or m.start() > best[0]:
                best = (m.start(), it)
    if best is None:
        return None
    metric = best[1]
    if metric.endswith("률") or metric == "부채비율":
        v = der.get(metric, {}).get(year)
        return (f"{metric} {year}", v, "pct") if v is not None else None
    v = facts.get(metric, {}).get(year)
    return (f"{metric} {year}", v, "krw") if v is not None else None


def check_claims(narr: str, facts, der, quarters, forecast, consensus) -> dict:
    # 정렬 대상 값 목록: (라벨, 값, 종류)
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
    if "FY" in forecast and "영업이익" in facts:
        for y, v in facts["영업이익"].items():
            if v:
                targets.append((f"2026 전망 vs {y} 성장률", (forecast["FY"] - v) / abs(v), "pct"))
    res = {"claims": 0, "verified": 0, "near_miss": 0, "unaligned": 0, "conflict_specific": 0, "examples": [], "specific_examples": []}
    for s, e, pn in find_numbers(narr):
        ctx = narr[max(0, s - 30):e + 10].replace("\n", " ")
        before = narr[max(0, s - 45):s]
        approx = "약" in narr[max(0, s - 6):s]
        if pn.kind == "krw":
            val = pn.value
            dec = len(pn.text.split(".")[1].split("조")[0]) if "." in pn.text and "조" in pn.text else 0
            tol = 0.5 * (10 ** (4 - dec) if "조" in pn.text and "억" not in pn.text else 1.0)
            if approx:
                tol = max(tol, abs(val) * 0.01)
            kinds = ("krw",)
        else:
            val = pn.value / 100.0
            dec = len(pn.text.rstrip("%").split(".")[1]) if "." in pn.text else 0
            tol = 0.5 * 10 ** (-dec) / 100.0
            kinds = ("pct",)
        if val == 0:
            continue
        res["claims"] += 1
        spec = specific_target(before, facts, der)
        if narr[e:e + 1] == "대" or narr[e:e + 2] == " 대":
            spec = None
        if spec and spec[2] in kinds:
            lab, tv, _ = spec
            step = tol * 2
            if abs(tv - val) <= tol or (0 <= (tv - val) * (1 if tv >= 0 else -1) < step * 1.001) or abs(abs(tv) - abs(val)) <= tol:
                res["verified"] += 1
                continue
            unit_hit = None
            for k in (-8, -6, -4, -3, -2, -1, 1, 2, 3, 4, 6, 8):
                if tv and abs(val * (10 ** k) - tv) <= max(tol * (10 ** k if k > 0 else 1), abs(tv) * 0.002):
                    unit_hit = k; break
            if unit_hit is not None:
                res["unit_label_mismatch"] = res.get("unit_label_mismatch", 0) + 1
                continue
            res["conflict_specific"] += 1
            if len(res["specific_examples"]) < 3:
                res["specific_examples"].append({"claim": pn.text, "context": ctx, "target": lab, "value": round(tv, 4)})
            continue
        cands = [(lab, tv) for lab, tv, k in targets if k in kinds and abs(tv - val) <= tol]
        if cands:
            res["verified"] += 1
            continue
        # 절삭(내림) 표기 허용: 표기 단위 1 안에서 claim ≤ fact (예: 727.9억 → 727억, 1,293.67 → 1,293)
        unit_step = tol * 2
        trunc = [(lab, tv) for lab, tv, k in targets if k in kinds and 0 <= (tv - val) * (1 if tv >= 0 else -1) < unit_step * 1.001]
        if trunc:
            res["verified"] += 1; res["truncated"] = res.get("truncated", 0) + 1
            continue
        # 부호 무시 근접 (예: -14.3% vs 14.3% 감소)
        near = [(lab, tv) for lab, tv, k in targets if k in kinds and tv and abs(abs(tv) - abs(val)) <= max(tol * 3, abs(tv) * 0.05)]
        if near:
            sign_only = any(abs(abs(tv) - abs(val)) <= tol for _, tv in near)
            if sign_only:
                res["verified"] += 1
                continue
            res["near_miss"] += 1
            if len(res["examples"]) < 3:
                lab, tv = near[0]
                res["examples"].append({"claim": pn.text, "context": ctx, "nearest": lab, "value": round(tv, 4)})
        else:
            res["unaligned"] += 1
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(os.environ.get("CASE_REPORTS_DIR", REPO / "data/case_reports")).parent))
    args = ap.parse_args()
    base = Path(args.dir)
    csv_mid = {}
    for r in csv.DictReader(open(base / "Case_Summary_2026.csv", encoding="utf-8-sig")):
        if r.get("중간"):
            csv_mid[r["기업명"].strip()] = float(r["중간"])
    rows = []
    t0 = time.time()
    for f in sorted((base / "files").glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        name = f.stem
        facts, unit = parse_table(text)
        der = derived(facts)
        quarters = parse_quarters(text)
        forecast = parse_forecast(text)
        consensus = parse_consensus(text)
        rec = {"name": name, "unit": unit, "table_items": len(facts), "years": sorted({y for ys in facts.values() for y in ys})}
        # Q: 분기 합 vs 연간
        qs = {}
        for q, v in quarters.items():
            y = q.split("/")[0]
            qs.setdefault(y, []).append(v)
        qcheck = []
        for y, vs in qs.items():
            if len(vs) == 4 and y in facts.get("영업이익", {}):
                a = facts["영업이익"][y]; ssum = sum(vs)
                ok = abs(a - ssum) <= max(1.0, abs(a) * 0.01)
                kind = "ok"
                if not ok and ssum and a:
                    import math
                    r = abs(a / ssum)
                    if r > 0 and abs(math.log10(r) - round(math.log10(r))) < 0.02 and round(math.log10(r)) != 0:
                        kind = f"unit_x10^{round(math.log10(r))}"
                    else:
                        kind = "data_mismatch"
                qcheck.append({"year": y, "table": round(a, 1), "quarters": round(ssum, 1), "ok": ok, "kind": kind})
        rec["quarter_check"] = qcheck
        # F: 전망 내부 합 + CSV
        if {"H1", "H2", "FY"} <= set(forecast):
            rec["forecast_sum_ok"] = abs(forecast["H1"] + forecast["H2"] - forecast["FY"]) <= max(0.5, abs(forecast["FY"]) * 0.001)
        if "FY" in forecast and name in csv_mid:
            diff = forecast["FY"] - csv_mid[name]
            rec["csv_match"] = abs(diff) <= max(0.5, abs(forecast["FY"]) * 0.001) or (0 <= diff < 1.0)  # 억원 절삭 허용
            rec["csv_vs_doc"] = (csv_mid[name], round(forecast["FY"], 1))
        # N: 코멘트 주장
        rec["claims"] = check_claims(narrative_sections(text), facts, der, quarters, forecast, consensus)
        rows.append(rec)
    n = len(rows)
    parsed = [r for r in rows if r["table_items"] >= 3]
    qc = [c for r in rows for c in r["quarter_check"]]
    fs = [r["forecast_sum_ok"] for r in rows if "forecast_sum_ok" in r]
    cm = [r["csv_match"] for r in rows if "csv_match" in r]
    claims = sum(r["claims"]["claims"] for r in rows); ver = sum(r["claims"]["verified"] for r in rows); nm = sum(r["claims"]["near_miss"] for r in rows); un = sum(r["claims"]["unaligned"] for r in rows)
    cs = sum(r["claims"]["conflict_specific"] for r in rows)
    trunc = sum(r["claims"].get("truncated", 0) for r in rows)
    summary = {
        "reports": n, "verified_by_truncation": trunc, "table_parsed": len(parsed), "units": dict(Counter(r["unit"] for r in rows).most_common(6)),
        "quarter_sum_checks": len(qc), "quarter_sum_consistent": sum(c["ok"] for c in qc), "reports_with_quarter_inconsistency": sum(1 for r in rows if any(not c["ok"] for c in r["quarter_check"])),
        "quarter_mismatch_kinds": dict(Counter(c["kind"] for c in qc if not c["ok"])),
        "reports_with_unit_error": sum(1 for r in rows if any(c["kind"].startswith("unit") for c in r["quarter_check"])),
        "forecast_h1h2_checks": len(fs), "forecast_h1h2_consistent": sum(fs),
        "csv_checks": len(cm), "csv_matches_doc": sum(cm),
        "narrative_claims": claims, "verified": ver, "conflict_specific": cs, "near_miss_conflicts": nm, "unaligned": un,
        "reports_with_specific_conflict": sum(1 for r in rows if r["claims"]["conflict_specific"]),
        "unit_label_mismatch_claims": sum(r["claims"].get("unit_label_mismatch", 0) for r in rows),
        "reports_with_unit_label_mismatch": sum(1 for r in rows if r["claims"].get("unit_label_mismatch")),
        "verified_rate": round(ver / claims, 3) if claims else None, "near_miss_rate": round(nm / claims, 3) if claims else None,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = REPO / "runs/report_audit/steps"; out.mkdir(parents=True, exist_ok=True)
    f = out / f"case_reports_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
    f.write_text(json.dumps({"summary": summary, "reports": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    md = [f"# LLM 생성 기업 분석 보고서 501건 정합성 검사 ({time.strftime('%Y-%m-%d')})", "",
          f"입력: `{base}` (files/*.md 501건, Case_Summary_2026.csv). 검사기: `report_audit/check_case_reports.py`.", "",
          "| 검사 | 결과 |", "|---|---:|",
          f"| 재무 표 파싱 (항목 3개 이상) | {summary['table_parsed']}/{n} (단위 미기재로 추정 {summary['units'].get('(추정)', 0)}) |",
          f"| 분기 영업이익 합 = 연간 (표) | {summary['quarter_sum_consistent']}/{summary['quarter_sum_checks']} 일치, 불일치 보고서 {summary['reports_with_quarter_inconsistency']} (그중 10ⁿ배 단위 오류 {summary['reports_with_unit_error']}) |",
          f"| 6절 상반기+하반기 = 연간 전망 | {summary['forecast_h1h2_consistent']}/{summary['forecast_h1h2_checks']} |",
          f"| 요약 CSV '중간' = 문서 연간 전망 | {summary['csv_matches_doc']}/{summary['csv_checks']} |",
          f"| 코멘트 숫자 주장 (1·5·6절) | {claims:,}개: 검증 {ver:,} ({summary['verified_rate']:.1%}, 절삭 표기 {summary['verified_by_truncation']}), 지표·연도 명시 불일치 {cs} (보고서 {summary['reports_with_specific_conflict']}), 단위 라벨 불일치 {summary['unit_label_mismatch_claims']} (보고서 {summary['reports_with_unit_label_mismatch']}), 근접 불일치 {nm}, 미정렬 {un:,} |",
          "", "## 발견된 오류 유형", "",
          "- **표 단위 오류**: 표 머리에 '억 원' 이라 쓰고 값은 백만원·원 단위. 분기 합과 10ⁿ배 차이로 드러남 (예: DL 영업이익 19,316 vs 분기 합 1,931.5).",
          "- **표 ↔ 코멘트 단위 불일치**: 코멘트의 억원 값이 표 값의 10ⁿ배 (예: BNK금융지주 8,759억 원 vs 표 875,920,000,000).",
          "- **분기 합 ≠ 연간 (데이터 불일치)**: 단위 문제가 아닌데 분기 합과 표의 연간이 다름 (예: AJ네트웍스 2021 480.7 vs 451.2).",
          "- **전망 산술 오류**: 상반기+하반기 ≠ 연간 (8건).",
          "- **요약 CSV ↔ 문서 불일치**: 5건 (예: 디아이씨 230 vs 150, 흥국화재 550 vs 500).",
          "- **지표·연도 명시 주장 vs 표**: 표에서 계산한 이익률과 코멘트의 이익률이 다름 (예: HD현대마린엔진 2025 영업이익률 표 41.0% vs 코멘트 18.9%).",
          "", "## 검사 방식", "",
          "표를 사실 원천으로 삼고(XBRL 정렬과 같은 구조), 도출값(이익률·부채비율·YoY)을 계산한 뒤 코멘트 숫자를 값·표기 정밀도로 정렬한다. 절삭(내림) 표기는 검증으로 인정한다.",
          "지표와 회계연도가 함께 명시된 주장은 그 특정 값과 대조해 불일치를 판정하고, 컨센서스·전망·증감·분기 문맥은 특정 대조에서 제외한다.",
          "미정렬 숫자의 다수는 뉴스·증권사 전망·비재무 수치(임금 인상률 등)로 표에서 검증할 수 없는 값이다."]
    (out / f"case_reports_summary_{time.strftime('%Y%m%d')}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n분기 합 불일치 예:", [(r["name"], c) for r in rows for c in r["quarter_check"] if not c["ok"]][:5])
    print("\nCSV 불일치 예:", [(r["name"], r["csv_vs_doc"]) for r in rows if r.get("csv_match") is False][:6])
    print("\n특정 불일치(지표·연도가 명시된 주장이 표와 어긋남) 예:")
    shown = 0
    for r in rows:
        for e in r["claims"]["specific_examples"]:
            print("  ", r["name"], "|", e["claim"], "| 표:", e["target"], e["value"], "|", e["context"][:90]); shown += 1
            if shown >= 12: break
        if shown >= 12: break
    print("\n근접 불일치(참고) 예:")
    shown = 0
    for r in rows:
        for e in r["claims"]["examples"]:
            print("  ", r["name"], "|", e["claim"], "| 가장 가까운:", e["nearest"], e["value"], "|", e["context"][:90])
            shown += 1
            if shown >= 10:
                break
        if shown >= 10:
            break


if __name__ == "__main__":
    main()
