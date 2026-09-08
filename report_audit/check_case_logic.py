#!/usr/bin/env python3
"""LLM 사례 보고서의 논리 검사 3종 (PLAN-013 남은 작업 12).

A. 분기 패턴 주장: "2023 Q4 적자/급감/최대" 류 주장을 문서 자체의 분기 영업이익 데이터와 대조한다.
B. 시간 역행 인과: (1) 뉴스 날짜가 설명 대상 기간의 끝보다 뒤인데 그 기간의 원인으로 제시된 문장,
   (2) N분기 실적 '발표·확인' 으로 그 분기가 끝나기 전 달의 주가 움직임을 설명한 문장.
C. 법인 혼동: 보고서 회사보다 연결 매출이 큰 관계사(따라서 연결 대상일 수 없는 회사)의 수주·백로그·실적을
   보고서 회사의 매출·영업이익 동인으로 서술한 문장. DART 매출로 비연결을 확정한다.

값 검사(check_case_reports.py)·DART 대조(dart_crosscheck.py)와 독립이며, 각 플래그에 근거(데이터 값·날짜·매출)를 붙인다.
사용: python3 -m report_audit.check_case_logic [--limit N] [--only 이름]
"""

from __future__ import annotations

import os

import argparse
import calendar
import datetime as dt
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
from report_audit.check_case_reports import parse_quarters, parse_table  # noqa: E402
from report_audit.dart_crosscheck import dart_fin, get_amounts, resolve2  # noqa: E402

BASE = Path(os.environ.get("CASE_REPORTS_DIR", REPO / "data/case_reports"))
OUT = REPO / "runs/report_audit/steps"; OUT.mkdir(parents=True, exist_ok=True)

NEWS_RE = re.compile(r"^\s*-\s*(20\d\d)\.(\d\d)\.(\d\d)\.?\s*(?:\(([^)]*)\))?\s*[:：]?\s*(.*)$")
PRICE_RE = re.compile(r"^\s*\d+\s+(\d{4})-(\d{2})-(\d{2})\s+([\d,.]+|NaN)\s*$")
QREF_RE = re.compile(r"(20\d\d)\s*[/년]?\s*Q([1-4])|(20\d\d)년\s*([1-4])분기|([1-4])분기\s*\((20\d\d)")


# ---------------------------------------------------------------- 공통
def sentences(text: str) -> list[str]:
    """데이터 라인(표·분기 리스트·주가 표·뉴스 라인)을 뺀 서술 문장."""
    keep = []
    for l in text.splitlines():
        if l.startswith("|") or "'Date'" in l or PRICE_RE.match(l) or NEWS_RE.match(l) or re.match(r"^\s*\[", l):
            continue
        if re.match(r"^\s*(\d\.\s|<출력|-\s*\[|-\s*주어진|-\s*이유|-\s*\d{4}년\s*(상반기|하반기|영업이익)\s*전망|\(중간 생략\)|작성일)", l) or l.strip().endswith(":") or not l.strip():
            continue
        keep.append(l)
    body = "\n".join(keep)
    parts = re.split(r"(?<=[.다음됨함임요])\s*\n|(?<=[가-힣)\]])\.\s+|(?<=\d\.)\s{2,}", body)
    return [p.strip() for p in parts if len(p.strip()) > 8]


ANAPHORA_RE = re.compile(r"^\s*[-+*\s]*(이는|이러한|이에|이로|해당|이것은|이러한|이와\s*함께)")


def with_antecedent(sents: list[str]) -> list[str]:
    """'이는 …' 처럼 앞 문장을 가리키는 문장은 앞 문장과 합쳐 하나의 판정 범위로 본다."""
    out = []
    for i, x in enumerate(sents):
        out.append((sents[i - 1] + " " + x) if i > 0 and ANAPHORA_RE.match(x) else x)
    return out


def section(text: str, n: str) -> str:
    m = re.search(rf"\n{n}\. .*?(?=\n[1-7]\. |\Z)", "\n" + text, re.S)
    return m.group(0) if m else ""


def qkey(y: str, q: str) -> str:
    return f"{y}/Q{q}"


def prev_q(key: str) -> str:
    y, q = key.split("/Q"); y, q = int(y), int(q)
    return qkey(str(y - 1), "4") if q == 1 else qkey(str(y), str(q - 1))


