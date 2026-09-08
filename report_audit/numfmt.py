"""숫자 표기 정규화: 억원 기준 정수값 <-> 한국어 재무 표기, 퍼센트 표기.

벤치마크 채점은 이 모듈의 규칙에 고정된다. 표기 규칙:
- 금액은 억원 정수. 1조 이상은 "X조 Y,ZZZ억원", 억 단위가 0이면 "X조원". 1조 미만은 "Y,ZZZ억원".
- 음수는 앞에 '-' 를 붙인다.
- 비율은 소수 1자리 퍼센트 "12.3%". 음수는 "-3.4%".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EOK_PER_JO = 10_000

# 조/억 패턴을 먼저 두어 "5조 3,000억원" 이 "3,000억원" 으로 잘리지 않게 한다.
# 소수 조 표기("6.5조원", 애널리스트 문체)도 읽는다. 생성기는 정수 조 + 억 표기만 쓴다.
_KRW_RE = re.compile(
    r"(?P<neg>-)?(?:(?P<jo>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)조(?:\s*(?P<eok_after_jo>\d{1,3}(?:,\d{3})*|\d+)억)?\s?원"
    r"|(?P<eok>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?P<unit>천억|백억|십억|억|백만)\s?원)"
)
_UNIT_EOK = {"천억": 1000.0, "백억": 100.0, "십억": 10.0, "억": 1.0, "백만": 0.01}
_PCT_RE = re.compile(r"(?P<pct>-?\d+(?:\.\d+)?)%")
# 퍼센트포인트 표기(예: 2.1%p)는 별도 처리 대상이 아니므로 뒤에 p가 붙으면 제외한다.
NUMBER_RE = re.compile(rf"(?:{_KRW_RE.pattern})|(?:{_PCT_RE.pattern}(?!p))")


@dataclass(frozen=True)
class ParsedNumber:
    kind: str  # "krw" | "pct"
    value: float  # krw: 억원, pct: 퍼센트 값 (12.3 -> 12.3)
    text: str


def fmt_krw(eok: float) -> str:
    v = int(round(eok))
    neg = v < 0
    v = abs(v)
    jo, rem = divmod(v, EOK_PER_JO)
    if jo > 0:
        s = f"{jo:,}조원" if rem == 0 else f"{jo:,}조 {rem:,}억원"
    else:
        s = f"{rem:,}억원"
    return ("-" if neg else "") + s


def fmt_pct(ratio: float) -> str:
    """비율(0.123) -> '12.3%'. -0.0 표기를 피한다."""
    v = round(ratio * 100, 1)
    if v == 0:
        v = 0.0
    return f"{v:.1f}%"


def parse_number(text: str) -> ParsedNumber | None:
    m = NUMBER_RE.fullmatch(text.strip())
    if not m:
        return None
    return _from_match(m)


def _from_match(m: re.Match) -> ParsedNumber:
    if m.group("pct") is not None:
        return ParsedNumber("pct", float(m.group("pct")), m.group(0))
    neg = -1 if m.group("neg") else 1
    if m.group("jo") is not None:
        jo = float(m.group("jo").replace(",", ""))
        eok = int((m.group("eok_after_jo") or "0").replace(",", ""))
        return ParsedNumber("krw", neg * (jo * EOK_PER_JO + eok), m.group(0))
    eok = float(m.group("eok").replace(",", "")) * _UNIT_EOK.get(m.group("unit") or "억", 1.0)
    return ParsedNumber("krw", neg * eok, m.group(0))


def find_numbers(text: str) -> list[tuple[int, int, ParsedNumber]]:
    out = []
    for m in NUMBER_RE.finditer(text):
        out.append((m.start(), m.end(), _from_match(m)))
    return out


# 채점 허용 오차: 표기 정밀도의 절반.
KRW_TOL = 0.5  # 억원
PCT_TOL = 0.0005  # 비율 단위 (0.05 퍼센트포인트)


def values_match(kind: str, a: float, b: float) -> bool:
    tol = KRW_TOL if kind == "krw" else PCT_TOL
    return abs(a - b) <= tol + 1e-9
