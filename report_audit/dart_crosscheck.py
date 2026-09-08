#!/usr/bin/env python3
"""보고서의 데이터 층 전체를 DART 와 셀 단위로 교차 검증한다.

- 표: 항목(매출액·영업이익·당기순이익·자산총계·부채총계·자본총계) × 연도(2021~2025) ↔ DART 사업보고서(11011) 연결 주요계정.
  매출총이익·매출원가·판관비는 전체계정 API(fnlttSinglAcntAll, 연결) 로 대조한다.
- 분기 영업이익: 보고서 목록 ↔ DART 1분기(11013)·반기(11012 누적)·3분기(11014 누적)·사업보고서에서 도출한 분기값.
  Q1 = 1분기, Q2 = 반기누적 − Q1, Q3 = 3분기누적 − 반기누적, Q4 = 연간 − 3분기누적.
- 단위: 회사별로 원/천원 배율을 셀 다수결로 추정. 금융사는 매출액 대신 영업수익 계정을 허용.
사용: python3 -m report_audit.dart_crosscheck [--limit N]
"""
from __future__ import annotations

import os

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.check_case_forecasts import ALIAS, corp_map, dart_fin  # noqa: E402
from report_audit.check_case_reports import parse_quarters, parse_table  # noqa: E402

BASE = Path(os.environ.get("CASE_REPORTS_DIR", REPO / "data/case_reports"))
ACCOUNTS = {"매출액": ("매출액", "수익(매출액)", "영업수익", "매출", "매출 및 지분법손익"), "영업이익": ("영업이익", "영업이익(손실)", "영업손익"),
            "당기순이익": ("당기순이익", "당기순이익(손실)", "당기순손익", "연결당기순이익", "당기순이익(손실)(지배주주지분포함)"),
            "자산총계": ("자산총계",), "부채총계": ("부채총계",), "자본총계": ("자본총계",)}
# 단일계정 API 에 없는 항목은 전체계정 API(fnlttSinglAcntAll, 연결) 의 account_id 로 찾는다
FULL_ITEMS = {"매출총이익": ("ifrs-full_GrossProfit",), "매출원가": ("ifrs-full_CostOfSales",),
              "판매비와관리비": ("dart_TotalSellingGeneralAdministrativeExpenses", "ifrs-full_SellingGeneralAndAdministrativeExpense")}
FULL_NAMES = {"매출총이익": ("매출총이익", "매출총이익(손실)", "매출총손익"), "매출원가": ("매출원가",), "판매비와관리비": ("판매비와관리비", "판매비및관리비", "판매관리비")}


def get_full_amount(d: dict | None, item: str, which: str = "thstrm_amount") -> float | None:
    """전체계정 API 응답(연결)에서 항목 값(억). which 로 전기·전전기 값도 읽는다."""
    if not d or d.get("status") != "000":
        return None
    for r in d.get("list", []):
        if r.get("sj_div") not in ("IS", "CIS"):
            continue
        if r.get("account_id") in FULL_ITEMS[item] or r.get("account_nm", "").strip() in FULL_NAMES[item]:
            try:
                return float(r[which].replace(",", "")) / 1e8
            except (ValueError, KeyError, AttributeError):
                continue
    return None


def norm(s: str) -> str:
    return re.sub(r"\s|㈜|주식회사|\(주\)", "", s).lower().replace("&", "앤")


def resolve2(name: str, rows) -> tuple[str, str] | None:
    import html as _html
    name = re.sub(r"_case\d+$|_v\d+$", "", name)  # 다른 폴더의 같은 회사 문서를 구분하기 위한 파일명 접미 태그
    rows = [(c, _html.unescape(n), s) for c, n, s in rows]  # DART 목록은 & 를 &amp; 로 저장
    exact = {n: (c, s) for c, n, s in rows}
    if name in exact:
        return exact[name]
    if name in ALIAS and ALIAS[name] in exact:
        return exact[ALIAS[name]]
    nn = norm(name); nn2 = re.sub(r"^sk", "에스케이", nn); nn2 = re.sub(r"^kt", "케이티", nn2)
    byn = {}
    for c, n, s in rows:
        byn.setdefault(norm(n), (c, s))
    for k in (nn, nn2):
        if k in byn:
            return byn[k]
    # 접두 일치가 유일하면 채택 (예: 삼성화재 → 삼성화재해상보험, 롯데칠성 → 롯데칠성음료)
    cands = [(k, v) for k, v in byn.items() if k.startswith(nn) or k.startswith(nn2)]
    if len(cands) == 1:
        return cands[0][1]
    return None