def same_q_last_year(key: str) -> str:
    y, q = key.split("/Q")
    return qkey(str(int(y) - 1), q)


# ---------------------------------------------------------------- A. 분기 패턴 주장
STATE_WORDS = [
    (r"적자에서\s*흑자(?:로)?\s*전환|흑자\s*전환|흑자로\s*전환", "turn_pos"), (r"흑자에서\s*적자(?:로)?\s*전환|적자\s*전환|적자로\s*전환", "turn_neg"),
    (r"적자|영업\s*손실|손실을\s*기록", "neg"), (r"흑자", "pos"),
    (r"사상\s*최대|역대\s*최대|사상\s*최고|역대\s*최고", "max_all"), (r"분기\s*최대|최대\s*(분기\s*)?실적|최고치|최고\s*실적|정점", "max_year"),
    (r"사상\s*최저|역대\s*최저", "min_all"), (r"최저|저점|바닥", "min_year"),
    ("급감|급락", "sharp_down"), ("급증|급등|급반등", "sharp_up"),
]


def quarter_refs(sent: str) -> list[tuple[int, int, str]]:
    out = []
    for m in QREF_RE.finditer(sent):
        if m.group(1):
            k = qkey(m.group(1), m.group(2))
        elif m.group(3):
            k = qkey(m.group(3), m.group(4))
        else:
            k = qkey(m.group(6), m.group(5))
        out.append((m.start(), m.end(), k))
    return out


def claim_context(sent: str, start: int, end: int) -> str:
    """분기 참조의 주장 문맥. 괄호 안 목록이면 여는 괄호 앞 60자, 아니면 앞뒤 45자(절 경계까지)."""
    open_paren = sent.rfind("(", 0, start)
    close_paren = sent.find(")", end)
    if open_paren != -1 and close_paren != -1 and "," in sent[open_paren:close_paren] and len(quarter_refs(sent[open_paren:close_paren])) >= 2:
        lead = re.split(r"[,;]|\s(?:그러나|반면|하지만|다만)\s|(?<=[다음됨함임])\.", sent[max(0, open_paren - 60):open_paren])[-1]
        return "" if re.search(r"20\d\d", lead) else lead
    before = sent[max(0, start - 45):start]
    before = re.split(r"[,;()]|\s(?:그러나|반면|하지만|다만)\s|(?<=[다음됨함임])\.", before)[-1]
    if re.search(r"20\d\d|Q[1-4]|분기|반기|연속|이후|이전|부터", before):
        before = ""  # 앞 절이 다른 기간을 말하면 붙이지 않는다
    after = sent[end:end + 45]
    after = re.split(r"[,;()]|\s(?:그러나|반면|하지만|다만)\s|(?<=[다음됨함임])\.", after)[0]
    if re.search(r"20\d\d|Q[1-4]|[1-4]분기|반기", after):
        after = re.split(r"20\d\d|Q[1-4]|[1-4]분기|반기", after)[0]  # 뒤 절의 다른 기간 참조 앞까지만
    return before + " " + after


def eval_state(state: str, key: str, q: dict[str, float]) -> tuple[bool | None, str]:
    """주장 상태가 분기 데이터와 맞는가. (None, 이유) 는 판정 불가."""
    v = q.get(key)
    if v is None:
        return None, "분기 데이터 없음"
    y = key.split("/")[0]
    year_vals = [x for k, x in q.items() if k.startswith(y + "/")]
    p = q.get(prev_q(key)); ly = q.get(same_q_last_year(key))
    if state == "neg":
        return v < 0, f"{key}={v:,.1f}억"
    if state == "pos":
        return v > 0, f"{key}={v:,.1f}억"
    if state in ("turn_pos", "turn_neg"):
        refs = [(prev_q(key), p), (same_q_last_year(key), ly)]
        have = [(rk, rv) for rk, rv in refs if rv is not None]
        if not have:
            return None, "비교 분기 없음"
        ok = any((rv < 0 <= v) if state == "turn_pos" else (rv >= 0 > v) for _, rv in have)
        return ok, ", ".join(f"{rk}={rv:,.1f}" for rk, rv in have) + f" → {key}={v:,.1f}"
    if state == "max_all":
        return v >= max(q.values()) - 1e-9, f"{key}={v:,.1f}, 전체 최대={max(q.values()):,.1f}"
    if state == "max_year":
        return v >= max(year_vals) - 1e-9, f"{key}={v:,.1f}, {y} 최대={max(year_vals):,.1f}"
    if state == "min_all":
        return v <= min(q.values()) + 1e-9, f"{key}={v:,.1f}, 전체 최소={min(q.values()):,.1f}"
    if state == "min_year":
        return v <= min(year_vals) + 1e-9, f"{key}={v:,.1f}, {y} 최소={min(year_vals):,.1f}"
    if state in ("sharp_down", "sharp_up"):
        # 전 분기 또는 전년 동기 어느 쪽이든 20% 이상 변동이면 인정 (보수적)
        refs = [(prev_q(key), p), (same_q_last_year(key), ly)]
        ok = False; desc = []
        for rk, rv in refs:
            if rv is None:
                continue
            desc.append(f"{rk}={rv:,.1f}")
            if state == "sharp_down":
                ok |= (v < rv and (rv <= 0 or (rv - v) / abs(rv) >= 0.2))
            else:
                ok |= (v > rv and (rv <= 0 or (v - rv) / abs(rv) >= 0.2))
        if not desc:
            return None, "비교 분기 없음"
        return ok, f"{key}={v:,.1f} vs " + ", ".join(desc)
    return None, "미지원"


