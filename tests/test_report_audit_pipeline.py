"""report_audit 오프라인 테스트: 전처리·표 파싱·단위 판정·원샷 실행기(LLM 없이)·FastAPI 라우터. 네트워크와 LLM 을 쓰지 않는다."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from report_audit.case_preprocess import normalize_numbers
from report_audit.check_case_reports import parse_quarters, parse_table
from report_audit.dart_crosscheck import is_pow10_ratio
from report_audit.run_report_audit import audit_report

SAMPLE = """작성일: 2026.06.22

1. 재무제표 및 핵심 지표 요약
- 요약된 재무 항목과 값: (단위: 억 원)
| 항목 | 2023 | 2024 | 2025 |
|---|---|---|---|
| 매출액 | 11,363 | 9,689 | 10,064 |
| 영업이익 | 1,166 | 981 | 1,026 |
| 당기순이익 | -1,054 | -3,839 | 695 |

2. 분기별 영업이익 분석
- [{'Date': '2025/Q1', '영업이익': 2.632e10}, {'Date': '2025/Q2', '영업이익': 21600000000.0}]

6. 2026년 연간 영업이익 전망
- 2026년 상반기 전망: 749,000,000,000
- 2026년 하반기 전망: 755,000,000,000
- 2026년 영업이익 전망: 1,504,000,000,000
"""


class PreprocessTest(unittest.TestCase):
    def test_scientific_and_raw_integers_are_grouped(self) -> None:
        text, stats = normalize_numbers("영업이익 8.275000e+10 원, 21870000000.0 원, nan")
        self.assertIn("82,750,000,000", text)
        self.assertIn("21,870,000,000", text)
        self.assertNotIn("e+10", text)
        self.assertTrue(stats)


class ParsingTest(unittest.TestCase):
    def test_table_and_quarters(self) -> None:
        text, _ = normalize_numbers(SAMPLE)
        facts, unit = parse_table(text)
        self.assertEqual(unit, "억원")
        self.assertEqual(facts["영업이익"]["2025"], 1026.0)
        self.assertEqual(facts["당기순이익"]["2024"], -3839.0)
        quarters = parse_quarters(text)
        self.assertAlmostEqual(quarters["2025/Q1"], 263.2)  # 억원으로 정규화
        self.assertAlmostEqual(quarters["2025/Q2"], 216.0)

    def test_pow10_ratio_requires_same_sign_and_tight_tolerance(self) -> None:
        self.assertTrue(is_pow10_ratio(1_026_000, 1_026))
        self.assertFalse(is_pow10_ratio(-1_026_000, 1_026))
        self.assertFalse(is_pow10_ratio(1_028_000, 100_000))  # 10.28× is a coincidence, not a unit error


class OneShotRunnerTest(unittest.TestCase):
    def test_no_llm_run_writes_html_json_and_warns_on_unmapped_company(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "존재하지않는회사.md"; src.write_text(SAMPLE, encoding="utf-8")
            res = audit_report([src], Path(td) / "out", "t1", use_llm=False)
            self.assertTrue(Path(res["html"]).exists()); self.assertTrue(Path(res["json"]).exists())
            self.assertEqual(res["reports"][0]["name"], "존재하지않는회사")
            self.assertTrue(any("매핑 실패" in w for w in res.get("warnings", [])))
            data = json.loads(Path(res["json"]).read_text(encoding="utf-8"))
            self.assertEqual(data["steps"], ["preprocess", "dart_crosscheck"])


class RouterTest(unittest.TestCase):
    def test_post_without_llm_and_fetch_html(self) -> None:
        from web_app.main import app
        client = TestClient(app)
        r = client.post("/api/report-audit", json={"name": "존재하지않는회사", "markdown_text": SAMPLE, "llm": False})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("html_url", body); self.assertFalse(body["summary"]["llm"])
        page = client.get(body["html_url"])
        self.assertEqual(page.status_code, 200); self.assertIn("영업이익 전망 보고서 LLM 감사", page.text)
        self.assertEqual(client.get("/api/report-audit/nope/report.html").status_code, 404)
        self.assertEqual(client.post("/api/report-audit", json={"markdown_text": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
