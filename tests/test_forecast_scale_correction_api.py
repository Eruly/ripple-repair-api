import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from web_app.main import (
    ForecastCorrectRequest,
    ForecastCorrectionPreviewRequest,
    ForecastScaleCorrectionRequest,
    _apply_factreasoner_markdown_corrections,
    _apply_scale_factreasoner_cascades,
    api_operating_profit_correct,
    api_operating_profit_correction_preview,
    api_operating_profit_scale_correction,
)
from web_app.pipeline.forecast_scale_correction import (
    correct_operating_profit_forecast_scale,
    evaluate_half_year_identity,
    extract_consensus_blocks,
    latest_consensus_won,
    scale_cluster_is_locked,
)


class ForecastScaleCorrectionTest(unittest.TestCase):
    def test_corrects_all_cases_by_consensus_scale_factor(self) -> None:
        text = """6. 최근 컨센서스 전망치 분석
1 2026-07-01 35000000000000
7. 전망
- 2026년 연간 영업이익 전망:
+ 상방: 4000000000000000원
+ 중간: 3500000000000000원
+ 하방: 3000000000000000원
"""

        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(1, result["stats"]["corrections_applied"])
        self.assertEqual(0.01, result["corrections"][0]["factor"])
        self.assertIn("+ 중간: 35000000000000원", result["corrected_text"])
        self.assertIn("+ 상방: 40000000000000원", result["corrected_text"])
        self.assertFalse(result["needs_manual_review"])

    def test_keeps_text_without_consensus(self) -> None:
        text = """- 2026년 연간 영업이익 전망:
+ 상방: 40조원
+ 중간: 35조원
+ 하방: 30조원
"""

        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(text, result["corrected_text"])
        self.assertTrue(result["needs_manual_review"])
        self.assertEqual(0, result["stats"]["corrections_applied"])

    def test_extracts_latest_consensus_from_markdown_table_by_date(self) -> None:
        text = """## 6. 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---|
| 2026-06-30 | 35,000,000,000,000 |
| 2026-05-31 | 34,000,000,000,000 |
"""
        self.assertEqual(35_000_000_000_000, latest_consensus_won(text))
        self.assertEqual("2026-06-30", extract_consensus_blocks(text)[0]["latest"]["date"])

    def test_extracts_latest_prose_trillion_consensus_by_month(self) -> None:
        text = """5. 최근 컨센서스 전망치 분석
- 컨센서스 데이터: 2025년 7월 40.8조원에서 2026년 1월 86.4조원으로 2026년 영업이익 전망치가 상향됨.
- 현재 컨센서스 86.4조원은 상방 리스크가 남아 있다.

7. 2026년 연간 영업이익 전망 (시나리오 분석)
- 2026년 영업이익 전망:
  + 상방: 105000000000000
  + 중간: 88000000000000
  + 하방: 65000000000000
"""
        self.assertEqual(86.4 * 1_0000_0000_0000, latest_consensus_won(text))
        result = correct_operating_profit_forecast_scale(text)
        self.assertEqual(0, result["stats"]["corrections_applied"])
        self.assertFalse(result["needs_manual_review"])
        self.assertEqual(text, result["corrected_text"])

    def test_prose_consensus_still_repairs_thousandx_scenario_cells(self) -> None:
        text = """5. 최근 컨센서스 전망치 분석
- 2026년 1월 86.4조원으로 2026년 영업이익 전망치가 상향됨.

- 2026년 영업이익 전망:
  + 상방: 105000000000000000
  + 중간: 88000000000000000
  + 하방: 65000000000000000
"""
        result = correct_operating_profit_forecast_scale(text)
        self.assertEqual(1, result["stats"]["corrections_applied"])
        self.assertEqual(0.001, result["corrections"][0]["factor"])
        self.assertIn("+ 중간: 88000000000000", result["corrected_text"])
        self.assertFalse(result["needs_manual_review"])

    def test_skhynix_eval_report_reads_prose_consensus_without_shifting_scale(self) -> None:
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "eval_results" / "skhynix_20260106.md"
        if not path.is_file():
            self.skipTest("optional local eval fixture is not shipped with the public API package")
        text = path.read_text(encoding="utf-8")
        result = correct_operating_profit_forecast_scale(text)
        self.assertEqual(86.4 * 1_0000_0000_0000, result["consensus_won"])
        self.assertEqual(0, result["stats"]["corrections_applied"])
        self.assertEqual(text, result["corrected_text"])
        self.assertFalse(result["needs_manual_review"])

    def test_markdown_table_and_bold_dash_scenarios_are_corrected(self) -> None:
        text = """## 6. 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---|
| 2026-06-30 | 35,000,000,000,000 |

## 7. 2026년 연간 영업이익 전망 (시나리오 분석)
**2026년 영업이익 전망:**
- **상방:** 4,000,000,000,000,000원  
- **중간:** 3,500,000,000,000,000원  
- **하방:** 3,000,000,000,000,000원  
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(1, result["stats"]["corrections_applied"])
        self.assertIn("- **중간:** 35,000,000,000,000원  ", result["corrected_text"])
        self.assertEqual("2026-06-30", result["corrections"][0]["consensus_date"])

    def test_dataframe_scientific_notation_and_section_five_are_supported(self) -> None:
        text = """5. 최근 컨센서스 전망치 분석