def check_quarter_claims(text: str, quarters: dict[str, float]) -> dict:
    flags = []; checked = 0
    if not quarters:
        return {"checked": 0, "flags": flags}
    for sent in sentences(text):
        refs = quarter_refs(sent)
        if not refs or len(sent) > 600:
            continue
        for start, end, key in refs:
            if sent[end:end + 1] in "~-–" or sent[max(0, start - 1):start] in "~-–":
                continue  # "2~4분기" 같은 범위 표기
            ctx = claim_context(sent, start, end)
            in_list = bool(re.search(r"\([^)]*$", sent[:start]))  # 괄호 목록 안의 참조
            if re.search(r"전망|예상|추정|가정|시나리오|컨센서스|목표|E\b|우려|가능성|리스크|패턴 반복|것으로 보|기대|근접|육박|버금", ctx) and not in_list:
                continue
            if re.search(r"통상|경향|계절성|패턴", ctx) and not in_list:
                continue  # 일반론(어느 분기가 보통 어떻다)은 특정 분기 주장이 아니다
            states = []
            for pat, st in STATE_WORDS:
                for m in re.finditer(pat, ctx):
                    # 상태어 바로 앞(12자)에 다른 명사(수주잔고·매출·주가 등)가 있으면 이익에 대한 주장이 아니다
                    head = ctx[max(0, m.start() - 30):m.start()]
                    # 상태어의 주어가 이익·실적이 아닌 다른 명사(수주잔고·매출·주가·부문 등)이면 제외:
                    # 가장 가까운 후보가 어느 쪽인지로 판단한다 ("비용 집중으로 인해 이익이 급감" 은 이익이 주어)
                    others = [mm.end() for mm in re.finditer(r"수주|잔고|매출|주가|점유율|가격|비용|수요|생산|판매|출하|환율|금리|물량|손익\s*\d|합산|부문|사업|법인|국내|해외|이익률|마진|률", head)]
                    subj = [mm.end() for mm in re.finditer(r"(?<!영업)이익(?!률)|영업이익(?!률)|실적|흑자|적자|손실", head)]
                    if others and (not subj or max(others) > max(subj)):
                        continue
                    if st in ("min_year", "min_all", "max_year", "max_all") and re.search(r"주가|원대|만\s*원", ctx):
                        continue  # 주가의 바닥·최고치
                    states.append((m.start(), st))
            if not states:
                continue
            # 전환 표현이 있으면 그것만 본다(부호 상태어는 전환의 일부). 없으면 참조 위치에 가장 가까운 상태어.
            turns = [st for _, st in states if st.startswith("turn_")]
            if turns:
                alts = [turns[0]]
            elif re.search(r"하거나|또는|혹은|이나\s", ctx):
                alts = [st for _, st in sorted(states)]
            else:
                ref_pos = len(sent[max(0, start - 45):start]) if ctx.startswith(" ") is False else 0
                alts = [min(states, key=lambda x: abs(x[0] - ref_pos))[1]]
            # 우선순위: 전환 > 부호 > 최대/최소 > 급변
            results = [(st, *eval_state(st, key, quarters)) for st in dict.fromkeys(alts)]
            decided = [(st, ok, why) for st, ok, why in results if ok is not None]
            if not decided:
                continue
            checked += 1
            if not any(ok for _, ok, _ in decided):
                flags.append({"quarter": key, "claims": [st for st, _, _ in decided], "evidence": "; ".join(why for _, _, why in decided),
                              "context": ctx.strip()[:120], "sentence": sent[:260]})
    return {"checked": checked, "flags": flags}


