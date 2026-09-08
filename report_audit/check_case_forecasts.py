#!/usr/bin/env python3
"""LLM 생성 기업 분석 보고서의 재무 추정(6절 2026 영업이익 전망) 검증 4층.

1. 산술(부호 고려): 상반기+하반기=연간, 0·누락 전망, 1분기 실적>0 인데 상반기 전망<1분기(2분기 적자 함의), 6절 인용 1분기 값 vs 분기 데이터.
2. 타당성 플래그: 2025 실적(DART) 대비 성장률 극단(>+100%, <-50%), 최신 컨센서스 대비 괴리 >20%.
3. 근거 연결: 6절 '이유' 의 억원 수치가 5절 컨센서스 표·3절 뉴스·분기 데이터·재무 표 어디에 있는가.
4. 실현 정확도(DART): 2026 반기보고서 연결 영업이익(누적) vs 상반기 전망; 2025 사업보고서 영업이익 vs 표·분기 합 (표 접지).
사용: python3 -m report_audit.check_case_forecasts
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.check_case_reports import parse_consensus, parse_forecast, parse_quarters, parse_table  # noqa: E402
from report_audit.numfmt import find_numbers  # noqa: E402

BASE = Path(os.environ.get("CASE_REPORTS_DIR", REPO / "data/case_reports")).parent
CACHE = REPO / "data/dart/fin"; CACHE.mkdir(parents=True, exist_ok=True)
KEY = os.environ.get("DART_API_KEY") or None
if not KEY and (REPO / ".env").exists():
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DART_API_KEY="):
            KEY = line.split("=", 1)[1].strip().strip('"') or None
ALIAS = {"KT&G": "케이티앤지", "NAVER": "네이버", "삼성SDI": "삼성에스디아이", "SK하이닉스": "에스케이하이닉스", "SK이노베이션": "에스케이이노베이션",
         "SK텔레콤": "에스케이텔레콤", "KT": "케이티", "SK": "에스케이", "IPARK현대산업개발": "HDC현대산업개발", "LG": "LG", "GS": "GS",
         "CJ CGV": "CJ CGV", "HMM": "HMM", "S-Oil": "S-Oil", "SK스퀘어": "에스케이스퀘어", "SKC": "SKC", "DB손해보험": "DB손해보험",
         "KB금융": "KB금융", "SK바이오팜": "에스케이바이오팜", "SK가스": "에스케이가스", "SK네트웍스": "에스케이네트웍스", "SK케미칼": "에스케이케미칼",
         "SK아이이테크놀로지": "에스케이아이이테크놀로지", "SK오션플랜트": "에스케이오션플랜트", "SK디스커버리": "에스케이디스커버리", "SK리츠": "에스케이리츠",
         "KT스카이라이프": "케이티스카이라이프", "KTis": "케이티아이에스", "KTcs": "케이티씨에스",
         # 501 사례 보고서에서 미매핑이던 약칭 → DART 정식명
         "CR홀딩스": "CR홀딩스", "DI동일": "DI동일", "F&F": "F&F", "F&F홀딩스": "F&F 홀딩스", "HD현대미포": "HD현대미포", "HD현대인프라코어": "HD현대인프라코어",
         "KCC": "KCC", "KCC글라스": "KCC글라스", "LS ELECTRIC": "LS ELECTRIC", "SPC삼립": "SPC삼립", "TKG휴켐스": "TKG휴켐스", "동양생명": "동양생명보험",
         "롯데칠성": "롯데칠성음료", "미창석유": "미창석유공업", "삼성E&A": "삼성E&A", "삼성화재": "삼성화재해상보험", "삼영전자": "삼영전자공업",
         "삼화콘덴서": "삼화콘덴서공업", "서울가스": "서울도시가스", "스카이라이프": "케이티스카이라이프", "신세계 I&C": "신세계I&C", "엘브이엠씨홀딩스": "엘브이엠씨홀딩스",
         "유나이티드제약": "한국유나이티드제약", "유진투자증권": "유진증권", "코오롱ENP": "코오롱ENP", "한국단자": "한국단자공업", "한국전력": "한국전력공사",
         "현대차": "현대자동차", "화승인더": "화승인더스트리",
         # 사명 변경 (DART 목록은 구 사명/한글 표기)
         "LS ELECTRIC": "엘에스일렉트릭", "HD현대미포": "에이치디현대미포", "HD현대인프라코어": "에이치디현대인프라코어", "코오롱ENP": "코오롱이앤피",
         "유나이티드제약": "유나이티드", "DI동일": "디아이동일", "SPC삼립": "삼립", "KCC": "케이씨씨", "KCC글라스": "케이씨씨글라스",
         "TKG휴켐스": "티케이지휴켐스", "엘브이엠씨홀딩스": "엘브이엠씨"}


def corp_map() -> dict[str, tuple[str, str]]:
    codes = REPO / "data/dart/corp_codes_listed.json"
    rows = json.load(open(codes, encoding="utf-8")) if codes.exists() else []
    by = {}
    import html
    for code, name, stock in rows:
        name = html.unescape(name)
        by.setdefault(name, (code, stock)); by.setdefault(name.replace(" ", "").lower(), (code, stock))
    return by


def resolve(name: str, by: dict) -> tuple[str, str] | None:
    cands = [name, ALIAS.get(name, name), name.replace(" ", ""), name.replace(" ", "").lower(), name.replace("&", "앤")]
    for c in cands:
        if c in by:
            return by[c]
    # 영문 접두 치환
    n2 = re.sub(r"^SK", "에스케이", name); n2 = re.sub(r"^KT", "케이티", n2)
    return by.get(n2) or by.get(n2.replace(" ", "").lower())


def dart_fin(corp_code: str, year: int, reprt: str) -> dict | None:
    f = CACHE / f"{corp_code}_{year}_{reprt}.json"
    if f.exists():
        return json.load(open(f))
    url = f"https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?{urllib.parse.urlencode({'crtfc_key': KEY, 'corp_code': corp_code, 'bsns_year': year, 'reprt_code': reprt})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
    except Exception as ex:  # noqa: BLE001
        d = {"status": "ERR", "message": str(ex)}
    time.sleep(0.06)
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def op_income(d: dict | None, cumulative: bool = False) -> float | None:
    """연결(CFS) 우선, 없으면 개별(OFS) 영업이익 (억원). 반기·분기 보고서는 cumulative=True 로 누적(thstrm_add_amount)을 읽는다.
    (반기보고서의 thstrm_amount 는 당분기 3개월 값이다.)"""
    if not d or d.get("status") != "000":
        return None
    lst = d.get("list", [])
    has_cfs = any(r.get("fs_div") == "CFS" for r in lst)
    # 연결(CFS)이 있으면 연결만 쓴다. 연결 없이 개별(OFS)만 있을 때만 개별. (지주사의 개별 영업이익은 연결과 자릿수가 다르다)
    for div in (("CFS",) if has_cfs else ("OFS",)):
        for r in lst:
            if r.get("fs_div") == div and r.get("account_nm") in ("영업이익", "영업이익(손실)", "영업손익"):
                key = "thstrm_add_amount" if cumulative and r.get("thstrm_add_amount") else "thstrm_amount"
                try:
                    return float(r[key].replace(",", "")) / 1e8
                except (ValueError, KeyError):
                    continue
    return None


def main() -> None:
    by = corp_map()
    rows = []
    unmatched = []
    t0 = time.time()
    files = sorted((BASE / "files").glob("*.md"))
    for i, f in enumerate(files):
        t = f.read_text(encoding="utf-8", errors="replace")
        fc = parse_forecast(t)
        if "FY" not in fc:
            continue
        facts, _ = parse_table(t); q = parse_quarters(t); cons = parse_consensus(t)
        rec = {"name": f.stem, "forecast": fc}
        q1 = q.get("2026/Q1")
        qs25 = [v for k, v in q.items() if k.startswith("2025/")]
        sum25 = sum(qs25) if len(qs25) == 4 else None
        # 1. 산술 (부호 고려)
        a = {}
        a["h1h2_fy_ok"] = abs(fc.get("H1", 0) + fc.get("H2", 0) - fc["FY"]) <= max(0.5, abs(fc["FY"]) * 0.001) if {"H1", "H2"} <= set(fc) else None
        a["zero_or_missing"] = (fc["FY"] == 0) or (fc.get("H1") == 0 and fc.get("H2") == 0)
        a["h1_below_positive_q1"] = bool(q1 is not None and q1 > 0 and "H1" in fc and fc["H1"] < q1 - 0.5)
        sec6 = (re.search(r"\n6\. .*", t, re.S) or [None, ""])[0] if re.search(r"\n6\. .*", t, re.S) else ""
        q1_claims = [float(m.group(1).replace(",", "")) for m in re.finditer(r"2026/?Q1\D{0,30}?(\d[\d,]*\.?\d*)\s?억", sec6)]
        a["q1_claims"] = len(q1_claims); a["q1_claims_ok"] = sum(1 for v in q1_claims if q1 is not None and abs(v - q1) <= max(1.0, abs(q1) * 0.01))
        rec["arith"] = a
        # 3. 근거 연결: 6절 이유의 억원 수치가 문서 내 어디에 있는가
        evidence_pool = []
        for v in cons:
            evidence_pool.append(("컨센서스", v))
        for k, v in q.items():
            evidence_pool.append((f"분기 {k}", v))
        for it, ys in facts.items():
            for y, v in ys.items():
                evidence_pool.append((f"표 {it} {y}", v))
        for k, v in fc.items():
            evidence_pool.append((f"전망 {k}", v))
        news = (re.search(r"\n3\. .*?(?=\n4\. )", t, re.S) or [None, ""])[0] if re.search(r"\n3\. .*?(?=\n4\. )", t, re.S) else ""
        news_nums = {round(pn.value, 1) for _, _, pn in find_numbers(news) if pn.kind == "krw"}
        reasons = sec6.split("이유:", 1)[1] if "이유:" in sec6 else sec6
        ev = {"claims": 0, "supported": 0, "in_news": 0, "unsupported": 0}
        for _, _, pn in find_numbers(reasons):
            if pn.kind != "krw" or pn.value == 0:
                continue
            ev["claims"] += 1
            v = pn.value
            if any(abs(pv - v) <= max(1.0, abs(pv) * 0.005) or 0 <= (pv - v) < 1.0 for _, pv in evidence_pool):
                ev["supported"] += 1
            elif any(abs(nv - v) <= 1.0 for nv in news_nums):
                ev["in_news"] += 1
            else:
                ev["unsupported"] += 1
        rec["evidence"] = ev
        # 4. DART
        r = resolve(f.stem, by)
        if r is None:
            unmatched.append(f.stem); rec["dart"] = None
        else:
            code, stock = r
            h1_raw = op_income(dart_fin(code, 2026, "11012"), cumulative=True)
            fy25_raw = op_income(dart_fin(code, 2025, "11011"))
            # DART 단일계정 API 금액은 항상 원 단위다 (S-Oil·코웨이·GKL 등으로 확인). 문서 값으로 배율을 추정하면 문서의
            # 1000배 단위 오류를 정상으로 흡수하므로 배율은 1 로 고정하고, 문서 2025 값과의 일치 여부만 신뢰도로 기록한다.
            ref = sum25 if sum25 else facts.get("영업이익", {}).get("2025") or facts.get("영업이익", {}).get("2024")
            scale, conf = 1.0, "low"
            if fy25_raw and ref:
                conf = "high" if abs(fy25_raw - ref) <= abs(ref) * 0.05 else "medium"
            h1_actual = h1_raw * scale if h1_raw is not None else None
            fy25_actual = fy25_raw * scale if fy25_raw is not None else None
            rec["dart"] = {"corp_code": code, "stock": stock, "h1_2026_actual": h1_actual, "fy_2025_actual": fy25_actual, "unit_scale": scale, "scale_confidence": conf}
            if h1_actual is not None and "H1" in fc:
                err = fc["H1"] - h1_actual
                rec["dart"]["h1_error"] = round(err, 1)
                rec["dart"]["h1_abs_pct_error"] = round(abs(err) / abs(h1_actual) * 100, 1) if h1_actual else None
                rec["dart"]["h1_sign_ok"] = (fc["H1"] >= 0) == (h1_actual >= 0)
                import math
                ratio = abs(fc["H1"] / h1_actual) if h1_actual and fc["H1"] else 0
                rec["dart"]["forecast_unit_error"] = bool(ratio > 0 and abs(math.log10(ratio) - round(math.log10(ratio))) < 0.05 and round(math.log10(ratio)) != 0 and abs(math.log10(ratio)) >= 1)
            if fy25_actual is not None:
                t25 = facts.get("영업이익", {}).get("2025")
                rec["dart"]["table_2025_match"] = (abs(t25 - fy25_actual) <= max(1.0, abs(fy25_actual) * 0.01)) if t25 is not None else None
                rec["dart"]["quarters_2025_match"] = (abs(sum25 - fy25_actual) <= max(1.0, abs(fy25_actual) * 0.01)) if sum25 is not None else None
                g = (fc["FY"] - fy25_actual) / abs(fy25_actual) if fy25_actual else None
                rec["dart"]["fy_growth_vs_2025_actual"] = round(g * 100, 1) if g is not None else None
        # 2. 타당성
        base25 = (rec.get("dart") or {}).get("fy_2025_actual") or sum25
        rec["plaus"] = {"growth_extreme": (abs(fc["FY"] - base25) / abs(base25) > 1.0 if base25 else None) if base25 else None,
                        "consensus_dev_pct": round((fc["FY"] - cons[-1]) / abs(cons[-1]) * 100, 1) if cons and cons[-1] else None}
        rows.append(rec)
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(files)} {time.time() - t0:.0f}s", flush=True)
    n = len(rows)
    withdart = [r for r in rows if r.get("dart") and r["dart"].get("h1_2026_actual") is not None and r["dart"].get("scale_confidence") != "low"]
    errs = [r["dart"]["h1_abs_pct_error"] for r in withdart if r["dart"].get("h1_abs_pct_error") is not None]
    summary = {
        "reports_with_forecast": n, "dart_matched": sum(1 for r in rows if r.get("dart")), "dart_unmatched": len(unmatched),
        "h1_2026_actual_available": len(withdart), "dart_scale_thousand_won": sum(1 for r in rows if (r.get("dart") or {}).get("unit_scale") == 1000.0),
        "dart_scale_low_confidence_excluded": sum(1 for r in rows if (r.get("dart") or {}).get("scale_confidence") == "low" and (r.get("dart") or {}).get("h1_2026_actual") is not None),
        "arith": {"h1h2_fy_ok": sum(1 for r in rows if r["arith"]["h1h2_fy_ok"]), "h1h2_fy_checked": sum(1 for r in rows if r["arith"]["h1h2_fy_ok"] is not None),
                  "zero_or_missing": sum(1 for r in rows if r["arith"]["zero_or_missing"]), "h1_below_positive_q1": sum(1 for r in rows if r["arith"]["h1_below_positive_q1"]),
                  "q1_claims": sum(r["arith"]["q1_claims"] for r in rows), "q1_claims_ok": sum(r["arith"]["q1_claims_ok"] for r in rows)},
        "plausibility": {"growth_extreme_gt100": sum(1 for r in rows if r["plaus"]["growth_extreme"]), "consensus_dev_gt20": sum(1 for r in rows if r["plaus"]["consensus_dev_pct"] is not None and abs(r["plaus"]["consensus_dev_pct"]) > 20)},
        "evidence": {k: sum(r["evidence"][k] for r in rows) for k in ("claims", "supported", "in_news", "unsupported")},
        "realized_h1": {"n": len(errs), "median_abs_pct_error": round(statistics.median(errs), 1) if errs else None,
                        "within_10pct": sum(1 for e in errs if e <= 10), "within_20pct": sum(1 for e in errs if e <= 20), "within_50pct": sum(1 for e in errs if e <= 50), "over_100pct": sum(1 for e in errs if e > 100),
                        "sign_ok": sum(1 for r in withdart if r["dart"].get("h1_sign_ok")),
                        "forecast_unit_error_suspected": sum(1 for r in withdart if r["dart"].get("forecast_unit_error"))},
        "table_grounding_2025": {"table_checked": sum(1 for r in rows if (r.get("dart") or {}).get("table_2025_match") is not None), "table_match": sum(1 for r in rows if (r.get("dart") or {}).get("table_2025_match")),
                                 "quarters_checked": sum(1 for r in rows if (r.get("dart") or {}).get("quarters_2025_match") is not None), "quarters_match": sum(1 for r in rows if (r.get("dart") or {}).get("quarters_2025_match"))},
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = REPO / "runs/report_audit/steps"; out.mkdir(parents=True, exist_ok=True)
    fpath = out / f"case_forecasts_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
    fpath.write_text(json.dumps({"summary": summary, "unmatched": unmatched, "reports": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    worst = sorted([r for r in withdart if r["dart"].get("h1_abs_pct_error") is not None], key=lambda r: -r["dart"]["h1_abs_pct_error"])[:6]
    print("worst H1 forecasts:", [(r["name"], r["forecast"].get("H1"), r["dart"]["h1_2026_actual"], r["dart"]["h1_abs_pct_error"]) for r in worst])
    print("unmatched sample:", unmatched[:15])


if __name__ == "__main__":
    main()
