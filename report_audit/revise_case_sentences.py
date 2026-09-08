#!/usr/bin/env python3
"""확연히 잘못된 문장의 수정 제안 (교정 단계).

입력: check_case_llm_chunked 결과(문장별 충돌·원장). 두 층으로 고친다.
- 자료 층(표·분기 셀의 10ⁿ배 단위 오류·불일치): LLM 없이 결정적으로 정정값을 만든다 — 단위 오류는 DART 값, 불일치는 DART 값(연결)을 제시.
- 서술 층(문장 충돌): LLM 에게 원장과 충돌 정보를 주고 "최소 수정" 문장을 받는다. 숫자를 바꿀 때는 원장 id 를 근거로 대고,
  시간 역행·법인 혼동처럼 값이 아니라 주장이 틀린 문장은 잘못된 원인·주체를 빼거나 한정하는 최소 수정을 제안한다.
  수정 문장의 숫자는 결정적으로 재검사한다: 원문에 있던 숫자가 아니면 원장 항목의 값과 일치해야 한다(억·조·% 정규화). 아니면 '보류'.

사용: python3 -m report_audit.revise_case_sentences --names DL,KT&G [--llm-base-url ...] [--llm-model ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.check_case_llm_chunked import chat_json  # noqa: E402  (재시도 포함)
from report_audit.llm import last_usage  # noqa: E402

OUT = REPO / "runs/report_audit/steps"; OUT.mkdir(parents=True, exist_ok=True)
NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*(조|억|만|%|원)?")


def load_records(names: set[str]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    files = sorted(glob.glob(str(OUT / "case_llm_chunked_batch*.json"))) + sorted(glob.glob(str(OUT / "case_llm_chunked_extra*.json")))
    for f in files:
        for r in json.load(open(f, encoding="utf-8"))["reports"]:
            if r["name"] in names and r.get("chunks"):
                by[r["name"]] = r
    # 결론 감사 결과 병합 (감사 전용 파일이 나중이면 우선)
    for f in sorted(glob.glob(str(OUT / "case_llm_chunked_audit*.json"))):
        for r in json.load(open(f, encoding="utf-8"))["reports"]:
            if r["name"] in by and r.get("conclusion_audit") and not r["conclusion_audit"].get("error"):
                by[r["name"]]["conclusion_audit"] = r["conclusion_audit"]
                have = {x["id"] for x in by[r["name"]]["ledger"]}
                by[r["name"]]["ledger"] += [x for x in r["ledger"] if x["id"] not in have]
    return by


def ledger_numbers(ledger: list[dict]) -> list[float]:
    """원장 항목에 나오는 억 단위 값들(억·조·% 를 억/비율로 정규화) — 수정 문장의 숫자 접지 검사용."""
    vals = []
    for f in ledger:
        for m in re.finditer(r"(-?\d[\d,]*(?:\.\d+)?)\s*(조|억|%|원)?", f["text"]):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            u = m.group(2)
            if u == "조":
                vals.append(v * 1e4)
            elif u == "억":
                vals.append(v)
            elif u == "%":
                vals.append(v)  # 퍼센트는 그대로
            elif u == "원":
                vals.append(v / 1e8); vals.append(v)  # 주가(원)는 그대로도 허용
            else:
                vals.append(v)
    return vals


def derived_pool(ledger: list[dict]) -> list[float]:
    """원장 값으로 결정적으로 계산되는 값들: 연도별 이익률, 항목별 전년 대비 증감률(%), 분기 합(반기·연간)과 그 차, 분기 간 증감률."""
    annual: dict[str, dict[str, float]] = {}
    quarters: dict[str, float] = {}
    for f in ledger:
        t = f["text"]
        m = re.match(r"(?:DART|보고서 표) (\d{4}) (?:사업보고서\S*\s*)?(매출액|영업이익|당기순이익|매출총이익|매출원가|판매비와관리비|자산총계|부채총계|자본총계)\s*=?\s*(-?[\d,]+(?:\.\d+)?)억", t)
        if m:
            annual.setdefault(m.group(1), {})[m.group(2)] = float(m.group(3).replace(",", "")); continue
        m = re.search(r"(\d{4}/Q[1-4]) (?:영업이익 )?=?\s*(-?[\d,]+(?:\.\d+)?)억", t)
        if m:
            quarters.setdefault(m.group(1), float(m.group(2).replace(",", "")))
    out: list[float] = []
    years = sorted(annual)
    for y in years:
        a = annual[y]
        for num, den in (("영업이익", "매출액"), ("당기순이익", "매출액"), ("매출총이익", "매출액")):
            if a.get(num) is not None and a.get(den):
                out.append(a[num] / a[den] * 100)
        for it, v in a.items():
            py = str(int(y) - 1)
            if py in annual and annual[py].get(it):
                out.append((v / annual[py][it] - 1) * 100)
    byy: dict[str, dict[str, float]] = {}
    for q, v in quarters.items():
        y, qq = q.split("/"); byy.setdefault(y, {})[qq] = v
    for y, qs in byy.items():
        vals = [qs.get(f"Q{i}") for i in range(1, 5)]
        if all(v is not None for v in vals[:2]):
            out.append(vals[0] + vals[1])  # 상반기
        if all(v is not None for v in vals[2:]):
            out.append(vals[2] + vals[3])  # 하반기
        if all(v is not None for v in vals):
            out.append(sum(vals))
        if all(v is not None for v in vals) and y in annual and annual[y].get("영업이익") is not None:
            out.append(annual[y]["영업이익"] - vals[0] - vals[1])  # 연간 − 상반기
        py = str(int(y) - 1)
        for qq, v in qs.items():
            pv = byy.get(py, {}).get(qq)
            if pv:
                out.append((v / pv - 1) * 100)
            pq = f"Q{int(qq[1]) - 1}" if qq != "Q1" else None
            if pq and qs.get(pq):
                out.append((v / qs[pq] - 1) * 100)
    return out


def number_grounded(tok: str, unit: str | None, pool: list[float]) -> bool:
    try:
        v = float(tok.replace(",", ""))
    except ValueError:
        return True
    cands = [v]
    if unit == "조":
        cands = [v * 1e4]
    elif unit == "만":
        cands = [v / 1e4, v]
    for c in cands:
        tol = max(0.15, abs(c) * 0.011) if unit == "%" else max(0.5, abs(c) * 0.011)  # 표시 정밀도 + 1%
        if any(abs(c - p) <= tol for p in pool):
            return True
    return False


def check_revision(original: str, revised: str, ledger: list[dict]) -> tuple[bool, list[str]]:
    """수정 문장의 숫자 중 원문에 없던 것은 모두 원장에 있어야 한다."""
    pool = ledger_numbers(ledger) + derived_pool(ledger)
    orig_nums = {m.group(0).replace(" ", "") for m in NUM_RE.finditer(original)}
    bad = []
    for m in NUM_RE.finditer(revised):
        tok = m.group(0).replace(" ", "")
        if tok in orig_nums:
            continue
        num = re.match(r"-?\d[\d,]*(?:\.\d+)?", tok).group(0); unit = tok[len(num):] or None
        if re.fullmatch(r"20\d\d|[1-9]|1[0-2]|Q[1-4]", num) and unit is None:
            continue  # 연도·분기·월 번호
        if not number_grounded(num, unit, pool):
            bad.append(tok)
    return (not bad), bad


def deterministic_data_fixes(rec: dict) -> list[dict]:
    """표·분기 셀의 단위 오류·불일치 → 정정값(DART). 원장 T#/Q# 문구에서 값을 읽는다."""
    fixes = []
    ledger = {f["id"]: f for f in rec["ledger"]}
    for c in rec["conflicts"]:
        if c.get("layer") != "data":
            continue
        f = ledger.get(c["ref"], {}); t = f.get("text", "")
        m = re.search(r"= (-?[\d,]+(?:\.\d+)?)억", t)
        if not m:
            continue
        v = float(m.group(1).replace(",", ""))
        # DART 값은 같은 항목·연도의 D# 에서 찾는다
        item_year = re.match(r"보고서 (?:표|분기 영업이익) (.+?) (\d{4}(?:/Q[1-4])?) =", t)
        dart_val = None
        if item_year:
            it, y = item_year.group(1), item_year.group(2)
            for g in rec["ledger"]:
                if g["id"].startswith("D") and y in g["text"] and (it in g["text"] or ("분기" in t and "도출" in g["text"])):
                    mm = re.search(r"(-?[\d,]+(?:\.\d+)?)억", g["text"])
                    if mm:
                        dart_val = float(mm.group(1).replace(",", "")); break
        if dart_val is None:
            continue
        kind = c["type"]
        k = round(math.log10(abs(v / dart_val))) if dart_val and v else 0
        fixes.append({"ref": c["ref"], "type": kind, "cell": t.split(" (")[0].replace("보고서 ", ""), "reported": v, "corrected": dart_val,
                      "why": (f"보고서 값이 DART 의 10^{k}배 — 단위 오류, DART 값으로 정정" if kind == "unit" else "DART 연결 공시 값으로 정정")})
    return fixes