# ---------------------------------------------------------------- B. 시간 역행
STOP = set("""매출 매출액 영업이익 영업 이익 공시 전망 증권 증권사 목표주가 투자의견 매수 리포트 연결 기준 전년 대비 억원 조원 실적 잠정 발표 분기 상향 하향
기대 코멘트 언급 연간 개선 회사 그룹 지주 자회사 관련 확정 기준 제시 전망치 유지 보고서 연구원 애널리스트 반기 상반기 하반기 참고용 수요 가격 설비 리스크
등 및 대한 통해 위한 따른 관련 가능성 변수 요인 확대 감소 증가 계획 추진 체결 확보 이상 수준 규모 시장 국내 해외 글로벌 사업 부문 신규 기존 최대 최초 최근
예상 전년비 전년동기 동기 대비 수주 계약 협력 발표했다 밝혔다 것으로 있다 한다 된다 했다 이번 올해 내년 지난해 작년 억 조 원 만 천 년 월 일""".split())


def news_items(text: str) -> list[dict]:
    items = []
    for l in text.splitlines():
        m = NEWS_RE.match(l)
        if not m:
            continue
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        body = m.group(5) or ""
        quoted = re.findall(r"[‘'\"“]([^’'\"”]{2,25})[’'\"”]", body)
        toks = [t for t in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9&·\-]{1,}", body) if t not in STOP and len(t) >= 2]
        items.append({"date": d, "entity": m.group(4), "text": body, "quoted": quoted, "tokens": set(toks)})
    # 문서 내 뉴스 절반 이상에 나오는 토큰은 식별력 없음
    if items:
        freq = Counter(t for it in items for t in it["tokens"])
        common = {t for t, c in freq.items() if c >= max(3, len(items) * 0.4)}
        for it in items:
            it["tokens"] -= common
            it["rare"] = {t for t in it["tokens"] if freq[t] == 1}
    return items


def period_end(sent: str) -> list[tuple[dt.date, str]]:
    out = []
    for m in re.finditer(r"(20\d\d)\s*[/년]?\s*Q([1-4])|(20\d\d)년[은는의]?\s*([1-4])분기", sent):
        y = int(m.group(1) or m.group(3)); q = int(m.group(2) or m.group(4)); mo = 3 * q
        out.append((dt.date(y, mo, calendar.monthrange(y, mo)[1]), m.group(0)))
    for m in re.finditer(r"(20\d\d)년\s*(상반기|하반기)", sent):
        y = int(m.group(1)); out.append((dt.date(y, 6, 30) if m.group(2) == "상반기" else dt.date(y, 12, 31), m.group(0)))
    for m in re.finditer(r"(20\d\d)년\s*(\d{1,2})월(?!\s*\d+\s*일)", sent):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            out.append((dt.date(y, mo, calendar.monthrange(y, mo)[1]), m.group(0)))
    return out


CAUSE_RE = re.compile(r"기인|원인|영향으로|영향을\s*(?:받|미치|주)|으로\s*인해|로\s*인해|때문|따른|따라|반영되|반영한|반영된|배경|초래|야기")
ANTICIP_RE = re.compile(r"기대|선반영|전망|예상|앞두|시나리오|가정|전제")


EVENT_RE = re.compile(r"사고|사망|화재|폭발|파업|매각|인수|합병|폐쇄|기소|판결|과징금|해지|파산|리콜|상장|폐지|출시|준공|착공|체결|선정|타결|적발|고발|압수|영업정지")


