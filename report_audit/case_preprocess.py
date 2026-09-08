#!/usr/bin/env python3
"""사례 보고서 숫자 표기 전처리.

LLM 파이프라인이 데이터프레임을 그대로 찍어 넣은 흔적을 사람이 읽을 수 있는 표기로 바꾼다. 값은 바꾸지 않는다.
- 지수 표기 8.275000e+10 → 82,750,000,000 (정수가 아니면 소수 둘째 자리까지)
- 콤마 없는 7자리 이상 정수와 끝의 .0: 21870000000.0 → 21,870,000,000
- 소문자 nan → NaN

함수 normalize_numbers(text) 는 렌더러·검사기에서 공용으로 쓰고, CLI 는 정규화한 사본을 만든다.
사용: python3 -m report_audit.case_preprocess [--src DIR] [--dst DIR]
"""
from __future__ import annotations

import os

import argparse
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCI_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)[eE]([+-]?\d+)(?![\w.])")
RAWNUM_RE = re.compile(r"(?<![\d,.\w])(-?\d{7,})(?:\.0+)?(?![\d,])")
NAN_RE = re.compile(r"(?<![\w])nan(?![\w])")


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-6 * max(1.0, abs(v)):
        return f"{int(round(v)):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def normalize_numbers(text: str) -> tuple[str, dict]:
    stats = {"sci": 0, "raw": 0, "nan": 0}

    def sci(m):
        stats["sci"] += 1
        return _fmt(float(m.group(0)))

    def raw(m):
        stats["raw"] += 1
        return f"{int(m.group(1)):,}"

    def nan(m):
        stats["nan"] += 1
        return "NaN"

    text = SCI_RE.sub(sci, text)
    text = RAWNUM_RE.sub(raw, text)
    text = NAN_RE.sub(nan, text)
    return text, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("CASE_REPORTS_DIR", str(REPO / "data/case_reports")))
    ap.add_argument("--dst", default=str(Path(__file__).resolve().parents[1] / "data/case_reports_normalized"))
    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst); dst.mkdir(parents=True, exist_ok=True)
    tot = {"sci": 0, "raw": 0, "nan": 0}; n = 0
    for f in sorted(src.glob("*.md")):
        t, st = normalize_numbers(f.read_text(encoding="utf-8", errors="replace"))
        (dst / f.name).write_text(t, encoding="utf-8"); n += 1
        for k in tot: tot[k] += st[k]
    print(f"{n} files → {dst}; replaced: 지수 표기 {tot['sci']:,}, 콤마 없는 큰 수 {tot['raw']:,}, nan {tot['nan']:,}")


if __name__ == "__main__":
    main()