def revise_document(rec: dict, base_url: str, model: str) -> dict:
    ledger = rec["ledger"]
    narr = [c for c in rec["conflicts"] if c.get("layer") == "narrative"]
    # 같은 문장의 충돌을 묶는다
    by_s: dict[str, dict] = {}
    for c in narr:
        e = by_s.setdefault(c["s"], {"s": c["s"], "sentence": c["sentence"], "conflicts": []})
        e["conflicts"].append({"ref": c["ref"], "type": c["type"], "why": c["why"]})
    out = {"name": rec["name"], "data_fixes": deterministic_data_fixes(rec), "revisions": [], "error": None, "seconds": 0.0}
    if not by_s:
        return out
    lid = {f["id"]: f for f in ledger}
    items = []
    for e in by_s.values():
        refs = sorted({c["ref"] for c in e["conflicts"]})
        items.append({"s": e["s"], "sentence": e["sentence"], "conflicts": e["conflicts"], "ledger": [f"{r} {lid[r]['text']}" for r in refs if r in lid]})
    task = (
        f"보고서 대상 회사: {rec['name']}. 아래 문장들은 원장(사실 자료)과 충돌한다고 판정된 문장이다. 각 문장을 원장에 맞게 '최소한으로' 고쳐라.\n"
        "규칙: (1) 틀린 숫자는 충돌 근거 원장 항목의 값으로 바꾸고 단위(억·조·%)를 문장의 표기에 맞춘다. (2) 시간 역행(원인의 보도일이 결과 기간보다 뒤)은 그 원인 구절을 빼거나 "
        "'~이후 부각된 리스크' 처럼 시점을 바로잡아 인과를 끊는다. (3) 법인 혼동(연결 대상이 아닌 관계사)은 주체를 바로잡거나 '지분법 관계사인 ~의' 로 한정한다. "
        "(4) 분기 패턴 주장이 데이터와 다르면 데이터가 맞는 분기로 바꾸거나 해당 분기를 뺀다. (5) 문장 구조·어조는 유지하고, 원장에 없는 새 숫자는 넣지 않는다. "
        "(6) 고칠 수 없으면 revised 를 원문과 같게 두고 note 에 이유를 쓴다.\n"
        '반드시 JSON 만 출력: {"revisions": [{"s": "S#", "revised": "수정 문장 전체", "basis": ["D55"], "change": "무엇을 무엇으로 바꿨는지 한 줄", "note": ""}]}\n\n'
        "[전체 원장]\n" + "\n".join(f"{f['id']} {f['text']}" for f in ledger) + "\n\n[충돌 문장]\n" + json.dumps(items, ensure_ascii=False, indent=1)
    )
    messages = [{"role": "system", "content": "재무 보고서 교정 도구. 원장만 근거로 삼고 최소 수정 원칙을 지키며 반드시 JSON 만 출력한다."}, {"role": "user", "content": task}]
    t0 = time.time()
    try:
        resp = chat_json(base_url, model, messages, max_tokens=4000)
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {ex}"; out["seconds"] = round(time.time() - t0, 1); return out
    valid = set(lid)
    for r in (resp.get("revisions") or []) if isinstance(resp, dict) else []:
        if not isinstance(r, dict) or r.get("s") not in by_s:
            continue
        original = by_s[r["s"]]["sentence"]; revised = str(r.get("revised", "")).strip()
        ok, bad = check_revision(original, revised, ledger)
        status = "unchanged" if revised == original.strip() or not revised else ("accepted" if ok else "held")
        out["revisions"].append({"s": r["s"], "original": original, "revised": revised, "basis": [b for b in (r.get("basis") or []) if b in valid],
                                 "change": str(r.get("change", ""))[:200], "note": str(r.get("note", ""))[:200], "status": status, "ungrounded_numbers": bad,
                                 "conflict_types": sorted({c["type"] for c in by_s[r["s"]]["conflicts"]})})
    out["seconds"] = round(time.time() - t0, 1); out["usage"] = last_usage()
    return out