def news_match(sent: str, item: dict) -> list[str]:
    """고유 표현(인용구, 영문·숫자 포함 토큰, 문서 내 그 뉴스에만 나오는 3자 이상 토큰) 하나 이상 + 사건 명사 공유."""
    hits = [q for q in item["quoted"] if q in sent]
    rare = [t for t in item.get("rare", set()) if len(t) >= 3 and t in sent]
    alnum = [t for t in item["tokens"] if re.search(r"[A-Za-z0-9]", t) and len(t) >= 3 and t in sent]
    ev_news = set(EVENT_RE.findall(item["text"])); ev_sent = set(EVENT_RE.findall(sent))
    if (hits or rare or alnum) and (ev_news & ev_sent):
        return hits + alnum + rare
    return []


def check_temporal(text: str, company: str) -> dict:
    items = news_items(text); flags = []; checked = 0
    narrative = "\n".join(section(text, n) for n in ("1", "2", "3", "6"))
    for sent in with_antecedent(sentences(narrative)):
        if not CAUSE_RE.search(sent) or ANTICIP_RE.search(sent) or len(sent) > 700:
            continue
        pers = period_end(sent)
        if not pers:
            continue
        # 기간 후보 위치 (연도 단독도 귀속 후보에 넣어, 뉴스가 다른 연도 절에 붙은 경우를 걸러낸다)
        anchors = [(sent.find(lbl), p, lbl) for p, lbl in pers if sent.find(lbl) >= 0]
        for m in re.finditer(r"(20\d\d)년(?!\s*[1-4]분기|\s*(상|하)반기|\s*\d{1,2}월|[은는의]?\s*[1-4]분기)", sent):
            anchors.append((m.start(), dt.date(int(m.group(1)), 12, 31), m.group(0)))
        for m in re.finditer(r"(?<![\d년/])\s([1-4])분기", sent):
            prior_years = [int(y) for y in re.findall(r"(20\d\d)", sent[:m.start()])]
            if prior_years:
                q = int(m.group(1)); y = prior_years[-1]
                anchors.append((m.start(), dt.date(y, 3 * q, calendar.monthrange(y, 3 * q)[1]), f"{y} {m.group(0).strip()}"))
        if not anchors:
            continue
        for it in items:
            m = news_match(sent, it)
            if not m:
                continue
            tok_pos = min((sent.find(t) for t in m if sent.find(t) >= 0), default=0)
            pos, effect_end, lbl = min(anchors, key=lambda a: abs(a[0] - tok_pos))
            if lbl.replace(" ", "") in it["text"].replace(" ", "") or (re.search(r"실적|공시|잠정|매출|영업이익|순이익|순익|이익|손실|적자|결산|분기|반기|연간|회계|적립|충당|준비금", it["text"]) and it["date"] <= effect_end + dt.timedelta(days=90)):
                continue
            checked += 1
            if it["date"] > effect_end + dt.timedelta(days=14):
                flags.append({"kind": "news_after_effect", "effect_period": lbl, "effect_end": effect_end.isoformat(),
                              "news_date": it["date"].isoformat(), "news": it["text"][:120], "matched": m[:4], "sentence": sent[:260]})
    # (2) 실적 발표 → 발표 전 달의 주가
    sec4 = sentences(section(text, "4")); stock_flags = []
    for i, sent in enumerate(sec4):
        m = re.search(r"(20\d\d)\s*[/년]?\s*Q([1-4])|(20\d\d)년[은는의]?\s*([1-4])분기", sent)
        if not m or not re.search(r"실적|영업이익", sent) or not re.search(r"발표|공시|확인|기록|호전|턴어라운드", sent) or ANTICIP_RE.search(sent):
            continue
        y = int(m.group(1) or m.group(3)); q = int(m.group(2) or m.group(4)); q_end_month = 3 * q
        scope = sent if not ANAPHORA_RE.match(sent) or i == 0 else sec4[i - 1] + " " + sent
        if re.search(r"이전|앞서|선행|미리", scope):
            continue  # 문서가 발표 전 움직임임을 스스로 인정
        priced = []  # 가격이 붙은 달만 (예: "2월 51,700원", "3월(55,400원)")
        for mm in re.finditer(r"(?:(20\d\d)년\s*)?(\d{1,2})월\s*\(?\s*[\d,]{4,}\s*원", scope):
            yy = int(mm.group(1)) if mm.group(1) else None; mo = int(mm.group(2))
            if 1 <= mo <= 12 and (yy is None or yy == y):
                priced.append(mo)
        if not priced:
            continue
        early = [mo for mo in priced if 3 * q - 4 <= mo <= q_end_month]
        late = [mo for mo in priced if mo > q_end_month]
        checked += 1
        if len(early) >= 2 or (early and not late):
            if re.search(r"급등|상승|반등|급락|하락|조정", scope):
                stock_flags.append({"kind": "results_before_release", "quarter": qkey(str(y), str(q)), "months_explained": sorted(set(early)),
                                    "sentence": scope[:300]})
    return {"checked": checked, "flags": flags + stock_flags}


