#!/usr/bin/env python3
"""DART 고유번호 목록을 내려받아 상장사만 [corp_code, corp_name, stock_code] 로 저장한다.

  python3 -m report_audit.dart_corp_codes            # data/dart/corpCode.zip 내려받기 → data/dart/corp_codes_listed.json
  python3 -m report_audit.dart_corp_codes --offline  # 이미 있는 corpCode.zip 만 다시 풀기
.env 의 DART_API_KEY 를 쓴다 (https://opendart.fss.or.kr 에서 발급). 두 파일 모두 gitignore 대상이다.
"""
from __future__ import annotations

import os

import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "data/dart"; ZIP = OUT_DIR / "corpCode.zip"; OUT = OUT_DIR / "corp_codes_listed.json"


def api_key() -> str:
    if os.environ.get("DART_API_KEY"):
        return os.environ["DART_API_KEY"]
    if not (REPO / ".env").exists():
        sys.exit(".env 가 없습니다 (DART_API_KEY 필요)")
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DART_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(".env 에 DART_API_KEY 가 없습니다")


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--offline", action="store_true"); args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not args.offline:
        url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key()}"
        data = urllib.request.urlopen(url, timeout=120).read()
        if not data.startswith(b"PK"):
            sys.exit("corpCode.xml 응답이 zip 이 아닙니다 (키 오류?): " + data[:200].decode("utf-8", "replace"))
        ZIP.write_bytes(data)
    with zipfile.ZipFile(ZIP) as z:
        xml = z.read(z.namelist()[0])
    rows = []
    for el in ET.fromstring(xml).iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:
            rows.append([el.findtext("corp_code"), el.findtext("corp_name"), stock])
    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{len(rows)} listed companies → {OUT}")


if __name__ == "__main__":
    main()