def propagate(rec: dict, first: dict, base_url: str, model: str) -> list[dict]:
    """1차 수정(accepted)이 바꾼 값·원인·주체에 의존하는 다른 문장을 찾아 함께 고친다.
    후보: (a) 1차 수정에서 삭제된 토큰(숫자·고유명사)을 담은 문장, (b) 같은 충돌 원장 id 를 근거로 삼은 문장,
    (c) 수정된 문장에서 파생된 K# 주장을 근거로 삼은 문장. LLM 이 후보마다 '바뀌어야 하는가' 를 판정하고 최소 수정을 낸다."""
    accepted = [x for x in first["revisions"] if x["status"] == "accepted"]
    if not accepted:
        return []
    ledger = rec["ledger"]; lid = {f["id"]: f for f in ledger}
    sents = {s_["id"]: s_["text"] for ch in rec["chunks"] for s_ in ch["sentences"] if s_["id"]}
    supports = {a["s"]: set(a.get("supports", [])) for ch in rec["chunks"] for a in ch.get("annotations", [])}
    conf_refs = {c["s"]: {c["ref"]} for c in rec["conflicts"] if c.get("layer") == "narrative"}
    import difflib
    cands: dict[str, set[str]] = {}
    for x in accepted:
        a = re.findall(r"\S+", x["original"]); b = re.findall(r"\S+", x["revised"])
        removed = {t.strip("()[],.·") for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != "equal" for t in a[i1:i2]}
        removed = {t for t in removed if len(t) >= 2 and not re.fullmatch(r"[가-힣]{1,2}", t)}
        derived_k = {f["id"] for f in ledger if f["id"].startswith("K") and f"(출처 {x['s']})" in f["text"]}
        refs = conf_refs.get(x["s"], set())
        for sid, txt in sents.items():
            if sid == x["s"] or sid in {y["s"] for y in accepted}:
                continue
            why = set()
            if any(t in txt for t in removed):
                why.add("같은 표현·값 사용: " + ", ".join(sorted(t for t in removed if t in txt))[:60])
            if refs & supports.get(sid, set()):
                why.add("같은 원장 항목 " + ", ".join(sorted(refs & supports.get(sid, set()))) + " 를 근거로 삼음")
            if derived_k & supports.get(sid, set()):
                why.add("수정 문장에서 파생된 주장 " + ", ".join(sorted(derived_k & supports.get(sid, set()))) + " 을 근거로 삼음")
            if why:
                cands.setdefault(sid, set()).update({f"{x['s']}: {w}" for w in why})
    if not cands:
        return []
    items = [{"s": sid, "sentence": sents[sid], "depends_on": sorted(w)} for sid, w in sorted(cands.items(), key=lambda kv: int(kv[0][1:]))]
    task = (
        f"보고서 대상 회사: {rec['name']}. 아래 [1차 수정] 은 확정된 교정이다. [후보 문장] 은 1차 수정이 바꾼 값·원인·주체에 의존할 가능성이 있는 문장이다.\n"
        "후보마다 판정하라: 1차 수정과 일관되게 바뀌어야 하면 최소 수정 문장을, 그대로 두어도 일관되면 revised 를 원문과 같게 두고 note 에 이유를 쓴다. "
        "규칙은 1차 수정과 같다: 틀린 숫자는 원장 값으로, 끊어진 인과는 원인 구절을 빼거나 시점을 바로잡고, 원장에 없는 새 숫자는 넣지 않는다. "
        "특히 1차 수정으로 어떤 값·원인이 바뀌었으면 그 값을 되풀이하거나 그 원인을 전제로 삼은 문장, 그 값으로 계산한 증감률·비율 문장은 반드시 함께 고친다.\n"
        '반드시 JSON 만 출력: {"revisions": [{"s": "S#", "revised": "문장 전체", "basis": ["D55"], "change": "한 줄", "note": ""}]}\n\n'
        "[1차 수정]\n" + "\n".join(f"{x['s']}: {x['original']}\n  → {x['revised']}\n  ({x['change']})" for x in accepted)
        + "\n\n[원장]\n" + "\n".join(f"{f['id']} {f['text']}" for f in ledger) + "\n\n[후보 문장]\n" + json.dumps(items, ensure_ascii=False, indent=1)
    )
    messages = [{"role": "system", "content": "재무 보고서 교정 도구. 1차 수정과의 일관성을 최소 수정으로 맞추고 반드시 JSON 만 출력한다."}, {"role": "user", "content": task}]
    try:
        resp = chat_json(base_url, model, messages, max_tokens=4000)
    except Exception as ex:  # noqa: BLE001
        return [{"s": None, "status": "error", "note": f"{type(ex).__name__}: {ex}"}]
    out = []
    for r in (resp.get("revisions") or []) if isinstance(resp, dict) else []:
        if not isinstance(r, dict) or r.get("s") not in cands:
            continue
        original = sents[r["s"]]; revised = str(r.get("revised", "")).strip()
        if not revised or revised == original.strip():
            out.append({"s": r["s"], "original": original, "revised": original, "basis": [], "change": "", "note": str(r.get("note", ""))[:200], "status": "unchanged",
                        "ungrounded_numbers": [], "conflict_types": [], "propagated_from": sorted({w.split(":")[0] for w in cands[r["s"]]})}); continue
        ok, bad = check_revision(original, revised, ledger)
        out.append({"s": r["s"], "original": original, "revised": revised, "basis": [b for b in (r.get("basis") or []) if b in lid], "change": str(r.get("change", ""))[:200],
                    "note": str(r.get("note", ""))[:200], "status": "propagated" if ok else "held", "ungrounded_numbers": bad, "conflict_types": [],
                    "propagated_from": sorted({w.split(":")[0] for w in cands[r["s"]]}), "depends_on": sorted(cands[r["s"]])})
    return out


