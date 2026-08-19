"""Fact-atom graph preview used by the forecast correction pipeline."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import re

from web_app.pipeline.fact_graph import extract_fact_atom_graph


def _date_from_path(path: Path) -> str:
    for part in path.parts:
        if re.fullmatch(r"20\d{6}", part):
            return part
    return "unknown_date"


def _slug(value: str) -> str:
    aliases = {"SK하이닉스": "skhynix", "삼성전자": "samsung"}
    if value in aliases:
        return aliases[value]
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", value).strip("_").lower()
    return slug or "report"


def _openkb_fact_summary(checks: list[dict[str, Any]]) -> dict[str, int]:
    evidence_items = [
        item for check in checks for item in check.get("evidence", [])
    ]
    per_kb_items = [
        item for check in checks for item in check.get("per_kb", [])
    ]
    mismatch_checks = 0
    for check in checks:
        if any(
            (item.get("metadata", {}).get("field_mismatches") or [])
            for item in check.get("evidence", [])
        ):
            mismatch_checks += 1
    with_evidence = sum(1 for item in checks if item.get("evidence"))
    return {
        "total": len(checks),
        "supported": sum(1 for item in checks if item.get("verdict") == "Supported"),
        "contradict": sum(1 for item in checks if item.get("verdict") == "Contradict"),
        "weak": sum(1 for item in checks if item.get("verdict") == "Weak"),
        "errors": sum(1 for item in checks if item.get("verdict") == "Error"),
        "kb_errors": sum(1 for item in per_kb_items if item.get("verdict") == "Error"),
        "with_evidence": with_evidence,
        "without_evidence": len(checks) - with_evidence,
        "evidence": len(evidence_items),
        "field_matches": sum(
            len(item.get("metadata", {}).get("field_matches") or [])
            for item in evidence_items
        ),
        "field_mismatches": sum(
            len(item.get("metadata", {}).get("field_mismatches") or [])
            for item in evidence_items
        ),
        "mismatch_checks": mismatch_checks,
    }


def run_fact_graph_preview(
    *,
    markdown_text: str,
    markdown_path: Path | None = None,
    company: str | None = None,
    date_label: str | None = None,
    mode: str = "fast",
    nli_mode: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return the Fact Atom Graph subset used by forecast correction."""
    source_path = markdown_path or Path("<request-body>")
    resolved_company = company or (markdown_path.stem if markdown_path else "report")
    resolved_date = date_label or (
        _date_from_path(markdown_path) if markdown_path else "api"
    )
    findings: list[Any] = []
    finding_dicts: list[dict[str, Any]] = []
    fact_atom_graph = extract_fact_atom_graph(
        markdown_text=markdown_text,
        seed_facts=[],
        llm_only=True,
        nli_mode=nli_mode or ("fast" if mode == "fast" else "all_pairs"),
        progress_callback=progress_callback,
    )
    return {
        "preview": True,
        "company": resolved_company,
        "date_label": resolved_date,
        "stats": {
            "findings_total": len(finding_dicts),
            "issues_confirmed": 0,
            "fixes_applied": 0,
            "conclusion_updates_applied": 0,
            "external_evidence_needed": 0,
        },
        "findings": finding_dicts,
        "fixes": [],
        "fact_atom_graph": fact_atom_graph,
        "conclusion_impacts": [],
        "conclusion_impact_graph": {"nodes": [], "edges": []},
        "semantic_impact_analysis": {
            "enabled": False,
            "disabled_reason": "llm_fact_graph_preview",
        },
        "openkb_fact_checks": {
            "enabled": False,
            "disabled_reason": "llm_fact_graph_preview",
            "kb_ids": [],
            "checks": [],
            "summary": _openkb_fact_summary([]),
        },
        "original_text_plain": markdown_text,
        "corrected_text_plain": markdown_text,
        "corrected_text_html": "",
        "output_slug": f"{_slug(resolved_company)}_{resolved_date}_fact_graph_preview.html",
        "markdown_path": str(source_path),
    }