# ---------------------------------------------------------------- C. 법인 혼동
IMPACT_RE = re.compile(r"백로그|수주|매출|원가|외형")


def related_entities(text: str, company: str, rows_codes) -> list[tuple[str, str]]:
    stem = company if len(company) <= 3 else company[:2]
    if len(stem) < 2:
        return []
    cands = Counter()
    for l in text.splitlines():
        if not NEWS_RE.match(l):
            continue
        for t in re.findall(r"[A-Za-z가-힣&]{2,}", l):
            if t.startswith(stem) and t != company and t.replace(" ", "") != company.replace(" ", "") and len(t) > len(stem):
                cands[t] += 1
    out = []
    for name, c in cands.most_common(8):
        if c < 2:
            continue
        r = resolve2(name, rows_codes)
        if r and r[0]:
            out.append((name, r[0]))
    return out


def check_entity(text: str, company: str, code: str | None, rows_codes) -> dict:
    flags = []; checked = 0
    if not code:
        return {"checked": 0, "flags": flags}
    own = None
    for y in (2024, 2023):
        own = get_amounts(dart_fin(code, y, "11011"), "매출액", False)
        if own:
            own_year = y; break
    if not own:
        return {"checked": 0, "flags": flags}
    narrative = "\n".join(section(text, n) for n in ("1", "2", "3", "6"))
    sents = sentences(narrative)
    for name, rcode in related_entities(text, company, rows_codes):
        rev = get_amounts(dart_fin(rcode, own_year, "11011"), "매출액", False)
        if not rev:
            continue
        checked += 1
        if rev <= own * 1.05:
            continue  # 연결 자회사일 수 있음 → 판정 불가
        for sent in sents:
            if name in sent and IMPACT_RE.search(sent) and re.search(r"기여|확대|개선|가시성|동인|견인|반영", sent) and not re.search(r"지분법|배당|지분\s*\d", sent) and len(sent) < 500:
                flags.append({"related": name, "related_revenue_bn": round(rev, 0), "own_revenue_bn": round(own, 0), "year": own_year, "sentence": sent[:260]})
    return {"checked": checked, "flags": flags}


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int); ap.add_argument("--only"); args = ap.parse_args()
    rows_codes = json.load(open(REPO / "data/dart/corp_codes_listed.json", encoding="utf-8")) if (REPO / "data/dart/corp_codes_listed.json").exists() else []
    files = sorted(BASE.glob("*.md"))
    if args.only:
        files = [f for f in files if f.stem == args.only]
    if args.limit:
        files = files[: args.limit]
    t0 = time.time(); per = []; tot = Counter()
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        quarters = parse_quarters(text)
        r = resolve2(f.stem, rows_codes); code = r[0] if r else None
        a = check_quarter_claims(text, quarters); b = check_temporal(text, f.stem); c = check_entity(text, f.stem, code, rows_codes)
        per.append({"name": f.stem, "quarter_claims": a, "temporal": b, "entity": c})
        tot["A_checked"] += a["checked"]; tot["A_flags"] += len(a["flags"]); tot["A_docs_flagged"] += bool(a["flags"])
        tot["B_checked"] += b["checked"]; tot["B_news_after_effect"] += sum(1 for x in b["flags"] if x["kind"] == "news_after_effect")
        tot["B_results_before_release"] += sum(1 for x in b["flags"] if x["kind"] == "results_before_release"); tot["B_docs_flagged"] += bool(b["flags"])
        tot["C_checked"] += c["checked"]; tot["C_flags"] += len(c["flags"]); tot["C_docs_flagged"] += bool(c["flags"])
    summary = {"reports": len(files), **dict(tot), "elapsed_sec": round(time.time() - t0, 1)}
    ts = time.strftime("%Y%m%d_%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"case_logic_check_{ts}.json"
    json.dump({"summary": summary, "reports": per}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(summary, ensure_ascii=False, indent=1)); print(out)


if __name__ == "__main__":
    main()