def _num(s_: str) -> float | None:
    try:
        return float(str(s_).replace(",", ""))
    except (TypeError, ValueError):
        return None


def unit_fix_candidate(ledger: list[dict]) -> dict | None:
    """전망이 10^k 배 단위 오류로 보이면 축소 후보를 결정적으로 만든다: H1/10^k 가 Q1×2 의 ±60%, FY/10^k 가 2025 연간의 ±60% 안에 들 때."""
    fc = {}; q1 = fy25 = None
    for f in ledger:
        t = f["text"]
        m = re.match(r"보고서 전망 2026 (상반기|하반기|연간) 영업이익 (-?[\d,]+(?:\.\d+)?)억", t)
        if m:
            fc[{"상반기": "H1", "하반기": "H2", "연간": "FY"}[m.group(1)]] = float(m.group(2).replace(",", ""))
        m = re.search(r"2026/Q1 (?:영업이익 )?=?\s*(-?[\d,]+(?:\.\d+)?)억", t)
        if m and q1 is None:
            q1 = float(m.group(1).replace(",", ""))
        m = re.match(r"DART 2025 사업보고서\(연결\) 영업이익 (-?[\d,]+(?:\.\d+)?)억", t)
        if m:
            fy25 = float(m.group(1).replace(",", ""))
    if "H1" not in fc or "FY" not in fc or not q1 or not fy25 or q1 <= 0 or fy25 <= 0:
        return None
    for k in (1, 2, 3):
        h1 = fc["H1"] / 10 ** k; fy = fc["FY"] / 10 ** k
        if 0.4 * 2 * q1 <= h1 <= 1.6 * 2 * q1 and 0.4 * fy25 <= fy <= 1.6 * fy25:
            return {"k": k, "H1": h1, "H2": fc.get("H2", fy - h1) / 10 ** k if "H2" in fc else fy - h1, "FY": fy,
                    "why": f"전망을 10^{k} 로 나누면 상반기 {h1:,.1f}억은 Q1 실적 {q1:,.1f}억의 2배 근처, 연간 {fy:,.1f}억은 2025 실적 {fy25:,.1f}억 근처가 된다 → 단위 오류로 추정"}
    return None