-           Date        Amount
231 2026-05-29  3.400000e+13
245 2026-06-19  3.500000e+13

6. 2027년 연간 영업이익 전망 (시나리오 분석)
- 2027년 영업이익 전망: 3500000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(35_000_000_000_000, result["consensus_won"])
        self.assertIn("- 2027년 영업이익 전망: 35000000000000원", result["corrected_text"])

    def test_multi_column_table_uses_operating_profit_not_revenue(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | 매출액 | 영업이익 |
|---|---:|---:|
| 2026-06-30 | 350,000,000,000,000 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 35000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(35_000_000_000_000, result["consensus_won"])
        self.assertEqual(text, result["corrected_text"])

    def test_consensus_is_not_reused_for_a_different_forecast_year(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 35000000000000원

## 2027년 연간 영업이익 전망
- 2027년 영업이익 전망: 350000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("2027년 영업이익 전망: 350000000000000원", result["corrected_text"])
        self.assertTrue(any(item["year"] == "2027" for item in result["review_items"]))

    def test_explicit_consensus_heading_year_beats_next_forecast_year(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2027년 연간 영업이익 전망
- 2027년 영업이익 전망: 350000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(text, result["corrected_text"])
        self.assertEqual("2026", result["consensus_extraction"]["blocks"][0]["forecast_year"])
        self.assertEqual("heading", result["consensus_extraction"]["blocks"][0]["year_source"])
        self.assertTrue(any(item.get("year") == "2027" for item in result["review_items"]))

    def test_dual_won_and_eokwon_columns_are_cross_checked(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) | Amount (억원) |
|---|---:|---:|
| 2026-06-30 | 35,000,000,000,000 | 350,000 |

## 2026년 연간 영업이익 전망
- **2026년 영업이익 전망**: 3500000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(35_000_000_000_000, result["consensus_won"])
        self.assertIn("**2026년 영업이익 전망**: 35000000000000원", result["corrected_text"])
        self.assertEqual([], result["review_items"])

    def test_inconsistent_dual_consensus_units_require_review(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) | Amount (억원) |
|---|---:|---:|
| 2026-06-30 | 35,000,000,000,000 | 3,500 |

## 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 35000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIsNone(result["consensus_won"])
        self.assertTrue(result["needs_manual_review"])
        self.assertTrue(any("환산값" in item["reason"] for item in result["review_items"]))

    def test_mixed_scale_scenario_block_corrects_only_erroneous_cells(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- **상방:** 400000000000000원
- **중간:** 350000000000000원
- **하방:** 30000000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("상방:** 40000000000000원", result["corrected_text"])
        self.assertIn("중간:** 35000000000000원", result["corrected_text"])
        self.assertIn("하방:** 30000000000000원", result["corrected_text"])
        self.assertEqual({"상방", "중간"}, {item["label"] for item in result["corrections"][0]["changes"]})

    def test_negative_operating_loss_consensus_is_scale_corrected(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | 영업이익 |
|---|---:|
| 2026-06-30 | -40,000,000,000 |

## 2026년 연간 영업이익 전망
- **상방:** -300000000000원
- **중간:** -400000000000원
- **하방:** -500000000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(-40_000_000_000, result["consensus_won"])
        self.assertIn("- **중간:** -40000000000원", result["corrected_text"])
        self.assertEqual(0.1, result["corrections"][0]["factor"])

    def test_large_annual_gap_applies_nearest_power_of_ten(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- **2026년 영업이익 전망**: 200조원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("20조원", result["corrected_text"])
        self.assertEqual(0.1, result["corrections"][0]["factor"])
        self.assertFalse(any("25%" in item["reason"] for item in result["review_items"]))

    def test_annual_scale_also_scales_consistent_half_year_lines(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-18 | 16,500,000,000 |

## 2026년 연간 영업이익 전망
- 2026년 상반기 전망: 1160000000원
- 2026년 하반기 전망: 700000000원
- 2026년 영업이익 전망: 1860000000원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("- 2026년 상반기 전망: 11600000000원", result["corrected_text"])
        self.assertIn("- 2026년 하반기 전망: 7000000000원", result["corrected_text"])
        self.assertIn("- 2026년 영업이익 전망: 18600000000원", result["corrected_text"])
        labels = {item["label"] for item in result["corrections"][0]["changes"]}
        self.assertEqual({"상반기", "하반기", "연간"}, labels)
        self.assertEqual(10.0, result["corrections"][0]["factor"])

    def test_three_times_annual_gap_is_sent_to_review(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 40,000,000,000 |

## 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 1200억원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(text, result["corrected_text"])
        self.assertTrue(result["needs_manual_review"])
        self.assertAlmostEqual(3.0, result["review_items"][0]["consensus_ratio"])

    def test_consensus_anchored_half_year_sum_repairs_shifted_halves(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 40,000,000,000,000 |

## 2026년 상반기 영업이익 전망
+ 상방: 200조원
+ 중간: 150조원
+ 하방: 100조원

## 2026년 하반기 영업이익 전망
+ 상방: 300조원
+ 중간: 250조원
+ 하방: 200조원

## 2026년 연간 영업이익 전망
+ 상방: 50조원
+ 중간: 40조원
+ 하방: 30조원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("+ 중간: 15조원", result["corrected_text"])
        self.assertIn("+ 중간: 25조원", result["corrected_text"])
        self.assertEqual({"H1", "H2"}, {item.get("period") for item in result["corrections"]})
        self.assertFalse(result["needs_manual_review"])

    def test_half_year_sum_without_consensus_stays_manual(self) -> None:
        text = """## 2026년 상반기 영업이익 전망
+ 상방: 20조원
+ 중간: 15조원
+ 하방: 10조원

## 2026년 하반기 영업이익 전망
+ 상방: 30조원
+ 중간: 25조원
+ 하방: 20조원

## 2026년 연간 영업이익 전망
+ 상방: 500조원
+ 중간: 400조원
+ 하방: 300조원
"""
        result = correct_operating_profit_forecast_scale(text)

        self.assertEqual(text, result["corrected_text"])
        self.assertTrue(result["needs_manual_review"])
        self.assertTrue(any("어느 블록" in item["reason"] for item in result["review_items"]))

    def test_crlf_and_non_numeric_markdown_are_preserved(self) -> None:
        text = (
            "6. 최근 컨센서스 전망치 분석\r\n"
            "1 2026-06-30 35000000000000\r\n"
            "7. 2026년 연간 영업이익 전망:\r\n"
            "+ 상방: 4000000000000000원 # optimistic\r\n"
            "+ 중간: 3500000000000000원 # base\r\n"
            "+ 하방: 3000000000000000원 # downside\r\n"
        )
        result = correct_operating_profit_forecast_scale(text)

        self.assertIn("\r\n", result["corrected_text"])
        self.assertIn("# base\r\n", result["corrected_text"])
        self.assertNotIn("\n", result["corrected_text"].replace("\r\n", ""))

    def test_api_rejects_blank_text(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(api_operating_profit_scale_correction(
                ForecastScaleCorrectionRequest(markdown_text="   ")
            ))
        self.assertEqual(400, raised.exception.status_code)

    def test_api_rejects_zero_consensus(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(api_operating_profit_scale_correction(
                ForecastScaleCorrectionRequest(markdown_text="보고서", consensus_won=0)
            ))
        self.assertEqual(400, raised.exception.status_code)

    def test_api_rejects_non_finite_consensus(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(api_operating_profit_scale_correction(
                ForecastScaleCorrectionRequest(markdown_text="보고서", consensus_won=float("nan"))
            ))
        self.assertEqual(400, raised.exception.status_code)

    def test_correction_request_defaults_to_llm_graph(self) -> None:
        request = ForecastCorrectRequest(markdown_text="보고서")
        self.assertEqual("llm", request.graph_mode)
        self.assertEqual(10, request.max_factreasoner_candidates)

    def test_integrated_preview_builds_graph_from_scaled_text(self) -> None:
        text = """- 2026년 연간 영업이익 전망:
+ 상방: 4000000000000000원
+ 중간: 3500000000000000원
+ 하방: 3000000000000000원
"""

        response = asyncio.run(api_operating_profit_correction_preview(
            ForecastCorrectionPreviewRequest(
                markdown_text=text,
                consensus_won=35000000000000,
                graph_mode="fast",
            )
        ))
        payload = __import__("json").loads(response.body)

        self.assertEqual(1, payload["scale_correction"]["stats"]["corrections_applied"])
        self.assertEqual(
            payload["scale_correction"]["corrected_text"],
            payload["fact_graph"]["original_text_plain"],
        )
        self.assertIn("judge_atom", payload["next_steps"])

    def test_correct_endpoint_returns_markdown_after_scale_correction(self) -> None:
        text = """- 2026년 연간 영업이익 전망:
+ 상방: 4000000000000000원
+ 중간: 3500000000000000원
+ 하방: 3000000000000000원
"""
        empty_graph = {"fact_atom_graph": {"nodes": [], "edges": [], "online": False, "error": None}}
        with (
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(empty_graph, {"hit": False})),
            ),
            patch(
                "web_app.main.review_llm_corrected_markdown",
                new=AsyncMock(return_value={"approve": True, "online": False, "error": None, "reason": "test"}),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(
                    markdown_text=text,
                    consensus_won=35000000000000,
                    graph_mode="fast",
                )
            ))
        payload = __import__("json").loads(response.body)

        self.assertIn("+ 중간: 35000000000000원", payload["corrected_markdown"])
        self.assertEqual(1, payload["stats"]["scale_corrections_applied"])
        self.assertIn("applied_corrections", payload["factreasoner"])

    def test_correct_endpoint_extracts_consensus_from_markdown(self) -> None:
        text = """## 6. 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---|
| 2026-06-30 | 35,000,000,000,000 |

## 7. 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 3500000000000000원
"""
        empty_graph = {"fact_atom_graph": {"nodes": [], "edges": [], "online": False, "error": None}}
        with (
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(empty_graph, {"hit": False})),
            ),
            patch(
                "web_app.main.review_llm_corrected_markdown",
                new=AsyncMock(return_value={"approve": True, "online": False, "error": None, "reason": "test"}),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(markdown_text=text, graph_mode="fast")
            ))
        payload = __import__("json").loads(response.body)

        self.assertIn("35000000000000원", payload["corrected_markdown"])
        self.assertEqual("document", payload["scale_correction"]["consensus_source"])

    def test_correct_endpoint_repairs_operating_profit_restatement(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- **상방:** 4000000000000000원
- **중간:** 3500000000000000원
- **하방:** 3000000000000000원

## 결론
2026년 중간 영업이익 전망은 3,500조원으로 판단한다.
"""
        empty_graph = {"fact_atom_graph": {"nodes": [], "edges": [], "online": False, "error": None}}
        with (
            patch("web_app.main.propagate_correction", new=AsyncMock()) as propagate,
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(empty_graph, {"hit": False})),
            ),
            patch(
                "web_app.main.review_llm_corrected_markdown",
                new=AsyncMock(return_value={"approve": True, "online": False, "error": None, "reason": "test"}),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(markdown_text=text, graph_mode="fast")
            ))
        payload = __import__("json").loads(response.body)

        propagate.assert_not_awaited()
        self.assertIn("중간 영업이익 전망은 35조원", payload["corrected_markdown"])
        self.assertEqual(1, payload["stats"]["literal_ripple_corrections_applied"])

    def test_literal_ripple_does_not_touch_other_metric_year_or_code_fence(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 전망
- 2026년 영업이익 전망: 3500조원

2026년 매출 전망은 3,500조원이다.
2027년 영업이익 전망은 3,500조원이다.
```text
2026년 영업이익 전망은 3,500조원이다.
```
"""
        empty_graph = {"fact_atom_graph": {"nodes": [], "edges": [], "online": False, "error": None}}
        with (
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(empty_graph, {"hit": False})),
            ),
            patch(
                "web_app.main.review_llm_corrected_markdown",
                new=AsyncMock(return_value={"approve": True, "online": False, "error": None, "reason": "test"}),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(markdown_text=text, graph_mode="fast")
            ))
        corrected = __import__("json").loads(response.body)["corrected_markdown"]

        self.assertIn("2026년 매출 전망은 3,500조원", corrected)
        self.assertIn("2027년 영업이익 전망은 3,500조원", corrected)
        self.assertIn("```text\n2026년 영업이익 전망은 3,500조원", corrected)

    def test_llm_mode_runs_scale_pin_cascade_and_applies_bounded_rewrite(self) -> None:
        text = """## 2026년 연간 영업이익 전망
- 2026년 영업이익 전망: 3500조원

## 요약
- 핵심 수치는 3500조원이다.
"""
        graph_result = {
            "fact_atom_graph": {
                "mode": "llm",
                "online": True,
                "error": None,
                "nodes": [{
                    "id": "op1",
                    "properties": {
                        "statement": "2026년 영업이익은 3500조원이다.",
                        "source_quote": "2026년 영업이익 전망: 3500조원",
                        "metric": "영업이익",
                        "value": "3500조원",
                        "period": "2026년",
                        "suspect": False,
                    },
                }],
                "edges": [],
            },
        }
        cascade = {
            "online": True,
            "error": None,
            "propagations": [{
                "chunk_id": "chunk_summary",
                "direction": "downstream",
                "original_text": "- 핵심 수치는 3500조원이다.",
                "suggested_text": "- 핵심 수치는 35조원이다.",
                "affected": True,
                "needs_manual": False,
            }],
        }
        with (
            patch("web_app.main.run_fact_graph_preview", return_value=graph_result),
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(graph_result, {"hit": False})),
            ),
            patch("web_app.main.propagate_correction", new=AsyncMock(return_value=cascade)) as propagate,
            patch(
                "web_app.main.review_llm_corrected_markdown",
                new=AsyncMock(return_value={"approve": True, "online": False, "error": None, "reason": "test"}),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(
                    markdown_text=text,
                    consensus_won=35_000_000_000_000,
                    graph_mode="llm",
                )
            ))
        payload = __import__("json").loads(response.body)

        propagate.assert_awaited_once()
        self.assertIn("핵심 수치는 35조원", payload["corrected_markdown"])
        self.assertEqual(1, payload["stats"]["factreasoner_cascades_run"])

    def test_cascade_does_not_apply_unverified_new_calculation(self) -> None:
        intervention = {
            "mapping_id": "scale_001",
            "node_id": "op1",
            "original_statement": "2026년 영업이익은 3500조원이다.",
            "corrected_statement": "2026년 영업이익은 35조원이다.",
        }
        cascade = {
            "error": None,
            "propagations": [{
                "chunk_id": "c2",
                "direction": "downstream",
                "original_text": "- 핵심 수치는 3500조원이다.",
                "suggested_text": "- 재계산한 핵심 수치는 40조원이다.",
                "affected": True,
                "needs_manual": False,
                "reason": "새 계산",
            }],
        }
        with patch("web_app.main.propagate_correction", new=AsyncMock(return_value=cascade)):
            corrected, traces, applied, review = asyncio.run(
                _apply_scale_factreasoner_cascades(
                    "- 핵심 수치는 3500조원이다.",
                    original_markdown="- 핵심 수치는 3500조원이다.",
                    graph={"nodes": [{"id": "op1", "properties": {}}], "edges": []},
                    interventions=[intervention],
                    enable_cascade=True,
                )
            )

        self.assertEqual("- 핵심 수치는 3500조원이다.", corrected)
        self.assertEqual([], applied)
        self.assertEqual(1, len(traces))
        self.assertIn("새 계산", review[0]["reason"])

    def test_scale_pin_target_repairs_restatement_without_metric_word(self) -> None:
        intervention = {
            "mapping_id": "scale_001",
            "node_id": "op1",
            "original_statement": "2026년 영업이익은 3500조원이다.",
            "corrected_statement": "2026년 영업이익은 35조원이다.",
        }
        cascade = {
            "error": None,
            "target": {
                "chunk_id": "c2",
                "original_chunk_text": "- 중간 전망은 3500조원이다.",
                "suggested_quote": "- 중간 전망은 35조원이다.",
            },
            "propagations": [],
        }
        with patch("web_app.main.propagate_correction", new=AsyncMock(return_value=cascade)):
            corrected, _, applied, review = asyncio.run(
                _apply_scale_factreasoner_cascades(
                    "- 중간 전망은 3500조원이다.",
                    original_markdown="- 중간 전망은 3500조원이다.",
                    graph={"nodes": [{"id": "op1", "properties": {}}], "edges": []},
                    interventions=[intervention],
                    enable_cascade=True,
                )
            )

        self.assertEqual("- 중간 전망은 35조원이다.", corrected)
        self.assertEqual("scale_pin_target", applied[0]["kind"])
        self.assertEqual([], review)

    def test_factreasoner_does_not_apply_ambiguous_duplicate_quote(self) -> None:
        graph = {
            "nodes": [{
                "id": "a1",
                "properties": {"suspect": True, "statement": "2026년 영업이익은 350조원이다."},
            }],
            "edges": [],
        }
        judgment = {
            "changed": True,
            "original_quote_text": "영업이익은 350조원이다.",
            "suggested_quote": "영업이익은 35조원이다.",
        }
        with patch("web_app.main.judge_atom", new=AsyncMock(return_value=judgment)):
            corrected, applied, review = asyncio.run(_apply_factreasoner_markdown_corrections(
                "영업이익은 350조원이다.\n영업이익은 350조원이다.", graph=graph, max_candidates=3,
            ))

        self.assertEqual("영업이익은 350조원이다.\n영업이익은 350조원이다.", corrected)
        self.assertEqual([], applied)
        self.assertIn("count=2", review[0]["reason"])

    def test_factreasoner_preserves_markdown_structure(self) -> None:
        graph = {
            "nodes": [{
                "id": "a1",
                "properties": {"suspect": True, "statement": "2026년 영업이익은 350조원이다."},
            }],
            "edges": [],
        }
        judgment = {
            "changed": True,
            "original_quote_text": "- 영업이익은 350조원이다.",
            "suggested_quote": "영업이익은 35조원이다.",
        }
        with patch("web_app.main.judge_atom", new=AsyncMock(return_value=judgment)):
            corrected, applied, review = asyncio.run(_apply_factreasoner_markdown_corrections(
                "- 영업이익은 350조원이다.", graph=graph, max_candidates=3,
            ))

        self.assertEqual("- 영업이익은 350조원이다.", corrected)
        self.assertEqual([], applied)
        self.assertIn("구조", review[0]["reason"])

    def test_half_year_identity_accepts_signed_sum(self) -> None:
        text = """- 2026년 상반기 전망: -406억원
- 2026년 하반기 전망: 610억원
- 2026년 영업이익 전망: 204억원
"""
        report = evaluate_half_year_identity(text)["2026"]
        self.assertTrue(report["identity_ok"])
        self.assertAlmostEqual(-406 * 1_0000_0000, report["h1"])
        self.assertAlmostEqual(610 * 1_0000_0000, report["h2"])
        self.assertAlmostEqual(204 * 1_0000_0000, report["annual"])

    def test_scale_cluster_unlocks_when_half_year_sum_breaks(self) -> None:
        text = """- 2026년 상반기 전망: 10억원
- 2026년 하반기 전망: 20억원
- 2026년 영업이익 전망: 200억원
"""
        guard = scale_cluster_is_locked(
            text,
            [{"kind": "scale_correction", "year": "2026", "residual": 0.1}],
        )
        self.assertFalse(guard["lock"])
        self.assertTrue(guard["manual_review"])

    def test_scale_correction_api_keeps_proposal_when_rereview_rejects(self) -> None:
        text = """## 2026년 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-18 | 16,500,000,000 |

## 2026년 연간 영업이익 전망
- 2026년 상반기 전망: 1160000000원
- 2026년 하반기 전망: 700000000원
- 2026년 영업이익 전망: 1860000000원
"""
        reject = {
            "approve": False,
            "online": True,
            "error": None,
            "reason": "-406+610이 204와 모순이다.",
            "model": "test",
        }
        with patch("web_app.main.review_llm_corrected_markdown", new=AsyncMock(return_value=reject)):
            response = asyncio.run(api_operating_profit_scale_correction(
                ForecastScaleCorrectionRequest(markdown_text=text)
            ))
        payload = __import__("json").loads(response.body)

        self.assertIn("- 2026년 영업이익 전망: 18600000000원", payload["corrected_text"])
        self.assertEqual(1, payload["stats"]["corrections_applied"])
        self.assertGreaterEqual(len(payload["corrections"]), 1)
        self.assertFalse(payload["rereview"]["approve"])
        self.assertTrue(payload["rereview"]["advisory"])
        self.assertTrue(payload["arithmetic_guard"]["lock"])
        self.assertTrue(payload["needs_manual_review"])

    def test_correct_keeps_scale_and_ripple_when_rereview_rejects(self) -> None:
        text = """## 최근 컨센서스 전망치 분석
| Date | Amount (원) |
|---|---:|
| 2026-06-30 | 35,000,000,000,000 |

## 2026년 연간 영업이익 전망
- **상방:** 4000000000000000원
- **중간:** 3500000000000000원
- **하방:** 3000000000000000원

## 결론
2026년 중간 영업이익 전망은 3,500조원으로 판단한다.
"""
        reject = {
            "approve": False,
            "online": True,
            "error": None,
            "reason": "이미 원 단위다.",
            "model": "test",
        }
        empty_graph = {"fact_atom_graph": {"nodes": [], "edges": [], "online": False, "error": None}}
        with (
            patch("web_app.main.review_llm_corrected_markdown", new=AsyncMock(return_value=reject)),
            patch("web_app.main.propagate_correction", new=AsyncMock()),
            patch(
                "web_app.main._get_factreasoner_graph",
                new=AsyncMock(return_value=(empty_graph, {"hit": False})),
            ),
        ):
            response = asyncio.run(api_operating_profit_correct(
                ForecastCorrectRequest(markdown_text=text, graph_mode="fast")
            ))
        payload = __import__("json").loads(response.body)

        self.assertIn("중간 영업이익 전망은 35조원", payload["corrected_markdown"])
        self.assertIn("35000000000000원", payload["corrected_markdown"])
        self.assertEqual(1, payload["stats"]["literal_ripple_corrections_applied"])
        self.assertEqual(0, payload["stats"]["rereview_reverted"])
        self.assertTrue(payload["factreasoner"]["rereview"]["advisory_reject"])
        self.assertTrue(payload["stats"]["manual_review_required"])


if __name__ == "__main__":
    unittest.main()