def get_prior(d: dict | None, item: str, which: str) -> float | None:
    """사업보고서의 전기(frmtrm) / 전전기(bfefrmtrm) 값."""
    if not d or d.get("status") != "000":
        return None
    lst = d.get("list", [])
    div = "CFS" if any(r.get("fs_div") == "CFS" for r in lst) else "OFS"
    for r in lst:
        if r.get("fs_div") == div and r.get("account_nm") in ACCOUNTS[item]:
            try:
                return float(r[which].replace(",", "")) / 1e8
            except (ValueError, KeyError, AttributeError):
                continue
    return None


def response_scale(d: dict | None, doc_values: dict[str, float]) -> float:
    """응답 하나의 배율 추정: 문서에 있는 같은 항목 값과 자릿수(log10)가 가장 가까운 배율을 택한다."""
    best, best_err = 1.0, None
    for sc in (1.0, 1000.0, 0.001):
        errs = []
        for it, v in doc_values.items():
            dv = get_amounts(d, it, False)
            if dv is None or not dv or not v:
                continue
            errs.append(abs(math.log10(abs(dv * sc)) - math.log10(abs(v))))
        if errs:
            e = sum(errs) / len(errs)
            if best_err is None or e < best_err:
                best, best_err = sc, e
    return best


def dart_all(corp_code: str, year: int, reprt: str) -> dict | None:
    """전체 계정 API (연결). 지배주주 귀속 순이익 등 단일계정 API 에 없는 항목용."""
    import urllib.parse, urllib.request
    from report_audit.check_case_forecasts import CACHE, KEY
    f = CACHE / f"{corp_code}_{year}_{reprt}_all.json"
    if f.exists():
        return json.load(open(f))
    url = f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?{urllib.parse.urlencode({'crtfc_key': KEY, 'corp_code': corp_code, 'bsns_year': year, 'reprt_code': reprt, 'fs_div': 'CFS'})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
    except Exception as ex:  # noqa: BLE001
        d = {"status": "ERR", "message": str(ex)}
    time.sleep(0.06)
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def controlling_ni(d: dict | None) -> float | None:
    if not d or d.get("status") != "000":
        return None
    for r in d.get("list", []):
        nm = r.get("account_nm", ""); aid = r.get("account_id", "")
        if ("ProfitLossAttributableToOwnersOfParent" in aid) or ("지배기업" in nm and "순이익" in nm and "비지배" not in nm) or ("지배주주" in nm and "순이익" in nm):
            try:
                return float(r["thstrm_amount"].replace(",", "")) / 1e8
            except (ValueError, KeyError, AttributeError):
                continue
    return None


def ni_bases(d: dict | None, which: str = "thstrm_amount") -> dict[str, float]:
    """전체계정 응답(연결)에서 순이익 후보 기준들: total / controlling / continuing / comprehensive. which=frmtrm_amount 면 전기 값."""
    out: dict[str, float] = {}
    if not d or d.get("status") != "000":
        return out
    for r in d.get("list", []):
        if r.get("sj_div") not in ("IS", "CIS"):
            continue
        nm = r.get("account_nm", ""); aid = r.get("account_id", "")
        try:
            v = float(r[which].replace(",", "")) / 1e8
        except (ValueError, KeyError, AttributeError):
            continue
        if "ProfitLossAttributableToOwnersOfParent" in aid or ("지배기업" in nm and "순이익" in nm and "비지배" not in nm):
            out.setdefault("controlling", v)
        elif aid.endswith("_ProfitLoss") or nm in ("당기순이익", "당기순이익(손실)"):
            out.setdefault("total", v)
        elif "ProfitLossFromContinuingOperations" in aid and "Attributable" not in aid:
            out.setdefault("continuing", v)
        elif "ComprehensiveIncome" in aid and "Attributable" not in aid and "Other" not in aid:
            out.setdefault("comprehensive", v)
    return out


def ofs_ni(d: dict | None) -> float | None:
    """단일계정 응답의 별도(OFS) 당기순이익."""
    if not d or d.get("status") != "000":
        return None
    for r in d.get("list", []):
        if r.get("fs_div") == "OFS" and r.get("account_nm", "").startswith("당기순이익"):
            try:
                return float(r["thstrm_amount"].replace(",", "")) / 1e8
            except (ValueError, KeyError, AttributeError):
                continue
    return None


RAW_TO_BN = {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "천만원": 1e-1, "억원": 1.0, "십억원": 10.0, "조원": 1e4, "셀표기": 1.0}


def resolve_table_unit(facts: dict, dart_annual: dict, unit_label: str | None) -> dict:
    """표의 실제 단위를 DART 대조로 확정한다.
    facts 는 parse_table 이 표기(또는 자릿수 추정) 단위로 억원 환산한 값. 각 셀의 보고서/DART 비율이 10^k 이면 k 의 최빈값으로
    표 값의 실제 단위 = 표기 단위 / 10^k 를 구한다. 표기가 없던 표(추정)도 같은 방식으로 단위를 확정한다."""
    label = (unit_label or "").replace("(추정)", "").replace(" ", "") or None
    inferred = bool(unit_label and "(추정)" in unit_label)
    label_factor = RAW_TO_BN.get(label or "억원", 1.0)
    ks = Counter(); used = 0
    for it, ys in facts.items():
        for y, v in ys.items():
            dv = dart_annual.get(it, {}).get(y)
            if v is None or dv is None or not v or not dv or (isinstance(v, float) and math.isnan(v)) or (v > 0) != (dv > 0):
                continue
            lr = math.log10(abs(v / dv))
            if abs(lr - round(lr)) < 0.01:
                ks[int(round(lr))] += 1; used += 1
    if not used:
        return {"unit_label": label, "unit_label_inferred": inferred, "unit_actual": None, "unit_k": None, "unit_cells_used": 0, "unit_status": "DART 대조 불가"}
    k, n = ks.most_common(1)[0]
    actual_factor = label_factor / (10 ** k)  # facts 는 표기(또는 자릿수 추정) 단위로 환산되어 있으므로 그 배율에서 10^k 를 되돌린다
    actual = min(RAW_TO_BN, key=lambda u: abs(math.log10(RAW_TO_BN[u]) - math.log10(actual_factor)))
    if inferred:
        status = f"단위 미기재 → DART 대조로 '{actual}' 확정" + ("" if actual == label else f" (자릿수 추정 '{label}' 와 다름)")
    elif k == 0:
        status = "셀마다 붙은 단위(조·억 원) 확인" if label == "셀표기" else f"표기 '{label}' 확인"
    else:
        status = f"표기 '{label}' 이지만 실제 단위는 '{actual}' (값이 DART 의 10^{k}배)"
    return {"unit_label": label, "unit_label_inferred": inferred, "unit_actual": actual, "unit_k": k, "unit_cells_used": n, "unit_status": status}


def is_pow10_ratio(v: float, ref: float | None) -> bool:
    """단위 오류 판정: 부호가 같고, 크기 비율이 10ⁿ (n≠0) 에서 2% 이내. 부호가 다르면 크기가 10ⁿ배라도 단위 오류가 아니다."""
    if not ref or not v or math.isnan(v) or math.isnan(ref) or (v > 0) != (ref > 0):
        return False
    ratio = abs(v / ref)
    lr = math.log10(ratio)
    return abs(lr - round(lr)) < 0.01 and round(lr) != 0


def get_amounts(d: dict | None, item: str, cumulative: bool) -> float | None:
    if not d or d.get("status") != "000":
        return None
    lst = d.get("list", [])
    has_cfs = any(r.get("fs_div") == "CFS" for r in lst)
    div = "CFS" if has_cfs else "OFS"
    for r in lst:
        if r.get("fs_div") != div or r.get("account_nm") not in ACCOUNTS[item]:
            continue
        key = "thstrm_add_amount" if cumulative and r.get("thstrm_add_amount") else "thstrm_amount"
        try:
            return float(r[key].replace(",", "")) / 1e8
        except (ValueError, KeyError, AttributeError):
            continue
    return None


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--base", default=None, help="보고서 폴더 (기본 CASE_REPORTS_DIR)"); ap.add_argument("--only", default=None, help="쉼표로 구분한 문서 이름")
    ap.add_argument("--out-tag", default="", help="출력 파일 이름 태그 (다른 폴더를 돌릴 때 기본 501건 결과와 섞이지 않게)")
    args = ap.parse_args()
    global BASE
    if args.base:
        BASE = Path(args.base)
    rows_codes = json.load(open(REPO / "data/dart/corp_codes_listed.json", encoding="utf-8")) if (REPO / "data/dart/corp_codes_listed.json").exists() else []
    files = sorted(BASE.glob("*.md"))[: args.limit] if args.limit else sorted(BASE.glob("*.md"))
    if args.only:
        want = set(args.only.split(",")); files = [f for f in files if f.stem in want]
    per = []; unmatched = []
    cell_stats = defaultdict(Counter); q_stats = Counter(); t0 = time.time()
    mism_examples = []; q_mism = []
    for i, f in enumerate(files):
        t = f.read_text(encoding="utf-8", errors="replace")
        facts, unit = parse_table(t); quarters = parse_quarters(t)
        r = resolve2(f.stem, rows_codes)
        if r is None:
            unmatched.append(f.stem); continue
        code, stock = r
        years = sorted({y for ys in facts.values() for y in ys} | {q.split("/")[0] for q in quarters})
        years = [y for y in years if 2021 <= int(y) <= 2026]
        annual = {y: dart_fin(code, int(y), "11011") for y in years if int(y) <= 2025}
        # DART 금액은 보고서 종류와 무관하게 원 단위(currency KRW)다. 문서 값으로 배율을 추정하면 문서의 단위 오류를
        # 정상으로 흡수해 버리므로(GKL·S-Oil·코웨이 등 9개 문서의 연간 표가 1000배 오류였음) 배율 추정을 하지 않는다.
        ann_scale = {y: 1.0 for y in annual}
        dart_annual = {it: {y: get_amounts(annual[y], it, False) for y in annual} for it in ACCOUNTS}
        # 매출총이익·매출원가·판관비: 문서 표에 있으면 전체계정 API 로 가져온다 (연도별 1회, 캐시)
        full_needed = [it for it in FULL_ITEMS if it in facts]
        full = {y: dart_all(code, int(y), "11011") for y in annual} if full_needed else {}
        for it in full_needed:
            dart_annual[it] = {y: get_full_amount(full[y], it) for y in annual}
        # 재작성 값: y 의 값이 (y+1) 보고서 전기·(y+2) 보고서 전전기에 어떻게 기재되었는가
        restated = {it: {} for it in dart_annual}
        for it in dart_annual:
            for y in annual:
                alts = []
                ny = str(int(y) + 1); ny2 = str(int(y) + 2)
                if it in FULL_ITEMS:
                    if ny in full:
                        alts.append(get_full_amount(full[ny], it, "frmtrm_amount"))
                    if ny2 in full:
                        alts.append(get_full_amount(full[ny2], it, "bfefrmtrm_amount"))
                else:
                    if ny in annual:
                        alts.append(get_prior(annual[ny], it, "frmtrm_amount"))
                    if ny2 in annual:
                        alts.append(get_prior(annual[ny2], it, "bfefrmtrm_amount"))
                restated[it][y] = [a for a in alts if a is not None]
        scale = 1.0  # 응답별 배율은 이미 dart_annual 에 반영됨
        rec = {"name": f.stem, "corp_code": code, "unit": unit, "scale": 1.0, "cells": {}, "quarters": {}, **resolve_table_unit(facts, dart_annual, unit)}
        for it, ys in facts.items():
            for y, v in ys.items():
                if it not in dart_annual or y not in dart_annual[it]:
                    cell_stats[it]["no_dart_year"] += 1; continue
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    cell_stats[it]["doc_missing"] += 1; continue  # 문서가 N/A·NaN 으로 둔 셀
                dv = dart_annual[it][y]
                if dv is None:
                    cell_stats[it]["no_dart_account"] += 1; rec["cells"][f"{it}@{y}"] = "no_account"; continue
                dvs = dv * scale
                if abs(dvs - v) <= max(1.0, abs(dvs) * 0.01):
                    cell_stats[it]["match"] += 1; rec["cells"][f"{it}@{y}"] = "match"
                elif any(abs(a * scale - v) <= max(1.0, abs(a * scale) * 0.01) for a in restated[it].get(y, [])):
                    cell_stats[it]["match_restated"] += 1; rec["cells"][f"{it}@{y}"] = "match_restated"
                else:
                    tol = lambda ref: max(1.0, abs(ref) * 0.01)  # noqa: E731
                    refs_for_unit = [dvs] + list(restated[it].get(y, []))
                    if it == "당기순이익":
                        al = dart_all(code, int(y), "11011"); bases = ni_bases(al)
                        prior = ni_bases(dart_all(code, int(y) + 1, "11011"), "frmtrm_amount") if int(y) + 1 <= 2025 else {}
                        cni = bases.get("controlling"); cni_prev = prior.get("controlling")
                        if (cni is not None and abs(cni - v) <= tol(cni)) or (cni_prev is not None and abs(cni_prev - v) <= tol(cni_prev)):
                            cell_stats[it]["match_controlling"] += 1; rec["cells"][f"{it}@{y}"] = "match_controlling"; continue
                        others = {k: bases[k] for k in ("continuing", "comprehensive") if k in bases}
                        o = ofs_ni(annual[y])
                        if o is not None:
                            others["ofs"] = o
                        hit = next((k for k, x in others.items() if abs(x - v) <= tol(x)), None)
                        if hit:
                            cell_stats[it]["match_other_basis"] += 1; rec["cells"][f"{it}@{y}"] = f"match_other_basis:{hit}"; continue
                        refs_for_unit += [x for x in (cni, cni_prev) if x is not None]
                    kind = "unit_error" if any(is_pow10_ratio(v, ref) for ref in refs_for_unit) else "mismatch"
                    cell_stats[it][kind] += 1; rec["cells"][f"{it}@{y}"] = kind
                    if kind == "mismatch" and len(mism_examples) < 12:
                        mism_examples.append((f.stem, it, y, round(v, 1), round(dvs, 1)))
        # 분기 영업이익
        by_year = defaultdict(dict)
        for q, v in quarters.items():
            y, qq = q.split("/"); by_year[y][qq] = v
        for y, qs in by_year.items():
            if not (2021 <= int(y) <= 2026):
                continue
            q1 = get_amounts(dart_fin(code, int(y), "11013"), "영업이익", False)
            h1 = get_amounts(dart_fin(code, int(y), "11012"), "영업이익", True)
            n9 = get_amounts(dart_fin(code, int(y), "11014"), "영업이익", True) if int(y) <= 2025 else None
            fy = dart_annual["영업이익"].get(y) if int(y) <= 2025 else None
            derived = {"Q1": q1, "Q2": (h1 - q1) if (h1 is not None and q1 is not None) else None,
                       "Q3": (n9 - h1) if (n9 is not None and h1 is not None) else None, "Q4": (fy - n9) if (fy is not None and n9 is not None) else None}
            for qq, v in qs.items():
                dv = derived.get(qq)
                if dv is None:
                    q_stats["no_dart"] += 1; continue
                dvs = dv
                if abs(dvs - v) <= max(1.0, abs(dvs) * 0.02):
                    q_stats["match"] += 1; rec["quarters"][f"{y}/{qq}"] = "match"
                else:
                    if is_pow10_ratio(v, dvs):
                        q_stats["unit_error"] += 1; rec["quarters"][f"{y}/{qq}"] = "unit_error"; continue
                    q_stats["mismatch"] += 1; rec["quarters"][f"{y}/{qq}"] = "mismatch"
                    if len(q_mism) < 10:
                        q_mism.append((f.stem, f"{y}/{qq}", round(v, 1), round(dvs, 1)))
        per.append(rec)
        if (i + 1) % 25 == 0:
            print(f"{i + 1}/{len(files)} {time.time() - t0:.0f}s", flush=True)
    unit_stats = Counter()
    for r_ in per:
        if r_.get("unit_actual") is None:
            unit_stats["대조 불가"] += 1
        elif r_.get("unit_label_inferred"):
            unit_stats["미기재→DART 확정"] += 1
        elif r_.get("unit_k") == 0:
            unit_stats["표기 일치"] += 1
        else:
            unit_stats["표기≠실제"] += 1
    summary = {"reports": len(files), "table_units": dict(unit_stats), "dart_matched": len(per), "unmatched": len(unmatched), "unmatched_names": unmatched[:40],
               "scale_thousand_won_companies": sum(1 for r in per if r["scale"] == 1000.0),
               "table_cells_by_item": {it: dict(c) for it, c in cell_stats.items()},
               "table_cells_total": {k: sum(c[k] for c in cell_stats.values()) for k in ("match", "match_restated", "match_controlling", "match_other_basis", "mismatch", "unit_error", "no_dart_account", "no_dart_year", "dart_not_provided", "doc_missing")},
               "quarter_cells": dict(q_stats), "elapsed_sec": round(time.time() - t0, 1)}
    out = REPO / "runs/report_audit/steps"; out.mkdir(parents=True, exist_ok=True)
    fpath = out / f"dart_crosscheck_{(args.out_tag + '_') if args.out_tag else ''}{time.strftime('%Y%m%d_%H%M%S')}.json"
    fpath.write_text(json.dumps({"summary": summary, "reports": per, "mismatch_examples": mism_examples, "quarter_mismatch_examples": q_mism}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("table mismatch examples:", mism_examples[:8])
    print("quarter mismatch examples:", q_mism[:6])


if __name__ == "__main__":
    main()
