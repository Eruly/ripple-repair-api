import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from web_app.pipeline.llm_json import (
    StructuredJSONError,
    parse_object,
    reset_json_schema_support,
    response_format_for,
    structured_response_format,
    mark_json_schema_unsupported,
)
from web_app.pipeline.fact_graph_correct import (
    _build_rereview_comparisons,
    _batch_items,
    _fact_atom_tree_batches,
    batch_correct_atoms,
    batch_judge_atoms,
    review_llm_corrected_markdown,
)


class StructuredLlmResponseContractTest(unittest.TestCase):
    def test_parses_fence_preamble_and_braces_inside_strings(self) -> None:
        raw = '검토 결과입니다.\n```json\n{"approve":true,"reason":"문장에 {중괄호}가 있어도 유지"}\n```'
        self.assertEqual(parse_object(raw, kind="approval")["approve"], True)

    def test_repairs_invalid_plain_backslash(self) -> None:
        raw = r'{"corrected_statement":"영업이익 49\% 증가","reason":"단위 확인"}'
        self.assertEqual(parse_object(raw, kind="atom_judgment")["corrected_statement"], "영업이익 49% 증가")

    def test_rejects_truncated_object(self) -> None:
        with self.assertRaises(StructuredJSONError):
            parse_object('{"approve": true, "reason": "잘림', kind="approval")

    def test_schema_payload_is_explicit_and_strict(self) -> None:
        payload = response_format_for("rewrite")
        self.assertEqual(payload["type"], "json_schema")
        self.assertTrue(payload["json_schema"]["strict"])
        self.assertEqual(payload["json_schema"]["schema"]["required"], ["suggested_text", "reason"])

    def test_json_schema_rejection_sticks_to_json_object(self) -> None:
        reset_json_schema_support()
        try:
            self.assertEqual("json_schema", structured_response_format("rewrite", attempt=0)["type"])
            self.assertEqual("json_object", structured_response_format("rewrite", attempt=1)["type"])
            mark_json_schema_unsupported()
            self.assertEqual("json_object", structured_response_format("rewrite", attempt=0)["type"])
        finally:
            reset_json_schema_support()

    def test_rereview_payload_contains_only_changed_spans(self) -> None:
        original = "앞 문맥 " * 100 + "영업이익 350조원이다." + " 뒤 문맥" * 100
        corrected = original.replace("350조원", "35조원")
        comparisons = _build_rereview_comparisons(
            original_markdown=original,
            corrected_markdown=corrected,
            corrections=[{
                "kind": "atom_judgment",
                "node_id": "a1",
                "original": "영업이익 350조원이다.",
                "corrected": "영업이익 35조원이다.",
            }],
            fact_judgments=[{"node_id": "a1", "text": "영업이익 350조원이다.", "reason": "자릿수"}],
        )
        self.assertEqual(len(comparisons), 1)
        self.assertNotIn(original, str(comparisons[0]))
        self.assertEqual(comparisons[0]["original_text"], "영업이익 350조원이다.")
        self.assertEqual(comparisons[0]["corrected_text"], "영업이익 35조원이다.")

    def test_noop_rereview_is_skipped_without_an_llm_call(self) -> None:
        result = __import__("asyncio").run(
            review_llm_corrected_markdown(
                original_markdown="긴 문서" * 10000,
                corrected_markdown="긴 문서" * 10000,
                corrections=[],
            )
        )
        self.assertTrue(result["approve"])
        self.assertTrue(result["skipped"])

    def test_fact_judgment_and_correction_use_bounded_batch_turns(self) -> None:
        nodes = [
            {
                "id": f"a{i}",
                "properties": {
                    "statement": f"영업이익 {350 + i}조원이다.",
                    "source_quote": f"- 영업이익 {350 + i}조원이다.",
                    "chunk_id": f"c{i}",
                    "section": "forecast",
                },
            }
            for i in range(9)
        ]
        calls = []
        edges = [
            {"source": f"a{i}", "target": f"a{i + 1}", "relation": "same_metric"}
            for i in range(4)
        ] + [
            {"source": f"a{i}", "target": f"a{i + 1}", "relation": "same_metric"}
            for i in range(5, 8)
        ]

        async def fake_post(*args, **kwargs):
            calls.append(kwargs["schema_kind"])
            items = kwargs["prompt"]["atoms"]
            if kwargs["schema_kind"] == "atom_judgments":
                return {"judgments": [
                    {"id": item["id"], "verdict": "correct", "reason": "내부 숫자 근거"}
                    for item in items
                ]}
            return {"corrections": [
                {
                    "id": item["id"],
                    "corrected_statement": item["statement"].replace("350", "35", 1),
                    "suggested_quote": item["source_quote"].replace("350", "35", 1),
                    "reason": "내부 숫자 근거",
                }
                for item in items
            ]}

        async def run() -> None:
            with patch("web_app.pipeline.fact_graph_correct._post_json", new=AsyncMock(side_effect=fake_post)):
                judged = await batch_judge_atoms(
                    nodes=nodes, edges=edges, markdown_text="\n".join(
                        item["properties"]["source_quote"] for item in nodes
                    ), candidates=nodes,
                )
                corrected = await batch_correct_atoms(
                    nodes=nodes, edges=edges, markdown_text="\n".join(
                        item["properties"]["source_quote"] for item in nodes
                    ), candidates=nodes, judgments=judged["judgments"],
                )
            self.assertEqual(2, judged["batches"])
            self.assertEqual(2, corrected["batches"])
            self.assertEqual(4, len(calls))
            self.assertEqual(9, len(judged["judgments"]))
            self.assertEqual(9, len(corrected["corrections"]))

        asyncio.run(run())

    def test_tree_batches_keep_disconnected_components_separate(self) -> None:
        values = [{"id": f"a{i}", "properties": {}} for i in range(6)]
        batches = _fact_atom_tree_batches(
            values,
            edges=[
                {"source": "a0", "target": "a1"},
                {"source": "a1", "target": "a2"},
                {"source": "a3", "target": "a4"},
            ],
            size=2,
        )
        self.assertEqual([["a0", "a1"], ["a2"], ["a3", "a4"], ["a5"]], [
            [item["id"] for item in batch] for batch in batches
        ])

    def test_large_tree_is_partitioned_at_128_atoms(self) -> None:
        values = [{"id": f"a{i}", "properties": {}} for i in range(260)]
        edges = [{"source": f"a{i}", "target": f"a{i + 1}"} for i in range(259)]
        batches = _batch_items(values, edges=edges)
        self.assertEqual([128, 128, 4], [len(batch) for batch in batches])

    def test_partition_adds_one_parent_and_child_as_reference_context(self) -> None:
        candidates = [{"id": f"a{i}", "properties": {}} for i in range(4)]
        all_nodes = [*candidates, {"id": "p", "properties": {}}, {"id": "c", "properties": {}}]
        edges = [
            {"source": "p", "target": "a0", "relation": "supports"},
            {"source": "a0", "target": "a1", "relation": "supports"},
            {"source": "a1", "target": "a2", "relation": "supports"},
            {"source": "a2", "target": "a3", "relation": "supports"},
            {"source": "a3", "target": "c", "relation": "supports"},
        ]
        batches = _fact_atom_tree_batches(candidates, edges, size=2, context_values=all_nodes)
        self.assertEqual(
            [["p", "a0", "a1", "a2"], ["a1", "a2", "a3", "c"]],
            [[item["id"] for item in batch] for batch in batches],
        )
        self.assertTrue(batches[0][0]["is_boundary_context"])
        self.assertEqual("parent", batches[0][0]["boundary_role"])
        self.assertTrue(batches[0][-1]["is_boundary_context"])
        self.assertEqual("child", batches[0][-1]["boundary_role"])
        self.assertFalse(batches[0][0]["correction_allowed"])
        self.assertFalse(batches[0][-1]["correction_allowed"])


if __name__ == "__main__":
    unittest.main()