def revise_conclusion(rec: dict, base_url: str, model: str) -> dict | None:
    """결론 감사가 partially/unsupported 이면 전망 수치와 이유 문장의 수정안을 낸다. 검산 R#·전제 판정·단위 오류 후보를 근거로 삼는다."""
    a = rec.get("conclusion_audit") or {}
    if a.get("verdict") not in ("partially", "unsupported"):
        return None
    ledger = rec["ledger"]; lid = {f["id"]: f for f in ledger}
    last = rec["chunks"][-1]
    sents = {s_["id"]: s_["text"] for s_ in last["sentences"] if s_["id"]}
    prem = a.get("premises", [])
    unit = unit_fix_candidate(ledger)
    posthoc = lambda t: ("사후 실현" in t) or ("2026" in t and ("반기보고서" in t or re.search(r"2026/Q[234]", t) is not None))  # noqa: E731  작성일(2026-06-22) 이후 공개된 값은 수정 근거에서 뺀다
    task = (
        f"보고서 대상 회사: {rec['name']}. 결론(2026 영업이익 전망)의 감사 판정은 '{a.get('verdict')}' 이다: {a.get('summary','')} / 가장 큰 결함: {a.get('top_issue','')}\n"
        "아래 원장(D# DART, Q# 분기, F# 전망, C# 컨센서스, R# 검산)과 전제 판정을 근거로 결론을 어떻게 고쳐야 하는지 제안하라. "
        "보고서 작성 시점(2026-06-22)에 저자가 알 수 있었던 사실만 쓴다 — 2026 반기 실제 실적 같은 사후 실현값은 원장에서 제외했고, 써서는 안 된다.\n"
        "규칙: (1) 상반기 = 2026 Q1 실적 + Q2 추정. Q2 추정은 원장 값에서 명시적으로 유도한다(예: 'Q1 수준 유지' → Q2 = Q1, '2025 Q2 와 같은 계절성' → Q2 = Q1 × (2025Q2/2025Q1)). "
        "(2) 하반기는 원장의 계절성(2025 하반기/상반기 비율)이나 컨센서스에서 유도하고, 연간 = 상반기 + 하반기 를 반드시 맞춘다. "
        "(3) 결론을 뒷받침하지 않는 전제(no)는 삭제하거나 최소 수정하고, 전제와 결론의 방향이 어긋나면 결론 쪽을 고친다. "
        "(4) 단위 오류 후보가 있으면 그것을 우선 검토한다. (5) 원장에 없는 새 사실은 넣지 않고, 모든 수치에 유도식을 쓴다. "
        "(6) 결론이 유지되어야 한다고 판단하면 keep=true 로 두고 이유를 쓴다.\n"
        + (f"[단위 오류 후보] {unit['why']} → 상반기 {unit['H1']:,.1f} / 하반기 {unit['H2']:,.1f} / 연간 {unit['FY']:,.1f}억\n" if unit else "")
        + '반드시 JSON 만 출력: {"keep": false, "revised_forecast": {"H1": 억단위 숫자, "H2": 숫자, "FY": 숫자}, "derivation": "각 수치의 유도식과 근거 id (한두 문장)", '
        '"basis": ["Q21","D30"], "premise_edits": [{"s": "S#", "action": "remove|revise", "revised": "문장 전체 또는 빈 문자열", "why": "한 줄"}], "note": "한 줄"}\n\n'
        "[원장]\n" + "\n".join(f"{f['id']} {f['text']}" for f in ledger if f["id"][0] in "DQFCRE" and not posthoc(f["text"])) + "\n\n[전제 판정]\n"
        + "\n".join(f"{p['s']} [{p['supports_conclusion']}] {sents.get(p['s'],'')[:160]} — {p['why'][:120]}" for p in prem)
        + "\n\n[검산 판정]\n" + "\n".join(f"{x['r']} {'정합' if x['consistent'] else '불일치'} — {x['why'][:120]}" for x in a.get("arithmetic", []) if not posthoc(lid.get(x['r'], {}).get('text', '')))
    )
    messages = [{"role": "system", "content": "재무 보고서 결론 교정 도구. 원장만 근거로 삼고 유도식을 명시하며 반드시 JSON 만 출력한다."}, {"role": "user", "content": task}]
    t0 = time.time()
    try:
        resp = chat_json(base_url, model, messages, max_tokens=3000)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {ex}", "seconds": round(time.time() - t0, 1)}
    if not isinstance(resp, dict):
        return {"error": "no json", "seconds": round(time.time() - t0, 1)}
    rf = resp.get("revised_forecast") or {}
    H1, H2, FY = _num(rf.get("H1")), _num(rf.get("H2")), _num(rf.get("FY"))
    checks = []
    if H1 is not None and H2 is not None and FY is not None:
        checks.append({"name": "상반기+하반기=연간", "ok": abs(H1 + H2 - FY) <= max(1.0, abs(FY) * 0.01), "detail": f"{H1:,.1f}+{H2:,.1f}={H1+H2:,.1f} vs {FY:,.1f}"})
        q1 = next((float(m.group(1).replace(",", "")) for f in ledger for m in [re.search(r"2026/Q1 (?:영업이익 )?=?\s*(-?[\d,]+(?:\.\d+)?)억", f["text"])] if m), None)
        if q1:
            checks.append({"name": "상반기 ≥ Q1 실적(양수일 때)", "ok": (H1 >= q1) if q1 > 0 else True, "detail": f"상반기 {H1:,.1f} vs Q1 {q1:,.1f}"})
    edits = []
    for e in (resp.get("premise_edits") or []):
        if not isinstance(e, dict) or e.get("s") not in sents:
            continue
        original = sents[e["s"]]; revised = str(e.get("revised", "")).strip(); action = str(e.get("action", "revise"))
        ok, bad = (True, []) if action == "remove" or not revised else check_revision(original, revised, ledger)
        edits.append({"s": e["s"], "action": action, "original": original, "revised": revised, "why": str(e.get("why", ""))[:200], "status": "accepted" if ok else "held", "ungrounded_numbers": bad})
    deriv = str(resp.get("derivation", ""))
    used_posthoc = any(posthoc(lid[b]["text"]) for b in (resp.get("basis") or []) if b in lid) or bool(re.search(r"사후|반기보고서|2026/Q[234]|Q2 (실적|확정)|실제 (상반기 )?영업이익", deriv))
    checks.append({"name": "사후 실현값 미사용(작성 시점 정보만)", "ok": not used_posthoc, "detail": "근거 id·유도식에 2026 반기 실제값이 없음" if not used_posthoc else "2026 반기 실제값을 근거로 씀"})
    out = {"keep": bool(resp.get("keep")), "revised_forecast": {"H1": H1, "H2": H2, "FY": FY}, "derivation": str(resp.get("derivation", ""))[:600],
           "basis": [b for b in (resp.get("basis") or []) if b in lid], "premise_edits": edits, "note": str(resp.get("note", ""))[:300],
           "unit_fix_candidate": unit, "checks": checks, "verdict": a.get("verdict"), "seconds": round(time.time() - t0, 1)}
    out["status"] = "keep" if out["keep"] else ("accepted" if all(c["ok"] for c in checks) and H1 is not None else "held")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", required=True)
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:30000/v1")
    ap.add_argument("--llm-model", default="dfischermittwald/Qwen3.8-27B-NVFP4-DFlash2")
    ap.add_argument("--conclusion-only", action="store_true", help="문장 수정은 최신 결과 파일에서 가져오고 결론 수정 제안만 다시 만든다")
    args = ap.parse_args()
    names = set(args.names.split(","))
    recs = load_records(names)
    prev = {}
    if args.conclusion_only:
        pf = sorted(glob.glob(str(OUT / "case_llm_revisions_*.json")))
        if pf:
            prev = {r["name"]: r for r in json.load(open(pf[-1], encoding="utf-8"))["reports"]}
    results = []
    for nm in args.names.split(","):
        if nm not in recs:
            print("no record:", nm); continue
        if args.conclusion_only and nm in prev:
            r = prev[nm]
        else:
            r = revise_document(recs[nm], args.llm_base_url, args.llm_model)
            t1 = time.time(); prop = propagate(recs[nm], r, args.llm_base_url, args.llm_model); r["propagation_seconds"] = round(time.time() - t1, 1)
            r["revisions"] += [x for x in prop if x.get("s")]
            r["propagation_error"] = next((x["note"] for x in prop if x.get("status") == "error"), None)
        r["conclusion_fix"] = revise_conclusion(recs[nm], args.llm_base_url, args.llm_model)
        results.append(r)
        acc = sum(1 for x in r["revisions"] if x["status"] == "accepted"); pr = sum(1 for x in r["revisions"] if x["status"] == "propagated")
        cf = r.get("conclusion_fix") or {}
        print(f"{nm}: data fixes {len(r['data_fixes'])} · revisions {len(r['revisions'])} (accepted {acc}, propagated {pr}, held {sum(1 for x in r['revisions'] if x['status']=='held')}) · conclusion fix: {cf.get('status') or cf.get('error') or '해당 없음(supported)'} · {r['seconds']}+{r['propagation_seconds']}s", flush=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = OUT / f"case_llm_revisions_{ts}.json"
    summ = {"reports": len(results), "data_fixes": sum(len(r["data_fixes"]) for r in results), "revisions": sum(len(r["revisions"]) for r in results),
            "accepted": sum(1 for r in results for x in r["revisions"] if x["status"] == "accepted"), "propagated": sum(1 for r in results for x in r["revisions"] if x["status"] == "propagated"), "held": sum(1 for r in results for x in r["revisions"] if x["status"] == "held"),
            "unchanged": sum(1 for r in results for x in r["revisions"] if x["status"] == "unchanged"), "errors": sum(1 for r in results if r["error"]),
            "conclusion_fixes": {k: sum(1 for r in results if (r.get("conclusion_fix") or {}).get("status") == k) for k in ("accepted", "held", "keep")}}
    json.dump({"summary": summ, "reports": results}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False)); print(out)


if __name__ == "__main__":
    main()
