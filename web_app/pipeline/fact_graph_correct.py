"""Propagate a fact-atom correction to upstream/downstream document chunks.

When the user corrects a single fact atom in the Interactive Atom Map, other
chunks of the report can become inconsistent: chunks that *influence* the
corrected atom (upstream) and chunks the corrected atom *influences*
(downstream). This module walks the fact-atom graph along causal edges in both
directions, re-derives the source text of every affected chunk, and asks an
OpenAI-compatible LLM to rewrite each affected chunk consistently with the
correction. When the LLM is offline the proposal is kept for manual review;
the automatic forecast path does not use a deterministic corrector fallback.

Direction semantics (matches the UI ``factAtomChunkInfluenceSummary``):
edge ``source`` influences edge ``target``. So for a target atom ``T``:

* downstream(T): follow edges out of T (T as source), transitively.
* upstream(T):   follow edges into T (T as target), transitively.

Only causal relations are traversed; structural relations (``same_metric``,
``same_period``) are ignored.
"""
from __future__ import annotations

from collections import deque
from typing import Any
import asyncio
import json
import re

import httpx

from hallucination_verifier.llm_config import (
    factreasoner_api_key,
    factreasoner_base_url,
    factreasoner_correction_model,
    factreasoner_judge_model,
    factreasoner_review_model,
    factreasoner_stop_sequences,
    with_local_chat_template,
)
from web_app.pipeline.fact_graph import (
    _AMOUNT_RE,
    _AMOUNT_UNIT_EOKWON,
    _amount_near_keyword,
    _document_chunks,
    _is_margin_atom,
    _parse_pct_value,
)
from web_app.pipeline.llm_json import (
    StructuredJSONError,
    format_instruction,
    is_json_schema_rejection,
    mark_json_schema_unsupported,
    parse_object,
    response_format_for,
    structured_response_format,
)

_CAUSAL_RELATIONS = {"supports", "contradicts", "depends_on", "causes", "derived_from"}
_LLM_TIMEOUT_SECONDS = 180.0
# High reasoning can consume part of the generation budget before the compact
# JSON answer. Keep the graph-level budgets unchanged, but give per-atom
# judgement and target rewrite enough room to close their JSON envelope.
_ATOM_JUDGEMENT_MAX_TOKENS = 4096
_TARGET_REWRITE_MAX_TOKENS = 4096
# Muse Glimmer's high reasoning can consume the compact JSON envelope budget;
# keep this aligned with the existing atom/rewrite calls so a valid approval
# is not mistaken for a failed review due to truncation.
_APPLIED_REREVIEW_MAX_TOKENS = 4096
_APPLIED_REREVIEW_TIMEOUT_SECONDS = 60.0
_MAX_AFFECTED_CHUNKS = 64
_MAX_REREVIEW_COMPARISONS = 32
_REREVIEW_CONTEXT_CHARS = 220
_VALUE_TOKEN_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|조원|억원|원|배|십억원|백만원|만원|조|억)?")
_VALUE_BOUNDARY_RE = re.compile(r"[0-9A-Za-z가-힣,._+\-/%]")
_TABLE_MARKERS = ("|", "DRAM_금액", "NAND_금액", "기간  DRAM")
_UNCERTAIN_CORRECTION_MARKERS = (
    "수동 검토", "확정할 수 없", "근거 부족", "명시적 수치가 없",
    "가능성", "추정", "후보", "불확실",
)
_EXPORT_ROW_RE = re.compile(
    r"^\s*\d+\s+(20\d{2}-\d{2})\s+([0-9,]+)\s+([0-9,]+)\s+([0-9,]+)\s+([0-9,]+)\s*$",
    re.MULTILINE,
)
_PERIOD_RE = re.compile(r"20\d{2}[-/]\d{2}")
_AMBIGUOUS_THOUSAND_USD_RE = re.compile(
    r"(?P<num>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<unit>"
    r"만\s*(?:달러)?(?:\s*\([^)]*천달러[^)]*\)|\s*천달러(?:\s*기준|\s*단위)?)?"
    r"|백만\s*(?:천달러|달러)"
    r"|억\s*(?:천달러|(?:달러|불)\s*(?:\([^)]*천달러[^)]*\)|(?=[^,\n)]{0,40}천달러\s*기준)))"
    r")"
)
_RAW_THOUSAND_USD_RE = re.compile(r"\b[1-9][0-9]{5,}\b")
_UNIT_VALUE_CONTEXT_RE = re.compile(r"(ASP\s*/\s*kg|단위가치|금액\s*/\s*중량)", re.IGNORECASE)
_UNRESOLVED_EXPORT_UNIT_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:만천달러|만\s*천달러|백만\s*천달러|억\s*천달러|만달러\s*\(천달러)"
)
_LLM_AFFIRMS_ORIGINAL_RE = re.compile(
    r"(교정\s*불필요|수정\s*불필요|"
    r"원문[^.\n]{0,100}(정확|일치|타당|맞|유지)|"
    r"수치[^.\n]{0,100}(일치|정확|타당)|"
    r"no correction is needed|"
    r"original statement[^.\n]{0,120}(accurate|correct|consistent)|"
    r"accurately reflects|values?[^.\n]{0,120}consistent)",
    re.IGNORECASE,
)


def _relation_value(edge: dict[str, Any]) -> str:
    return str(
        edge.get("type")
        or (edge.get("properties") or {}).get("relation")
        or edge.get("relation")
        or ""
    ).strip()


def _node_statement(node: dict[str, Any]) -> str:
    props = node.get("properties") or {}
    return str(props.get("statement") or props.get("entity_name") or node.get("id") or "").strip()


def _node_chunk_id(node: dict[str, Any]) -> str:
    return str((node.get("properties") or {}).get("chunk_id") or "").strip()


def _node_section(node: dict[str, Any]) -> str:
    return str((node.get("properties") or {}).get("section") or "").strip()


def _build_adjacency(edges: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (downstream_adj, upstream_adj) over causal edges.

    downstream_adj[src] -> {targets that src influences}
    upstream_adj[tgt]   -> {sources that influence tgt}
    """
    downstream: dict[str, set[str]] = {}
    upstream: dict[str, set[str]] = {}
    for edge in edges or []:
        if _relation_value(edge) not in _CAUSAL_RELATIONS:
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if not source or not target:
            continue
        downstream.setdefault(source, set()).add(target)
        upstream.setdefault(target, set()).add(source)
    return downstream, upstream


def _bfs(adjacency: dict[str, set[str]], start: str, max_depth: int | None) -> set[str]:
    """Return reachable node ids from start (excluding start)."""
    reached: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        current, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for nxt in adjacency.get(current, set()):
            if nxt in visited:
                continue
            visited.add(nxt)
            reached.add(nxt)
            queue.append((nxt, depth + 1))
    return reached


def collect_affected_chunks(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    target_node_id: str,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Compute target info + affected chunks grouped by direction.

    Returns ``{"target": node | None, "chunks": [chunk_info, ...]}`` where each
    chunk_info has ``chunk_id``, ``section``, ``direction`` and ``atoms``.
    """
    node_by_id = {str(node.get("id")): node for node in (nodes or []) if node.get("id")}
    target = node_by_id.get(str(target_node_id))
    if target is None:
        return {"target": None, "chunks": []}

    downstream_adj, upstream_adj = _build_adjacency(edges)
    downstream_ids = _bfs(downstream_adj, str(target_node_id), max_depth)
    upstream_ids = _bfs(upstream_adj, str(target_node_id), max_depth)

    target_chunk = _node_chunk_id(target)
    grouped: dict[str, dict[str, Any]] = {}
    for node_id in downstream_ids | upstream_ids:
        node = node_by_id.get(node_id)
        if node is None:
            continue
        chunk_id = _node_chunk_id(node)
        if not chunk_id or chunk_id == target_chunk:
            continue
        is_down = node_id in downstream_ids
        is_up = node_id in upstream_ids
        direction = "both" if (is_down and is_up) else ("downstream" if is_down else "upstream")
        bucket = grouped.setdefault(
            chunk_id,
            {"chunk_id": chunk_id, "section": _node_section(node), "directions": set(), "atoms": []},
        )
        bucket["directions"].add(direction)
        bucket["atoms"].append({"id": node_id, "statement": _node_statement(node)})

    chunks: list[dict[str, Any]] = []
    for chunk_id, bucket in grouped.items():
        directions = bucket["directions"]
        if {"downstream", "upstream"} <= directions or "both" in directions:
            direction = "both"
        elif "downstream" in directions:
            direction = "downstream"
        else:
            direction = "upstream"
        chunks.append({
            "chunk_id": chunk_id,
            "section": bucket["section"],
            "direction": direction,
            "atoms": bucket["atoms"],
            "affected_atom_ids": [atom["id"] for atom in bucket["atoms"]],
        })

    # Stable ordering: upstream first, then by chunk id.
    _order = {"upstream": 0, "both": 1, "downstream": 2}
    chunks.sort(key=lambda item: (_order.get(item["direction"], 9), item["chunk_id"]))
    return {"target": target, "chunks": chunks[:_MAX_AFFECTED_CHUNKS]}


def _value_tokens(text: str) -> list[str]:
    return [tok.strip() for tok in _VALUE_TOKEN_RE.findall(text or "") if tok.strip()]


def _replace_value_token_once(text: str, old: str, new: str) -> tuple[str, bool]:
    """Replace a value token only when it is not embedded in a larger unit."""
    if not old:
        return text, False
    old_has_unit = bool(re.search(r"[%가-힣]$", old.strip()))
    for match in re.finditer(re.escape(old), text):
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if before and _VALUE_BOUNDARY_RE.match(before):
            continue
        if after and _VALUE_BOUNDARY_RE.match(after) and not old_has_unit:
            continue
        return text[:match.start()] + new + text[match.end():], True
    return text, False


def _nonblank_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _contains_table_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in _TABLE_MARKERS)


def _format_int(value: int) -> str:
    return f"{value:,}"


def _format_thousand_usd_for_report(value: int, *, inside_parentheses: bool = False) -> str:
    """Render a thousand-USD raw value for prose, preserving auditability."""
    eok_usd = value / 100_000.0
    rendered = f"{eok_usd:,.1f}".rstrip("0").rstrip(".")
    if inside_parentheses:
        return f"약 {rendered}억 달러, 원자료 {_format_int(value)}천달러"
    return f"약 {rendered}억 달러(원자료 {_format_int(value)}천달러)"


def _span_inside_parentheses(text: str, start: int) -> bool:
    """True if start is currently inside an unmatched parenthesis pair."""
    return text[:start].count("(") > text[:start].count(")")


def _flatten_nested_export_source_parentheses(text: str) -> str:
    """Avoid prose like ``(...약 50.6억 달러(원자료 ...))``."""
    pattern = re.compile(
        r"(약\s+[0-9][0-9,.]*\s*억\s*달러)\(원자료\s+([0-9,]+천달러)\)"
    )
    out: list[str] = []
    pos = 0
    for match in pattern.finditer(text or ""):
        out.append(text[pos:match.start()])
        if _span_inside_parentheses(text, match.start()):
            out.append(f"{match.group(1)}, 원자료 {match.group(2)}")
        else:
            out.append(match.group(0))
        pos = match.end()
    out.append((text or "")[pos:])
    return "".join(out)


def _parse_export_rows(text: str) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    for match in _EXPORT_ROW_RE.finditer(text or ""):
        rows[match.group(1)] = {
            "DRAM": int(match.group(2).replace(",", "")),
            "NAND": int(match.group(3).replace(",", "")),
            "DRAM_KG": int(match.group(4).replace(",", "")),
            "NAND_KG": int(match.group(5).replace(",", "")),
        }
    return rows


def _metric_near(text: str, start: int, end: int) -> str:
    left_bound = max(
        text.rfind("\n", 0, start),
        text.rfind("+", 0, start),
        text.rfind("-", 0, start),
        text.rfind(":", 0, start),
    )
    right_candidates = [pos for pos in (
        text.find("\n", end),
        text.find(".", end),
        text.find(",", end),
    ) if pos >= 0]
    right_bound = min(right_candidates) if right_candidates else len(text)
    clause = text[left_bound + 1:right_bound]
    clause_nand_pos = clause.rfind("NAND")
    clause_dram_pos = clause.rfind("DRAM")
    if clause_nand_pos >= 0 or clause_dram_pos >= 0:
        return "NAND" if clause_nand_pos > clause_dram_pos else "DRAM"

    left = text[max(0, start - 80):start]
    right = text[end:min(len(text), end + 40)]
    window = left + right
    nand_pos = window.rfind("NAND")
    dram_pos = window.rfind("DRAM")
    if nand_pos >= 0 and nand_pos > dram_pos:
        return "NAND"
    return "DRAM"


def _nearest_period_for_span(text: str, start: int, end: int) -> str:
    previous: list[tuple[int, str]] = []
    for match in _PERIOD_RE.finditer(text or ""):
        if match.end() <= start:
            previous.append((start - match.end(), match.group(0).replace("/", "-")))
    if previous:
        previous.sort(key=lambda item: item[0])
        if previous[0][0] <= 100:
            return previous[0][1]

    best_period = ""
    best_distance = 10**9
    for match in _PERIOD_RE.finditer(text or ""):
        period = match.group(0).replace("/", "-")
        if match.end() <= start:
            distance = start - match.end()
        elif match.start() >= end:
            distance = match.start() - end
        else:
            distance = 0
        if distance < best_distance:
            best_distance = distance
            best_period = period
    return best_period if best_distance <= 100 else ""


def _raw_values_near_statement(text: str) -> list[int]:
    values: list[int] = []
    for match in _RAW_THOUSAND_USD_RE.finditer(text or ""):
        try:
            value = int(match.group(0))
        except ValueError:
            continue
        # Export raw values in these reports are normally million-level
        # thousand-USD amounts; avoid catching won-scale scenario values.
        if 100_000 <= value <= 99_999_999:
            values.append(value)
    return values


def _display_scale_for_ambiguous_unit(unit: str) -> float:
    if "백만" in unit:
        return 1_000_000.0
    if "억" in unit:
        return 100_000.0
    return 10_000.0


def _choose_raw_thousand_usd_value(
    raw_values: list[int],
    used_indexes: set[int],
    displayed_text: str,
    unit: str,
) -> tuple[int | None, int | None]:
    try:
        displayed = float(displayed_text.replace(",", ""))
    except ValueError:
        return None, None
    scale = _display_scale_for_ambiguous_unit(unit)
    candidates: list[tuple[float, int, int]] = []
    for idx, value in enumerate(raw_values):
        if idx in used_indexes:
            continue
        diff = abs((value / scale) - displayed)
        candidates.append((diff, idx, value))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0])
    diff, idx, value = candidates[0]
    tolerance = max(2.0, abs(displayed) * 0.03)
    if diff > tolerance:
        return None, None
    return value, idx


def _infer_raw_thousand_usd_from_display(
    displayed_text: str, unit: str, *, thousand_context: bool = False,
) -> int | None:
    """Infer raw thousand-USD value from an explicit ambiguous thousand-USD display."""
    if "천달러" not in (unit or "") and not thousand_context:
        return None
    if "억" in (unit or ""):
        return None
    try:
        displayed = float(displayed_text.replace(",", ""))
    except ValueError:
        return None
    if displayed <= 0:
        return None
    return int(round(displayed * _display_scale_for_ambiguous_unit(unit)))


def _raw_thousand_usd_value_in_unit(unit: str, displayed_text: str = "") -> int | None:
    try:
        displayed = float((displayed_text or "").replace(",", ""))
    except ValueError:
        displayed = 0.0
    candidates: list[int] = []
    for match in re.finditer(r"\b[1-9][0-9]{0,2}(?:,[0-9]{3})+\b|\b[1-9][0-9]{5,}\b", unit or ""):
        try:
            value = int(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if 100_000 <= value <= 99_999_999:
            candidates.append(value)
    if not candidates:
        return None
    if not displayed:
        return candidates[0]
    scales = [_display_scale_for_ambiguous_unit(unit)]
    if "억" in (unit or "") and "천달러" in (unit or ""):
        scales.append(1_000_000.0)
    scored: list[tuple[float, int]] = []
    for value in candidates:
        diff = min(abs((value / scale) - displayed) for scale in scales)
        scored.append((diff, value))
    scored.sort(key=lambda item: item[0])
    tolerance = max(2.0, abs(displayed) * 0.03)
    return scored[0][1] if scored[0][0] <= tolerance else candidates[0]


def _replace_ambiguous_export_units(
    text: str,
    *,
    export_rows: dict[str, dict[str, int]],
    raw_values: list[int],
) -> tuple[str, bool, list[str]]:
    matches = list(_AMBIGUOUS_THOUSAND_USD_RE.finditer(text or ""))
    if not matches:
        return text, False, []

    replacements: list[tuple[int, int, str, str]] = []
    used_raw_indexes: set[int] = set()
    for idx, match in enumerate(matches):
        if _is_weight_context(text, match.start(), match.end()):
            continue
        if _is_unit_value_context(text, match.start(), match.end()):
            continue
        period = _nearest_period_for_span(text, match.start(), match.end())
        metric = _metric_near(text, match.start(), match.end())
        value: int | None = None
        source = ""
        value = _raw_thousand_usd_value_in_unit(match.group("unit"), match.group("num"))
        raw_idx = None
        if value is not None:
            source = "괄호 안 천달러 원자료"
        if value is None:
            value, raw_idx = _choose_raw_thousand_usd_value(
                raw_values,
                used_raw_indexes,
                match.group("num"),
                match.group("unit"),
            )
            if raw_idx is not None:
                used_raw_indexes.add(raw_idx)
            if value is not None:
                source = "문장 내 원자료 숫자"
        if value is None and period and period in export_rows:
            value = export_rows[period][metric]
            source = f"{period} {metric}_금액"
        if value is None:
            context = text[max(0, match.start() - 12):min(len(text), match.end() + 32)]
            value = _infer_raw_thousand_usd_from_display(
                match.group("num"),
                match.group("unit"),
                thousand_context="천달러" in context,
            )
            if value is not None:
                source = "표시값 기반 천달러 환산"
        if value is None:
            continue
        prefix = text[max(0, match.start() - 28):match.start()]
        prefix_match = re.search(r"(?P<num>[0-9][0-9,]*(?:\.\d+)?)\s*(?:→|->|~|-)\s*$", prefix)
        if prefix_match:
            prefix_start = max(0, match.start() - 28) + prefix_match.start("num")
            prefix_end = max(0, match.start() - 28) + prefix_match.end("num")
            before_prefix = text[prefix_start - 1] if prefix_start > 0 else ""
            after_prefix = text[prefix_end] if prefix_end < len(text) else ""
            if not re.match(r"[0-9A-Za-z가-힣.]", before_prefix) and not re.match(r"[0-9A-Za-z가-힣.]", after_prefix):
                prefix_value = _raw_thousand_usd_value_in_unit(match.group("unit"), prefix_match.group("num"))
                if prefix_value is None:
                    prefix_value = _infer_raw_thousand_usd_from_display(
                        prefix_match.group("num"),
                        match.group("unit"),
                        thousand_context="천달러" in text[prefix_start:match.end() + 32],
                    )
                if prefix_value is not None:
                    replacements.append((
                        prefix_start,
                        prefix_end,
                        _format_thousand_usd_for_report(
                            prefix_value,
                            inside_parentheses=_span_inside_parentheses(text, prefix_start),
                        ),
                        "범위 앞 표시값 기반 천달러 환산",
                    ))
        start = match.start()
        if text[max(0, start - 2):start] == "약 ":
            start -= 2
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        replacements.append((
            start,
            match.end(),
            _format_thousand_usd_for_report(
                value,
                inside_parentheses=_span_inside_parentheses(text, match.start()) or (before == "(" and after == ")"),
            ),
            source,
        ))

    if not replacements:
        return text, False, []

    out = text
    reasons: list[str] = []
    for start, end, replacement, source in reversed(replacements):
        out = out[:start] + replacement + out[end:]
        reasons.append(f"{source}를 원자료 단위 그대로 {replacement}로 표기")
    reasons.reverse()
    return out, out != text, reasons


def _is_weight_context(text: str, start: int, end: int) -> bool:
    """Avoid interpreting Korean 만 in weight clauses as money."""
    after = (text[end:end + 8] or "").lower()
    if after.startswith(("kg", "㎏")) or after.startswith("톤"):
        return True
    before_local = text[max(0, start - 24):start]
    after_local = text[end:min(len(text), end + 32)]
    if "중량" in before_local and re.search(r"(kg|KG|㎏|톤)", after_local):
        return True
    left_bound = max(text.rfind(",", 0, start), text.rfind(":", 0, start), text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [pos for pos in (
        text.find(",", end),
        text.find(".", end),
        text.find("\n", end),
    ) if pos >= 0]
    right_bound = min(right_candidates) if right_candidates else len(text)
    clause = text[left_bound + 1:right_bound]
    return "중량" in clause and bool(re.search(r"(kg|KG|㎏|톤)", clause))


def _is_unit_value_context(text: str, start: int, end: int) -> bool:
    """Avoid converting ASP/unit-value tokens while still fixing amount clauses."""
    left_bound = max(text.rfind(",", 0, start), text.rfind(":", 0, start), text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [pos for pos in (
        text.find(",", end),
        text.find(".", end),
        text.find("\n", end),
    ) if pos >= 0]
    right_bound = min(right_candidates) if right_candidates else len(text)
    clause = text[left_bound + 1:right_bound]
    return bool(_UNIT_VALUE_CONTEXT_RE.search(clause))


def _has_unresolved_export_unit(text: str) -> bool:
    return bool(_UNRESOLVED_EXPORT_UNIT_RE.search(text or ""))


def _has_unresolved_ambiguous_export_money(text: str) -> bool:
    for match in _AMBIGUOUS_THOUSAND_USD_RE.finditer(text or ""):
        if "원자료" in match.group("unit"):
            continue
        if not _is_weight_context(text, match.start(), match.end()):
            return True
    return False


def _correct_eok_dollar_thousand_basis_summary(text: str) -> tuple[str, bool, list[str]]:
    """Fix summary prose that used million-USD figures as Korean 억달러."""
    if "천달러 기준 합계" not in (text or "") or "수출금액" not in (text or ""):
        return text, False, []
    if re.search(r"\b[1-9][0-9]{0,2}(?:,[0-9]{3})+\b", text or ""):
        return text, False, []

    reasons: list[str] = []

    def repl(match: re.Match[str]) -> str:
        displayed = float(match.group("num").replace(",", ""))
        raw_value = int(round(displayed * 1_000_000.0))
        replacement = _format_thousand_usd_for_report(raw_value)
        reasons.append(f"{match.group(0)}을 천달러 기준 합계 환산으로 {replacement}로 표기")
        return replacement

    corrected = re.sub(
        r"(?P<num>[0-9][0-9,]*(?:\.\d+)?)\s*억\s*(?:달러|불)",
        repl,
        text,
    )
    return corrected, corrected != text, reasons


def _deterministic_export_unit_correction(
    *, original_statement: str, source_quote: str, chunk_text: str, target_text: str = "",
) -> dict[str, Any] | None:
    """Correct ambiguous thousand-USD export wording using document data.

    The safest correction for these reports is not to invent a converted
    Korean unit, but to restore the raw table unit: ``N천달러``.
    """
    combined = "\n".join(
        part for part in (source_quote, chunk_text, original_statement) if part
    )
    unit_probe = original_statement + " " + source_quote
    if "천달러" not in combined or not any(unit in unit_probe for unit in ("만", "억", "백만")):
        return None

    export_rows = _parse_export_rows(combined)
    raw_values = _raw_values_near_statement(original_statement)
    if not raw_values:
        raw_values = _raw_values_near_statement(source_quote)

    corrected_statement, changed, reasons = _replace_ambiguous_export_units(
        original_statement,
        export_rows=export_rows,
        raw_values=raw_values,
    )
    if not changed:
        corrected_statement, changed, reasons = _correct_eok_dollar_thousand_basis_summary(
            original_statement
        )
    if not changed:
        return None
    if _has_unresolved_export_unit(corrected_statement) or _has_unresolved_ambiguous_export_money(corrected_statement):
        return None

    suggested_quote = ""
    if target_text:
        suggested_quote, quote_changed, _ = _replace_ambiguous_export_units(
            target_text,
            export_rows=export_rows,
            raw_values=raw_values,
        )
        if not quote_changed:
            suggested_quote, quote_changed, _ = _correct_eok_dollar_thousand_basis_summary(
                target_text
            )
        if not quote_changed:
            suggested_quote = corrected_statement
    else:
        suggested_quote = corrected_statement

    return {
        "corrected_statement": corrected_statement,
        "suggested_quote": suggested_quote,
        "reason": "수출 금액의 천달러 원자료 단위를 보존해 교정: " + "; ".join(reasons),
    }


def _llm_affirms_original(reason: str) -> bool:
    """True when the LLM explicitly says the source statement should stand."""
    return bool(_LLM_AFFIRMS_ORIGINAL_RE.search(reason or ""))


def _is_actionable_document_correction(
    *, original_statement: str, corrected_statement: str, target_text: str, suggested_quote: str = "",
) -> bool:
    """Return whether a changed atom can lead to a concrete document edit."""
    if suggested_quote:
        return True
    original = (original_statement or "").strip()
    corrected = (corrected_statement or "").strip()
    target = (target_text or "").strip()
    if not target or not corrected or corrected == original:
        return False
    if original and original in target:
        return True
    old_tokens = _value_tokens(original)
    new_tokens = _value_tokens(corrected)
    if old_tokens and len(old_tokens) == len(new_tokens):
        return all(token in target for token in old_tokens)
    return False


def _choose_target_rewrite_text(
    *, original_statement: str, source_quote: str, chunk_text: str,
) -> str:
    """Pick the smallest report text span the target rewrite may replace.

    ``chunk_text`` can be a full section and often contains raw tables. For an
    on-demand atom correction, returning a rewrite of that whole chunk is too
    destructive. Prefer the exact report sentence/block that contains the atom;
    keep full chunks only as context passed separately.
    """
    statement = (original_statement or "").strip()
    quote = (source_quote or "").strip()
    chunk = (chunk_text or "").strip()

    if statement:
        for line in _nonblank_lines(quote):
            if statement in line or line in statement:
                return line
        for line in _nonblank_lines(chunk):
            if statement in line or line in statement:
                return line
        if statement in chunk:
            return statement

    quote_lines = _nonblank_lines(quote)
    if quote_lines:
        non_table = [line for line in quote_lines if not _contains_table_marker(line)]
        value_tokens = set(_value_tokens(statement))
        if value_tokens:
            scored: list[tuple[int, str]] = []
            for line in non_table or quote_lines:
                score = sum(1 for token in value_tokens if token in line)
                if score:
                    scored.append((score, line))
            if scored:
                scored.sort(key=lambda item: (-item[0], len(item[1])))
                return scored[0][1]
        if len(quote_lines) <= 6 and len(quote) <= 2000:
            return quote
        if non_table:
            return min(non_table, key=len)

    return chunk


def _scenario_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in re.finditer(
        r"(상방|중간|하방)\s*[: ]\s*([-+]?\d[\d,]*(?:\.\d+)?(?:e[+-]?\d+)?)\s*(원|조원|억원)?",
        text or "",
        re.IGNORECASE,
    ):
        values.setdefault(match.group(1), match.group(2).replace(",", "") + (match.group(3) or ""))
    return values


def _is_underdetermined_reason(reason: str) -> bool:
    return any(marker in (reason or "") for marker in _UNCERTAIN_CORRECTION_MARKERS)


def _normalize_judged_correction(
    *, original_statement: str, proposed: str, reason: str,
) -> tuple[str, bool, str]:
    """Accept a corrected atom only when the reason is decisive enough."""
    corrected = (proposed or "").strip() or original_statement
    if corrected == original_statement:
        return original_statement, False, ""

    if _is_underdetermined_reason(reason):
        return original_statement, False, (
            "교정 근거가 불확실하거나 수동 검토가 필요하므로 자동 교정으로 채택하지 않음"
        )

    if "도메인 상식" in (reason or "") and not any(
        marker in reason for marker in ("원문", "source_quote", "related", "계산", "산술", "데이터")
    ):
        return original_statement, False, (
            "도메인 상식만으로 확정한 교정은 자동 교정으로 채택하지 않음"
        )

    orig_scenario = _scenario_values(original_statement)
    corr_scenario = _scenario_values(corrected)
    if set(orig_scenario) == {"상방", "중간", "하방"} and set(corr_scenario) == {"상방", "중간", "하방"}:
        for label, value in corr_scenario.items():
            for other_label, other_value in orig_scenario.items():
                if label != other_label and value == other_value and value != orig_scenario[label]:
                    return original_statement, False, (
                        f"{label} 값이 {other_label}의 원래 값으로 바뀌어 라벨 재배치 위험"
                    )

    return corrected, True, ""


def _validate_rewrite_suggestion(
    *, original_text: str, suggested_text: str, corrected_statement: str,
) -> tuple[str, bool, str]:
    """Guard against destructive quote rewrites before exposing them."""
    original = (original_text or "").strip()
    suggested = (suggested_text or "").strip()
    if not original or not suggested or suggested == original:
        return "", False, "교정 대상 원문이 없거나 제안이 원문과 동일함"

    original_lines = _nonblank_lines(original)
    suggested_lines = _nonblank_lines(suggested)
    if len(original_lines) <= 1 and len(suggested_lines) > 1:
        return "", False, "단일 문장 교정 대상에 여러 줄 제안을 반환함"

    if not _contains_table_marker(original) and _contains_table_marker(suggested):
        return "", False, "원문 대상에 없던 표/원자료 텍스트를 제안에 포함함"

    if len(suggested) > max(len(original) * 2, len(original) + 600):
        return "", False, "교정 제안이 원문 대상보다 과도하게 길어짐"

    corrected_tokens = set(_value_tokens(corrected_statement))
    original_tokens = set(_value_tokens(original))
    new_tokens = [token for token in _value_tokens(suggested) if token not in original_tokens]
    if corrected_tokens and any(token not in corrected_tokens for token in new_tokens[:12]):
        return "", False, "교정 fact와 무관한 새 수치가 제안에 포함됨"

    return suggested, True, ""


# --------------------------------------------------------------------------- #
# Proposal-only backward repair (multi-gold premise revisions)
# --------------------------------------------------------------------------- #
def _replace_amount_near_keyword(text: str, keyword_re: str, new_eokwon: float) -> str:
    """Rewrite the 금액 token after a keyword to a new value, keeping the unit."""
    for kw in re.finditer(keyword_re, text):
        window = text[kw.end():kw.end() + 40]
        match = _AMOUNT_RE.search(window)
        if not match:
            continue
        unit = match.group(2)
        scaled = new_eokwon / _AMOUNT_UNIT_EOKWON[unit]
        had_decimal = "." in match.group(1)
        if had_decimal or unit in ("조원", "조"):
            rendered = f"{scaled:,.2f}".rstrip("0").rstrip(".")
        else:
            rendered = f"{round(scaled):,}"
        start = kw.end() + match.start(1)
        end = kw.end() + match.end(1)
        return text[:start] + rendered + text[end:]
    return text


def propose_premise_revisions(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    target_node_id: str,
    corrected_statement: str,
) -> dict[str, Any] | None:
    """Enumerate every premise revision that reproduces a corrected ratio atom.

    When the corrected atom is a derived ratio (영업이익률 = 영업이익/매출액),
    support revision is underdetermined: raising 영업이익 or lowering 매출액 both
    license the new value, so committing to one fabricates evidence. Instead of
    rewriting upstream text directly, this returns the full *multi-gold* set of
    single-premise revisions -- each verified against the formula -- ranked by
    minimality (relative change), for the author to choose from.

    Returns None when the target is not a parseable derived ratio, so callers
    can fall back to the LLM chunk-rewrite path unchanged.
    """
    node_by_id = {str(n.get("id")): n for n in (nodes or []) if n.get("id")}
    target = node_by_id.get(str(target_node_id))
    if target is None:
        return None
    original = _node_statement(target)
    if not _is_margin_atom(original):
        return None
    old_pct = _parse_pct_value(original)
    new_pct = _parse_pct_value(corrected_statement or "")
    if old_pct is None or new_pct is None or abs(new_pct - old_pct) < 0.05:
        return None

    # premises = derived_from neighbours of the target (either edge direction)
    premise_ids: set[str] = set()
    for edge in edges or []:
        if _relation_value(edge) != "derived_from":
            continue
        src, tgt = str(edge.get("source") or ""), str(edge.get("target") or "")
        if src == str(target_node_id):
            premise_ids.add(tgt)
        elif tgt == str(target_node_id):
            premise_ids.add(src)

    revenue_node = opinc_node = None
    revenue_val = opinc_val = None
    for pid in premise_ids:
        node = node_by_id.get(pid)
        if node is None:
            continue
        stmt = _node_statement(node)
        if revenue_node is None and "매출액" in stmt:
            val = _amount_near_keyword(stmt, r"매출액")
            if val:
                revenue_node, revenue_val = node, val
        elif opinc_node is None and "영업이익" in stmt and "영업이익률" not in stmt:
            val = _amount_near_keyword(stmt, r"영업이익(?!률)")
            if val is not None:
                opinc_node, opinc_val = node, val
    if revenue_node is None or opinc_node is None:
        return None

    def verified(op: float, rev: float) -> bool:
        return rev > 0 and abs(op / rev * 100.0 - new_pct) <= 0.15

    def build(node: dict[str, Any], role: str, keyword_re: str,
              old_val: float, new_val: float) -> dict[str, Any]:
        stmt = _node_statement(node)
        # source_quote is the literal report text, so a replacement there can
        # be applied to the document verbatim; fall back to the statement.
        quote = str((node.get("properties") or {}).get("source_quote") or "").strip()
        original_quote = quote or stmt
        return {
            "premise_node_id": str(node.get("id")),
            "premise_role": role,
            "original_statement": stmt,
            "original_value_eokwon": old_val,
            "proposed_value_eokwon": new_val,
            "suggested_statement": _replace_amount_near_keyword(stmt, keyword_re, new_val),
            "original_quote": original_quote,
            "suggested_quote": _replace_amount_near_keyword(original_quote, keyword_re, new_val),
            "rel_change": abs(new_val - old_val) / old_val if old_val else 0.0,
        }

    proposals: list[dict[str, Any]] = []
    new_opinc = round(new_pct / 100.0 * revenue_val, 1)
    if verified(new_opinc, revenue_val):
        proposals.append(build(opinc_node, "영업이익", r"영업이익(?!률)", opinc_val, new_opinc))
    if new_pct > 0:
        new_revenue = round(opinc_val / (new_pct / 100.0), 1)
        if verified(opinc_val, new_revenue):
            proposals.append(build(revenue_node, "매출액", r"매출액", revenue_val, new_revenue))
    if not proposals:
        return None
    proposals.sort(key=lambda p: p["rel_change"])
    return {
        "target_node_id": str(target_node_id),
        "kind": "margin_ratio",
        "formula": "영업이익률 = 영업이익 / 매출액 x 100",
        "old_value_pct": old_pct,
        "new_value_pct": new_pct,
        "proposals": proposals,
        "underdetermined": len(proposals) >= 2,
        "note": (
            "여러 전제 수정이 동일하게 목표값을 재현하므로 시스템이 하나를 임의로 "
            "확정하지 않고, 검증된 제안을 최소 변경 순으로 나열합니다."
        ),
    }


def merge_correction_into_quote(
    original_statement: str, corrected_statement: str, quote: str,
) -> tuple[str, bool]:
    """Transplant the corrected VALUES into the report's own sentence.

    The corrected statement is a correction of the normalized atom, not of the
    report text; replacing the report sentence with the atom verbatim discards
    its wording and context. Instead, diff the value tokens between the
    original and corrected atom statements and rewrite only those tokens
    inside ``quote``. Returns (merged_text, applied); ``applied=False`` means
    the token mapping could not be established and the caller must not
    substitute the atom text silently.
    """
    orig_tokens = _value_tokens(original_statement)
    corr_tokens = _value_tokens(corrected_statement)
    if len(orig_tokens) == len(corr_tokens):
        pairs = [(o, c) for o, c in zip(orig_tokens, corr_tokens) if o != c]
    else:  # token counts differ: fall back to set-difference pairing
        removed = [t for t in orig_tokens if t not in corr_tokens]
        added = [t for t in corr_tokens if t not in orig_tokens]
        if len(removed) != len(added):
            return "", False
        pairs = list(zip(removed, added))
    if not pairs or any(o not in quote for o, _ in pairs):
        return "", False
    merged = quote
    for old, new in pairs:
        merged, replaced = _replace_value_token_once(merged, old, new)
        if not replaced:
            return "", False
    return merged, True


def _heuristic_rewrite(chunk_text: str, original_statement: str, corrected_statement: str) -> tuple[str, bool]:
    """Best-effort literal value replacement. Returns (suggested_text, applied)."""
    return merge_correction_into_quote(original_statement, corrected_statement, chunk_text)


def _chunk_prompt(
    *,
    original_statement: str,
    corrected_statement: str,
    chunk: dict[str, Any],
    chunk_text: str,
) -> dict[str, Any]:
    return {
        "correction": {
            "original_fact": original_statement,
            "corrected_fact": corrected_statement,
        },
        "affected_chunk": {
            "chunk_id": chunk.get("chunk_id"),
            "section": chunk.get("section"),
            "direction": chunk.get("direction"),
            "text": chunk_text,
            "related_atoms": [atom.get("statement") for atom in chunk.get("atoms") or []],
        },
        "instruction": (
            "한 fact가 correction.original_fact 에서 correction.corrected_fact 로 교정되었습니다. "
            "affected_chunk.text 는 이 fact 와 인과적으로 연결된(영향을 주거나 받는) 원문 chunk 입니다. "
            "이 교정으로 chunk 의 서술이 사실과 어긋나게 되는지 판단하고, 어긋난다면 chunk 전체를 "
            "교정된 사실과 일관되게 다시 작성하세요. 영향을 받지 않는 부분은 원문 그대로 두고 "
            "영향을 받은 수치·서술만 고치세요. 문서에 없는 새로운 사실을 만들지 마세요. "
            'JSON 으로만 출력: {"affected": boolean, "reason": string, "suggested_text": string}. '
            "affected 가 false 면 suggested_text 는 빈 문자열로 두세요."
        ),
    }


def _target_chunk_rewrite_prompt(
    *, original_statement: str, corrected_statement: str, chunk_text: str, section: str,
    related_statements: list[str] | None = None,
    related_chunks: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correction": {
            "original_fact": original_statement,
            "corrected_fact": corrected_statement,
        },
        "original_text": chunk_text,
        "section": section,
        "instruction": (
            "correction.original_fact 가 correction.corrected_fact 로 교정되었습니다. "
            "original_text 는 이 fact 가 서술된 보고서의 교정 대상 문장 또는 짧은 블록입니다. "
            "original_text 만 교정된 사실에 맞게 다시 작성하세요. related_atoms 와 related_chunks 는 그래프에서 이 "
            "fact 와 직접 연결된 사실·원문 맥락이니, 교정 결과가 이들과 일관되도록 하세요. "
            "규칙: (1) 반드시 original_text 와 같은 범위만 반환하고, related_chunks 나 원자료 표를 "
            "복사해 넣지 마세요. (2) 원문의 문장 구조·어조·길이를 그대로 "
            "유지하고, 문장을 요약·축약하거나 표현을 삭제하지 마세요. (3) 바뀐 수치를 "
            "반영하고, 그 수치에 의존하는 서술(예: '크게 개선되었다', '증가했다')이 새 값과 "
            "모순되면 그 표현만 자연스럽게 고치세요. (4) 문서와 related 맥락에 없는 새로운 "
            "사실이나 수치를 추가하지 마세요. (5) original_text 가 한 줄이면 suggested_text 도 "
            "한 줄이어야 합니다. (6) original_text 가 표(|...|) 형식이면 형식을 그대로 유지하고, "
            "표가 아니면 표를 만들지 마세요. "
            'JSON 으로만 출력: {"suggested_text": string, "reason": string}.'
        ),
    }
    if related_statements:
        payload["related_atoms"] = related_statements[:12]
    if related_chunks:
        payload["related_chunks"] = related_chunks[:4]
    return payload


def _related_context(
    *,
    node_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    target_node_id: str,
    chunk_text_lookup: dict[str, str],
    exclude_chunk: str = "",
    max_chunks: int = 4,
    max_chunk_chars: int = 2000,
) -> tuple[list[str], list[str]]:
    """그래프에서 target 과 직접(인과) 연결된 atom statement 와, 그 atom 들이
    속한 원본 청크 텍스트를 수집한다 — 교정 제안/재작성의 retrieval 근거."""
    related_statements: list[str] = []
    related_chunk_ids: list[str] = []
    seen_atoms: set[str] = set()
    for edge in edges or []:
        if _relation_value(edge) not in _CAUSAL_RELATIONS:
            continue
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        other = target if source == str(target_node_id) else (
            source if target == str(target_node_id) else "")
        if not other or other in seen_atoms:
            continue
        seen_atoms.add(other)
        other_node = node_by_id.get(other)
        if other_node is None:
            continue
        statement = _node_statement(other_node)
        if statement:
            related_statements.append(statement)
        other_chunk = _node_chunk_id(other_node)
        if other_chunk and other_chunk != exclude_chunk \
                and other_chunk not in related_chunk_ids:
            related_chunk_ids.append(other_chunk)
    related_chunks = [
        chunk_text_lookup[cid][:max_chunk_chars]
        for cid in related_chunk_ids[:max_chunks]
        if chunk_text_lookup.get(cid)
    ]
    return related_statements, related_chunks


def _target_correction_prompt(
    *, original_statement: str, source_quote: str, section: str,
    chunk_text: str = "", related_statements: list[str] | None = None,
    suspect_reason: str = "", verified_correction_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "atom": {
            "statement": original_statement,
            "source_quote": source_quote,
            "section": section,
        },
        "instruction": (
            "다음 fact atom 이 사실과 어긋나는지 점검하고, 어긋난다면 교정된 statement 를 제안하세요. "
            "original_chunk_text(이 atom 이 나온 보고서 원문)와 related_atoms(연결된 사실들)를 근거로 "
            "내부 수치 일관성(예: 영업이익률 = 영업이익/매출액, 합계, 증감률)과 명백한 모순을 우선 확인하고, "
            "suspect_reason 이 있으면 그 지적과 교정 힌트를 검증해 반영하세요. "
            "verified_correction_hint 가 있으면 이는 코드가 문서 내부 원자료로 검산한 보조 힌트입니다. "
            "LLM이 원문과 문맥을 다시 확인한 뒤 맞다고 판단하면 이 힌트를 활용해 직접 교정하세요. "
            "힌트가 원문과 충돌하면 힌트를 따르지 말고 reason 에 충돌 사유를 쓰세요. "
            "문서 내부 근거만으로 판정하기 어려워도, 상장사 재무제표·애널리스트 리포트에서 통상적인 "
            "숫자 규모와 단위(원/억원/조원/%/배), 회사 규모, 지표 성격에 비추어 명백히 비현실적인 "
            "값은 LLM의 금융 도메인 지식과 직관으로 검토하세요. "
            "단, 도메인 상식만으로 '정답 값'을 하나로 확정할 수 없으면 corrected_statement 를 "
            "원문 그대로 두고 reason 에 의심 사유와 수동 검토 필요를 쓰세요. "
            "예를 들어 대형 반도체 기업의 연간 영업이익, 컨센서스, 목표주가, 주가, 영업이익률이 "
            "일반적인 범위와 큰 폭으로 어긋나면 숫자 규모·단위 오류 후보로 다루세요. "
            "단위 변환은 반드시 산술적으로 검산하세요. 특히 '천달러'는 thousand USD 이므로 "
            "20,407,971천달러 = 20,407.971백만 달러 = 약 204.1억 달러 = 약 2,040,797.1만 달러입니다. "
            "5,059,379천달러는 5,059.379백만 달러 = 약 50.6억 달러 = 약 505,937.9만 달러입니다. "
            "'만 천달러'처럼 중첩 단위가 나오면 숫자를 임의로 줄이지 말고, 원자료 단위가 "
            "천달러인지 달러인지 확인할 수 없으면 corrected_statement 를 원문 그대로 두고 "
            "reason 에 수동 검토 필요와 가능한 변환 후보를 쓰세요. 수출 통계 교정문은 원자료 숫자만 "
            "그대로 쓰면 읽기 어려우므로, 보고서 문장에서는 명확한 환산 단위를 우선 사용하세요. "
            "예: '5,059,379천달러' 단독 표기보다 '약 50.6억 달러(원자료 5,059,379천달러)'가 낫습니다. "
            "단, 원자료 근거를 괄호 안에 남겨 검산 가능하게 하세요. "
            "상방/중간/하방 시나리오 값은 라벨의 의미를 보존하세요. 값의 크기순으로 "
            "숫자를 라벨에 재배치하지 마세요. 상방 < 중간 또는 중간 < 하방처럼 순서가 "
            "깨진 경우에는 먼저 자릿수 누락(예: 10500000000000 → 105000000000000)이나 "
            "단위 누락을 검토하고, 어느 값이 틀렸는지 확정할 수 없으면 원문을 유지한 채 "
            "수동 검토 필요를 reason 에 쓰세요. "
            "이때 reason 에 문서 내부 근거인지, 관련 atom 근거인지, 도메인 상식 기반인지와 "
            "불확실성을 명확히 쓰세요. "
            "교정 statement 는 원문 statement 와 같은 형식을 유지하고 값·서술만 고치세요. "
            "교정 후보가 여러 개라 단정할 수 없으면 corrected_statement 를 원문 그대로 두고 "
            "reason 에 가능한 후보와 수동 검토 필요를 명시하세요. "
            'JSON 으로만 출력: {"corrected_statement": string, "reason": string}. '
            "교정이 불필요하면 corrected_statement 는 원문 statement 그대로 두세요."
        ),
    }
    if chunk_text:
        payload["original_chunk_text"] = chunk_text[:4000]
    if related_statements:
        payload["related_atoms"] = related_statements[:12]
    if suspect_reason:
        payload["suspect_reason"] = suspect_reason
    if verified_correction_hint:
        payload["verified_correction_hint"] = {
            "corrected_statement": str(verified_correction_hint.get("corrected_statement") or "")[:1200],
            "reason": str(verified_correction_hint.get("reason") or "")[:1200],
        }
    return payload


def _loads_lenient(content: str) -> dict[str, Any]:
    """Backward-compatible wrapper around the shared balanced JSON parser."""
    return parse_object(content)


async def _post_json(
    cli: httpx.AsyncClient,
    *,
    base: str,
    api_key: str,
    model: str,
    system: str,
    prompt: dict[str, Any],
    max_tokens: int,
    schema_kind: str | None = None,
) -> dict[str, Any]:
    """Call the LLM with a schema, then retry once using compact JSON mode.

    Some OpenAI-compatible servers reject ``json_schema`` even though they
    support ``json_object``.  The first attempt therefore uses the formal
    schema and the second is a compatibility fallback; both attempts still
    carry the exact schema in the system prompt.
    """
    if schema_kind:
        # Fail early for programmer errors rather than sending an unvalidated
        # schema to the model server.
        response_format_for(schema_kind)
    last_error: Exception | None = None
    for attempt in range(2):
        attempt_system = system
        if schema_kind:
            attempt_system += format_instruction(schema_kind)
        if attempt:
            attempt_system += (
                " This is a retry. Return a compact object with every required "
                "field, do not reason aloud, and do not truncate the JSON."
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": attempt_system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": structured_response_format(
                schema_kind, attempt=attempt,
            ),
        }
        stop_sequences = factreasoner_stop_sequences()
        if stop_sequences:
            payload["stop"] = stop_sequences
        with_local_chat_template(payload)
        try:
            resp = await cli.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"LLM HTTP {resp.status_code}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
            body = resp.json()
            choice = body["choices"][0]
            if str(choice.get("finish_reason") or "").lower() in {"length", "content_filter"}:
                raise StructuredJSONError("LLM 응답이 토큰 한도에서 잘렸습니다.")
            content = choice["message"]["content"]
            return parse_object(content, kind=schema_kind)
        except (httpx.HTTPError, KeyError, TypeError, StructuredJSONError, json.JSONDecodeError) as exc:
            last_error = exc
            if is_json_schema_rejection(exc):
                mark_json_schema_unsupported()
            if attempt == 1:
                raise
    raise StructuredJSONError(f"LLM structured response failed: {last_error}")


async def _judge_atom_with_client(
    cli: httpx.AsyncClient,
    *,
    base: str,
    api_key: str,
    model: str,
    target_node: dict[str, Any],
    chunk_text: str = "",
    related_statements: list[str] | None = None,
    verified_correction_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the LLM whether a single fact atom needs correction.

    The proposal is grounded first in the atom's ORIGINAL chunk text, related
    atoms (graph neighbours), and suspect analysis. If those are insufficient,
    the prompt also allows bounded financial-domain judgment for obvious
    numeric magnitude/unit errors, with uncertainty recorded in the reason.
    Returns ``{corrected_statement, reason, changed, online, error}``.
    """
    original_statement = _node_statement(target_node)
    props = target_node.get("properties") or {}
    source_quote = str(props.get("source_quote") or "").strip()
    out: dict[str, Any] = {
        "corrected_statement": original_statement,
        "reason": "",
        "changed": False,
        "online": False,
        "error": None,
    }
    try:
        data = await _post_json(
            cli,
            base=base,
            api_key=api_key,
            model=model,
            system="You are a precise financial fact checker. Output JSON only.",
            prompt=_target_correction_prompt(
                original_statement=original_statement,
                source_quote=source_quote,
                section=_node_section(target_node),
                chunk_text=chunk_text,
                related_statements=related_statements,
                suspect_reason=str(props.get("suspect_reason") or "").strip(),
                verified_correction_hint=verified_correction_hint,
            ),
            max_tokens=_ATOM_JUDGEMENT_MAX_TOKENS,
            schema_kind="atom_judgment",
        )
        out["online"] = True
        proposed = _flatten_nested_export_source_parentheses(
            str(data.get("corrected_statement") or "").strip()
        )
        raw_reason = str(data.get("reason") or "").strip()
        corrected, accepted, rejection_reason = _normalize_judged_correction(
            original_statement=original_statement,
            proposed=proposed,
            reason=raw_reason,
        )
        out["corrected_statement"] = corrected
        out["reason"] = raw_reason
        out["changed"] = bool(accepted and corrected != original_statement)
        if not accepted and proposed and proposed != original_statement:
            out["needs_review"] = True
            out["rejected_corrected_statement"] = proposed
            out["rejection_reason"] = rejection_reason
    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        out["error"] = f"target 교정 제안 실패: {exc}"
    return out


_BATCH_TURN_SIZE = 128
_BATCH_TURN_CONCURRENCY = 4
_BATCH_ATOM_CONTEXT_CHARS = 360
# llama.cpp counts prompt and generated JSON against the same 32k context.
# Keep the serialized user payload comfortably below that limit while still
# allowing a graph partition to approach 128 atoms when their records are
# short. Oversized trees are split further without mixing components.
_BATCH_PROMPT_MAX_CHARS = 22000


def _fact_atom_tree_batches(
    values: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    size: int = _BATCH_TURN_SIZE,
    context_values: list[dict[str, Any]] | None = None,
) -> list[list[dict[str, Any]]]:
    """Group candidates by tree and add one-hop boundary context.

    Edges to non-candidates are not used as bridges: otherwise one generic
    context atom could merge unrelated correction candidates into one huge
    component. Candidates without an edge still form a singleton tree. An
    oversized tree is split in deterministic breadth-first order, preserving
    nearby facts in the same turn as much as the context limit allows.

    ``context_values`` is the full graph record set.  When supplied, each
    partition receives at most one incoming parent of its first (top) atom and
    one outgoing child of its last (bottom) atom.  These boundary atoms are
    reference-only: they are included in the fact-check turn, but are marked
    ``correction_allowed=False`` and must never become a rewrite candidate.
    The active candidate count remains bounded by ``size``; the two optional
    boundary records are extra context around that partition.
    """
    by_id = {str(item.get("id") or ""): item for item in values if item.get("id")}
    ids = set(by_id)
    context_by_id = (
        {str(item.get("id") or ""): item for item in context_values if item.get("id")}
        if context_values is not None
        else {}
    )
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in ids}
    incoming: dict[str, set[str]] = {}
    outgoing: dict[str, set[str]] = {}
    for edge in edges or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source and target and source != target:
            outgoing.setdefault(source, set()).add(target)
            incoming.setdefault(target, set()).add(source)
        if source in ids and target in ids and source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
    # Atoms from the same source chunk are a local tree fallback when graph
    # extraction emitted no explicit relation edge between them.
    by_chunk: dict[str, list[str]] = {}
    for node_id, item in by_id.items():
        props = item.get("properties") or {}
        chunk_id = str(item.get("chunk_id") or props.get("chunk_id") or "").strip()
        if chunk_id:
            by_chunk.setdefault(chunk_id, []).append(node_id)
    for chunk_ids in by_chunk.values():
        for index, node_id in enumerate(chunk_ids):
            if index:
                adjacency[node_id].add(chunk_ids[index - 1])
                adjacency[chunk_ids[index - 1]].add(node_id)

    components: list[list[str]] = []
    unseen = set(ids)
    while unseen:
        start = min(unseen)
        queue = [start]
        unseen.remove(start)
        component_members: list[str] = []
        while queue:
            node_id = queue.pop(0)
            component_members.append(node_id)
            for neighbor in sorted(adjacency[node_id]):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
        # Prefer a deterministic parent-to-child walk for the partition order
        # so the first/last atoms really are the graph's upper/lower boundary.
        # Cycles or structural-only edges fall back to the connected BFS order.
        member_set = set(component_members)
        roots = sorted(
            node_id for node_id in member_set
            if not (incoming.get(node_id, set()) & member_set)
        )
        walk_queue = list(roots or [min(component_members)])
        walked: set[str] = set()
        ordered: list[str] = []
        while walk_queue:
            node_id = walk_queue.pop(0)
            if node_id in walked:
                continue
            walked.add(node_id)
            ordered.append(node_id)
            directed_children = sorted(outgoing.get(node_id, set()) & member_set)
            neighbors = directed_children or sorted(adjacency[node_id] & member_set)
            walk_queue.extend(neighbor for neighbor in neighbors if neighbor not in walked)
        ordered.extend(node_id for node_id in component_members if node_id not in walked)
        components.append(ordered)

    batches: list[list[dict[str, Any]]] = []
    for component in components:
        tree_id = f"tree:{component[0]}"
        partition_count = max(1, (len(component) + size - 1) // size)
        rank = {node_id: index for index, node_id in enumerate(component)}
        for partition_index, offset in enumerate(range(0, len(component), size)):
            active_ids = component[offset:offset + size]
            active_set = set(active_ids)
            parent_ids = {
                node_id for node_id in incoming.get(active_ids[0], set())
                if node_id not in active_set and node_id in context_by_id
            }
            child_ids = {
                node_id for node_id in outgoing.get(active_ids[-1], set())
                if node_id not in active_set and node_id in context_by_id
            }

            def _boundary_pick(candidates: set[str]) -> str | None:
                if not candidates:
                    return None
                return min(candidates, key=lambda node_id: (rank.get(node_id, 10**9), node_id))

            parent_id = _boundary_pick(parent_ids)
            child_id = _boundary_pick(child_ids)
            boundary_roles: dict[str, list[str]] = {}
            if parent_id:
                boundary_roles.setdefault(parent_id, []).append("parent")
            if child_id:
                boundary_roles.setdefault(child_id, []).append("child")

            batch: list[dict[str, Any]] = []

            def _copy_record(node_id: str, *, is_context: bool) -> dict[str, Any]:
                source_record = (
                    context_by_id.get(node_id) or by_id.get(node_id)
                    if is_context
                    else by_id[node_id]
                )
                item = dict(source_record or {})
                roles = boundary_roles.get(node_id, [])
                item["tree_group_id"] = tree_id
                item["tree_size"] = len(component)
                if is_context:
                    item["partition_index"] = partition_index
                    item["partition_count"] = partition_count
                    item["partition_size"] = len(active_ids)
                    item["is_boundary_context"] = True
                    item["correction_allowed"] = False
                    item["boundary_role"] = "+".join(roles)
                    item["boundary_for"] = active_ids[0] if roles == ["parent"] else (
                        active_ids[-1] if roles == ["child"] else ""
                    )
                return item

            # Keep the graph chain visually ordered: parent context, active
            # atoms in partition order, then child context.
            if parent_id:
                batch.append(_copy_record(parent_id, is_context=True))
            for node_id in active_ids:
                item = _copy_record(node_id, is_context=False)
                batch.append(item)
            if child_id and child_id != parent_id:
                batch.append(_copy_record(child_id, is_context=True))
            batches.append(batch)
    return batches


def _batch_items(
    values: list[dict[str, Any]],
    *,
    edges: list[dict[str, Any]] | None = None,
    size: int = _BATCH_TURN_SIZE,
    context_values: list[dict[str, Any]] | None = None,
) -> list[list[dict[str, Any]]]:
    tree_batches = (
        [values[index:index + size] for index in range(0, len(values), size)]
        if edges is None
        else _fact_atom_tree_batches(values, edges, size=size, context_values=context_values)
    )
    # A connected tree can still have verbose evidence. Split only that tree
    # by serialized size; never combine it with another disconnected tree.
    output: list[list[dict[str, Any]]] = []
    for tree_batch in tree_batches:
        current: list[dict[str, Any]] = []
        for item in tree_batch:
            candidate = [*current, item]
            if current and len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > _BATCH_PROMPT_MAX_CHARS:
                output.append(current)
                current = [item]
            else:
                current = candidate
        if current:
            output.append(current)
    return output


def _batch_atom_context(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    markdown_text: str,
    target_node: dict[str, Any],
) -> dict[str, Any]:
    """Build one compact atom record for a batch turn."""
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    chunk_lookup = {
        str(chunk.get("id")): str(chunk.get("raw_text") or chunk.get("text") or "")
        for chunk in _document_chunks(markdown_text)
    }
    node_id = str(target_node.get("id") or "")
    props = target_node.get("properties") or {}
    statement = _node_statement(target_node)
    chunk_id = _node_chunk_id(target_node)
    chunk_text = chunk_lookup.get(chunk_id, "")
    source_quote = str(props.get("source_quote") or "").strip() or statement
    if source_quote and source_quote not in chunk_text:
        for candidate in chunk_lookup.values():
            if source_quote in candidate:
                chunk_text = candidate
                break
    related_statements, _ = _related_context(
        node_by_id=node_by_id,
        edges=edges,
        target_node_id=node_id,
        chunk_text_lookup=chunk_lookup,
        exclude_chunk=chunk_id,
        max_chunks=0,
        max_chunk_chars=0,
    )
    return {
        "id": node_id,
        "tree_group_id": "",
        "chunk_id": chunk_id,
        "statement": statement[:500],
        "source_quote": source_quote[:500],
        "section": _node_section(target_node),
        "original_chunk_text": chunk_text[:_BATCH_ATOM_CONTEXT_CHARS],
        "related_atoms": [str(item)[:220] for item in related_statements[:2]],
        "suspect_reason": str(props.get("suspect_reason") or "")[:240],
        "is_boundary_context": bool(target_node.get("is_boundary_context")),
        "correction_allowed": bool(target_node.get("correction_allowed", True)),
        "boundary_role": str(target_node.get("boundary_role") or ""),
        "boundary_for": str(target_node.get("boundary_for") or ""),
    }


def _batch_graph_edges(
    batch: list[dict[str, Any]], edges: list[dict[str, Any]], *, limit: int = 256,
) -> list[dict[str, str]]:
    """Return the compact directed edges that connect atoms in one turn."""
    ids = {str(item.get("id") or "") for item in batch}
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in ids or target not in ids or source == target:
            continue
        relation = _relation_value(edge)
        key = (source, target, relation)
        if key in seen:
            continue
        seen.add(key)
        output.append({"source": source, "target": target, "relation": relation})
        if len(output) >= limit:
            break
    return output


async def batch_judge_atoms(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    markdown_text: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Judge multiple atoms per LLM turn, preserving an id-indexed result."""
    all_records = [
        _batch_atom_context(nodes=nodes, edges=edges, markdown_text=markdown_text, target_node=node)
        for node in nodes
    ]
    record_by_id = {str(item.get("id") or ""): item for item in all_records}
    records = [record_by_id[str(node.get("id") or "")] for node in candidates if str(node.get("id") or "") in record_by_id]
    output: dict[str, Any] = {"judgments": {}, "errors": [], "batches": 0, "online": False}
    if not records:
        return output
    base = factreasoner_base_url().rstrip("/")
    api_key = factreasoner_api_key()
    model = factreasoner_judge_model()
    semaphore = asyncio.Semaphore(_BATCH_TURN_CONCURRENCY)

    async def run_batch(batch: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
        prompt = {
            "atoms": batch,
            "ordered_atom_ids": [str(item.get("id") or "") for item in batch],
            "graph_edges": _batch_graph_edges(batch, edges),
            "instruction": (
                "각 atom을 서로 비교하면서 사실성을 판단하세요. 문서 내부 수치·기간·단위·지표 "
                "일관성을 우선하고, 명백한 자릿수/단위 오류 후보만 correct로 표시하세요. "
                "정답을 확정할 수 없으면 review로 표시하고 추측하지 마세요. "
                "graph_edges의 source를 상위/부모, target을 하위/자식으로 해석하세요. "
                "부모와 자식이 충돌하면 하위/자식 atom을 문제 후보로 판단하세요. "
                "is_boundary_context=true인 atom은 연결 문맥 확인용이므로 fact-check 기준으로만 사용하고, "
                "문제가 있어 보여도 correct로 판정하거나 교정 대상으로 만들지 마세요. "
                '각 id를 빠짐없이 한 번씩 반환하세요: {"judgments":[{"id":string,"verdict":"keep|correct|review",'
                '"reason":string}]}. JSON 외 텍스트는 출력하지 마세요.'
            ),
        }
        async with semaphore:
            try:
                async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as cli:
                    data = await _post_json(
                        cli,
                        base=base,
                        api_key=api_key,
                        model=model,
                        system="You are a conservative financial fact judge. Output one batch JSON object only.",
                        prompt=prompt,
                        max_tokens=min(6144, max(2048, len(batch) * 48)),
                        schema_kind="atom_judgments",
                    )
                by_id: dict[str, dict[str, Any]] = {}
                allowed = {str(item["id"]) for item in batch}
                for row in data.get("judgments") or []:
                    if not isinstance(row, dict) or str(row.get("id") or "") not in allowed:
                        continue
                    verdict = str(row.get("verdict") or "review").strip().lower()
                    if verdict not in {"keep", "correct", "review"}:
                        verdict = "review"
                    source = next((item for item in batch if str(item.get("id")) == str(row["id"])), None)
                    if source and bool(source.get("is_boundary_context")):
                        # Boundary context is deliberately never actionable,
                        # even if a model incorrectly returns ``correct``.
                        verdict = "keep"
                        reason = (
                            "경계 문맥 노드는 자동 교정에서 제외했습니다. "
                            + str(row.get("reason") or "").strip()
                        ).strip()
                    else:
                        reason = str(row.get("reason") or "").strip()
                    by_id[str(row["id"])] = {
                        "id": str(row["id"]),
                        "verdict": verdict,
                        "reason": reason,
                        "is_boundary_context": bool(source and source.get("is_boundary_context")),
                        "correction_allowed": bool(source is None or source.get("correction_allowed", True)),
                    }
                missing = allowed - set(by_id)
                if missing:
                    return by_id, f"batch judgment missing ids: {sorted(missing)}"
                return by_id, None
            except (httpx.HTTPError, asyncio.TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return {}, f"batch judgment failed: {type(exc).__name__}: {exc}"

    batches = _batch_items(records, edges=edges, context_values=all_records)
    output["tree_batches"] = [
        {
            "tree_group_id": str(batch[0].get("tree_group_id") or ""),
            "ids": [str(item.get("id")) for item in batch],
            "active_ids": [str(item.get("id")) for item in batch if item.get("correction_allowed", True)],
            "boundary_context_ids": [str(item.get("id")) for item in batch if item.get("is_boundary_context")],
            "ordered_ids": [str(item.get("id")) for item in batch],
        }
        for batch in batches
    ]
    results = await asyncio.gather(*(run_batch(batch) for batch in batches))
    output["batches"] = len(results)
    for by_id, error in results:
        output["judgments"].update(by_id)
        if by_id:
            output["online"] = True
        if error:
            output["errors"].append(error)
    return output


async def batch_correct_atoms(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    markdown_text: str,
    candidates: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Propose exact statement/quote edits for suspicious atoms per turn."""
    # A caller may pass a mixed graph defensively; boundary context is never a
    # correction candidate, even if an upstream model accidentally labels it
    # as ``correct``.
    candidates = [
        node for node in candidates
        if not bool(node.get("is_boundary_context"))
        and bool(node.get("correction_allowed", True))
    ]
    all_records = [
        _batch_atom_context(nodes=nodes, edges=edges, markdown_text=markdown_text, target_node=node)
        for node in nodes
    ]
    for item in all_records:
        # If this record is selected as a boundary context, the correction
        # turn must see an explicit non-actionable judgment as well.
        item["judgment"] = {
            "verdict": "keep",
            "reason": "reference-only boundary context",
        }
    record_by_id = {str(item.get("id") or ""): item for item in all_records}
    records: list[dict[str, Any]] = []
    for node in candidates:
        item = dict(record_by_id.get(str(node.get("id") or "")) or _batch_atom_context(
            nodes=nodes, edges=edges, markdown_text=markdown_text, target_node=node,
        ))
        item["judgment"] = judgments.get(str(node.get("id") or ""), {})
        item["target_text"] = _choose_target_rewrite_text(
            original_statement=item["statement"],
            source_quote=item["source_quote"],
            chunk_text=item["original_chunk_text"],
        )[:2200]
        records.append(item)
    output: dict[str, Any] = {"corrections": {}, "errors": [], "batches": 0, "online": False}
    if not records:
        return output
    base = factreasoner_base_url().rstrip("/")
    api_key = factreasoner_api_key()
    model = factreasoner_correction_model()
    semaphore = asyncio.Semaphore(_BATCH_TURN_CONCURRENCY)

    async def run_batch(batch: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
        prompt = {
            "atoms": batch,
            "ordered_atom_ids": [str(item.get("id") or "") for item in batch],
            "graph_edges": _batch_graph_edges(batch, edges),
            "instruction": (
                "judgment.verdict가 correct인 atom만 교정하고, review/keep은 corrected_statement와 "
                "suggested_quote를 빈 문자열로 두세요. "
                "문서 원문·주변 atom·판정 사유를 재검증해 corrected_statement와 target_text 범위의 "
                "suggested_quote를 제안하세요. 근거가 부족하면 원문을 그대로 반환하세요. "
                "graph_edges의 source는 상위/부모, target은 하위/자식입니다. 부모·자식이 충돌하면 "
                "하위/자식 atom만 교정 후보로 삼고, is_boundary_context=true인 atom은 어떤 경우에도 "
                "교정하지 마세요. "
                "각 id를 빠짐없이 반환하세요. "
                '{"corrections":[{"id":string,"corrected_statement":string,"suggested_quote":string,"reason":string}]}'
                " 형태의 JSON만 출력하세요."
            ),
        }
        async with semaphore:
            try:
                async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as cli:
                    data = await _post_json(
                        cli,
                        base=base,
                        api_key=api_key,
                        model=model,
                        system="You are a conservative financial correction editor. Output one batch JSON object only.",
                        prompt=prompt,
                        max_tokens=min(6144, max(2048, len(batch) * 48)),
                        schema_kind="atom_corrections",
                    )
                by_id: dict[str, dict[str, Any]] = {}
                allowed = {str(item["id"]) for item in batch}
                for row in data.get("corrections") or []:
                    if not isinstance(row, dict) or str(row.get("id") or "") not in allowed:
                        continue
                    row_id = str(row["id"])
                    by_id[row_id] = {
                        "id": row_id,
                        "corrected_statement": str(row.get("corrected_statement") or "").strip(),
                        "suggested_quote": str(row.get("suggested_quote") or "").strip(),
                        "reason": str(row.get("reason") or "").strip(),
                    }
                    source = next((item for item in batch if str(item.get("id")) == row_id), None)
                    if source is not None:
                        if bool(source.get("is_boundary_context")):
                            by_id[row_id]["corrected_statement"] = ""
                            by_id[row_id]["suggested_quote"] = ""
                            by_id[row_id]["excluded_from_correction"] = True
                        by_id[row_id]["original_statement"] = source.get("statement") or ""
                        by_id[row_id]["target_text"] = source.get("target_text") or ""
                        by_id[row_id]["tree_group_id"] = source.get("tree_group_id") or ""
                        by_id[row_id]["tree_size"] = source.get("tree_size") or 1
                missing = allowed - set(by_id)
                if missing:
                    return by_id, f"batch correction missing ids: {sorted(missing)}"
                return by_id, None
            except (httpx.HTTPError, asyncio.TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return {}, f"batch correction failed: {type(exc).__name__}: {exc}"

    batches = _batch_items(records, edges=edges, context_values=all_records)
    output["tree_batches"] = [
        {
            "tree_group_id": str(batch[0].get("tree_group_id") or ""),
            "ids": [str(item.get("id")) for item in batch],
            "active_ids": [str(item.get("id")) for item in batch if item.get("correction_allowed", True)],
            "boundary_context_ids": [str(item.get("id")) for item in batch if item.get("is_boundary_context")],
            "ordered_ids": [str(item.get("id")) for item in batch],
        }
        for batch in batches
    ]
    results = await asyncio.gather(*(run_batch(batch) for batch in batches))
    output["batches"] = len(results)
    for by_id, error in results:
        output["corrections"].update(by_id)
        if by_id:
            output["online"] = True
        if error:
            output["errors"].append(error)
    return output


async def judge_atom(
    *,
    nodes: list[dict[str, Any]],
    target_node_id: str,
    markdown_text: str | None = None,
    edges: list[dict[str, Any]] | None = None,
    corrected_statement: str | None = None,
) -> dict[str, Any]:
    """On-demand judgement of a single fact atom (no propagation).

    The proposal is grounded in the atom's original chunk (from
    ``markdown_text``) and its graph neighbours (from ``edges``). Returns
    ``{node_id, original_statement, corrected_statement, reason, changed,
    online, error}``. When the target node is missing, returns without any
    network call.
    """
    node_by_id = {str(node.get("id")): node for node in (nodes or []) if node.get("id")}
    target_node = node_by_id.get(str(target_node_id))
    base = factreasoner_base_url().rstrip("/")
    api_key = factreasoner_api_key()
    model = factreasoner_judge_model()
    result: dict[str, Any] = {
        "node_id": str(target_node_id),
        "original_statement": "",
        "corrected_statement": "",
        "reason": "",
        "changed": False,
        "online": False,
        "model": model,
        "error": None,
    }
    if target_node is None:
        result["error"] = f"target_node_id '{target_node_id}' 를 그래프에서 찾을 수 없습니다."
        return result

    original_statement = _node_statement(target_node)
    result["original_statement"] = original_statement
    result["corrected_statement"] = original_statement

    # 근거 수집(retrieval): atom 이 나온 원본 청크 + 그래프에서 직접 연결된
    # atom statement 들과 그 atom 들이 속한 청크 원문
    target_chunk = _node_chunk_id(target_node)
    chunk_text_lookup: dict[str, str] = {}
    if markdown_text:
        for chunk in _document_chunks(markdown_text):
            chunk_text_lookup[str(chunk.get("id"))] = str(
                chunk.get("raw_text") or chunk.get("text") or "")
    chunk_text = chunk_text_lookup.get(target_chunk, "")
    # 청크 경계 드리프트 보정: 그래프가 옛 청킹으로 만들어졌으면 chunk_id 가
    # 다른 청크를 가리킬 수 있다. atom 의 근거 인용이 실제로 들어 있는 청크를
    # 찾아 앵커로 쓴다.
    source_quote_probe = str(
        (target_node.get("properties") or {}).get("source_quote") or ""
    ).strip() or original_statement
    if source_quote_probe and source_quote_probe not in chunk_text:
        for candidate_text in chunk_text_lookup.values():
            if source_quote_probe in candidate_text:
                chunk_text = candidate_text
                break
        else:
            tokens = _value_tokens(source_quote_probe)
            probe_token = max(tokens, key=len, default="")
            if len(probe_token) >= 4:
                for candidate_text in chunk_text_lookup.values():
                    if probe_token in candidate_text:
                        chunk_text = candidate_text
                        break
    related_statements, related_chunks = _related_context(
        node_by_id=node_by_id, edges=edges or [],
        target_node_id=str(target_node_id),
        chunk_text_lookup=chunk_text_lookup, exclude_chunk=target_chunk,
    )

    result["original_chunk_text"] = chunk_text
    source_quote = str((target_node.get("properties") or {}).get("source_quote") or "").strip()
    target_rewrite_text = _choose_target_rewrite_text(
        original_statement=original_statement,
        source_quote=source_quote,
        chunk_text=chunk_text,
    )
    result["original_quote_text"] = target_rewrite_text
    result["suggested_quote"] = ""
    result["quote_edit_source"] = "none"

    try:
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as cli:
            if corrected_statement and corrected_statement.strip():
                # 호출자가 교정 statement 를 지정한 경우: 판정은 건너뛰고
                # 보고서 문장 재작성만 수행한다 (사용자 편집 후 재생성용).
                result["corrected_statement"] = corrected_statement.strip()
                result["changed"] = corrected_statement.strip() != original_statement
            else:
                judged = await _judge_atom_with_client(
                    cli, base=base, api_key=api_key, model=model,
                    target_node=target_node, chunk_text=chunk_text,
                    related_statements=related_statements,
                    verified_correction_hint=None,
                )
                result.update(
                    corrected_statement=judged["corrected_statement"],
                    reason=judged["reason"],
                    changed=judged["changed"],
                    online=judged["online"],
                    error=judged["error"],
                )
                if result["changed"]:
                    result["correction_source"] = "llm"
                for key in ("needs_review", "rejected_corrected_statement", "rejection_reason"):
                    if key in judged:
                        result[key] = judged[key]
            # 보고서 원문(청크)에 대한 교정 문장: LLM 이 (교정 fact + 원문 +
            # 그래프 연결 맥락)으로 최소 수정 재작성 — '보고서 교정 문장' 기본값.
            if result["changed"] and target_rewrite_text:
                try:
                    data = await _post_json(
                        cli, base=base, api_key=api_key, model=model,
                        system="You are a precise financial copy editor. Output JSON only.",
                        prompt=_target_chunk_rewrite_prompt(
                            original_statement=original_statement,
                            corrected_statement=str(result["corrected_statement"]),
                            chunk_text=target_rewrite_text,
                            section=_node_section(target_node),
                            related_statements=related_statements,
                            related_chunks=([chunk_text[:2000]] if chunk_text and chunk_text != target_rewrite_text else []) + related_chunks,
                        ),
                        max_tokens=_TARGET_REWRITE_MAX_TOKENS,
                        schema_kind="rewrite",
                    )
                    result["online"] = True
                    suggested = _flatten_nested_export_source_parentheses(
                        str(data.get("suggested_text") or "").strip()
                    )
                    suggested, accepted, rejection_reason = _validate_rewrite_suggestion(
                        original_text=target_rewrite_text,
                        suggested_text=suggested,
                        corrected_statement=str(result["corrected_statement"]),
                    )
                    if accepted:
                        result["suggested_quote"] = suggested
                        result["quote_edit_source"] = "llm"
                        result["quote_edit_reason"] = str(data.get("reason") or "").strip()
                    elif rejection_reason:
                        result["quote_edit_rejection_reason"] = rejection_reason
                except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError,
                        json.JSONDecodeError) as exc:
                    result["error"] = result["error"] or f"보고서 문장 재작성 실패: {exc}"
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        result["error"] = f"LLM 연결 실패: {exc}"

    if result["changed"] and not _is_actionable_document_correction(
        original_statement=original_statement,
        corrected_statement=str(result["corrected_statement"]),
        target_text=target_rewrite_text,
        suggested_quote=str(result.get("suggested_quote") or ""),
    ):
        result["needs_review"] = True
        result["rejected_corrected_statement"] = result["corrected_statement"]
        result["rejection_reason"] = (
            "교정 statement가 문서 원문 대상에 안정적으로 매핑되지 않아 자동 문서 교정으로 채택하지 않음"
        )
        result["corrected_statement"] = original_statement
        result["changed"] = False
        result.pop("correction_source", None)
    if result["changed"] and _has_unresolved_export_unit(
        str(result.get("suggested_quote") or result.get("corrected_statement") or "")
    ):
        result["needs_review"] = True
        result["rejected_corrected_statement"] = result["corrected_statement"]
        result["rejection_reason"] = (
            "교정 후에도 천달러/만 단위 혼용 표현이 남아 자동 문서 교정으로 채택하지 않음"
        )
        result["corrected_statement"] = original_statement
        result["suggested_quote"] = ""
        result["changed"] = False
        result["quote_edit_source"] = "none"
        result.pop("correction_source", None)
    return result


async def review_applied_atom_correction(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    target_node_id: str,
    original_text: str,
    corrected_text: str,
    markdown_text: str,
    review_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a second, approval-only check for an already-applied atom edit.

    This call is deliberately not allowed to propose another rewrite. It can
    only approve or reject the exact edit that the first pass produced. Any
    transport or parse error fails closed so the caller can restore the
    original text and route the edit to manual review. Some safe literal-scale
    edits do not have a FactReasoner atom; those are reviewed from the exact
    edit and document context instead of being rejected solely for lacking a
    graph node.
    """
    node_by_id = {str(node.get("id")): node for node in (nodes or []) if node.get("id")}
    target_node = node_by_id.get(str(target_node_id)) if target_node_id else None
    result: dict[str, Any] = {
        "node_id": str(target_node_id),
        "approve": False,
        "online": False,
        "error": None,
        "reason": "",
        "model": factreasoner_review_model(),
    }
    if target_node is not None:
        props = target_node.get("properties") or {}
        target_statement = _node_statement(target_node)
        source_quote = str(props.get("source_quote") or "").strip()
        related_statements, _ = _related_context(
            node_by_id=node_by_id,
            edges=edges or [],
            target_node_id=str(target_node_id),
            chunk_text_lookup={},
            exclude_chunk=_node_chunk_id(target_node),
        )
        graph_context_note = "FactReasoner atom과 관련 근거를 사용할 수 있습니다."
    else:
        props = {}
        target_statement = ""
        source_quote = ""
        related_statements = []
        graph_context_note = (
            "이 변경에는 연결된 FactReasoner atom이 없습니다. exact edit와 문서 맥락만으로 "
            "보수적으로 검토하고, 근거가 부족하면 거부하세요."
        )
    corrected_document_context = str(markdown_text or "")[:3000]
    original_document_context = corrected_document_context
    if original_text and corrected_text and corrected_text in original_document_context:
        original_document_context = original_document_context.replace(corrected_text, original_text, 1)
    prompt = {
        "atom": {
            "statement": target_statement,
            "source_quote": source_quote,
            "related_atoms": related_statements[:12],
        },
        "proposed_edit": {
            "original_text": str(original_text or ""),
            "corrected_text": str(corrected_text or ""),
        },
        "document_context": {
            "original": original_document_context,
            "corrected": corrected_document_context,
        },
        "correction_metadata": dict(review_context or {}),
        "instruction": (
            f"{graph_context_note} 이미 적용된 재무 Markdown 교정을 사후 검토하세요. "
            "교정문을 새로 만들거나 수정하지 말고 "
            "주어진 exact edit만 승인/거부하세요. source_quote와 related_atoms로 metric·기간·단위가 "
            "같은 사실을 가리키는지, 숫자·단위가 보존되는지, 원문에 없는 사실을 추가하지 않았는지 확인하세요. "
            "숫자%숫자처럼 손상된 금액, 영업이익과 영업이익률의 metric 혼동, 근거 없는 자릿수 변경은 거부하세요. "
            'JSON으로만 출력: {"approve": boolean, "reason": string}. '
            "확신이 없거나 근거가 부족하면 approve=false로 하세요."
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=_APPLIED_REREVIEW_TIMEOUT_SECONDS) as cli:
            data = await _post_json(
                cli,
                base=factreasoner_base_url().rstrip("/"),
                api_key=factreasoner_api_key(),
                model=factreasoner_review_model(),
                system="You are a conservative financial correction auditor. Output JSON only.",
                prompt=prompt,
                max_tokens=_APPLIED_REREVIEW_MAX_TOKENS,
                schema_kind="approval",
            )
        raw_approve = data.get("approve")
        if isinstance(raw_approve, bool):
            approve = raw_approve
        else:
            approve = str(raw_approve or "").strip().lower() in {"true", "yes", "approve", "approved"}
        result.update(
            approve=approve,
            online=True,
            reason=str(data.get("reason") or "").strip(),
        )
    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = f"자동 교정 재검토 실패: {exc}"
    return result


async def review_llm_corrected_markdown(
    *,
    original_markdown: str,
    corrected_markdown: str,
    corrections: list[dict[str, Any]] | None = None,
    fact_judgments: list[dict[str, Any]] | None = None,
    consensus: dict[str, Any] | None = None,
    arithmetic_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Approve only the exact changed spans and their nearby context.

    The reviewer intentionally does not receive either full Markdown document.
    Sending both copies made a no-op review duplicate a large report and, in
    practice, could exceed the model context before the reviewer emitted JSON.
    """
    result: dict[str, Any] = {
        "approve": False,
        "online": False,
        "error": None,
        "reason": "",
        "model": factreasoner_review_model(),
    }
    comparisons = _build_rereview_comparisons(
        original_markdown=str(original_markdown or ""),
        corrected_markdown=str(corrected_markdown or ""),
        corrections=list(corrections or []),
        fact_judgments=list(fact_judgments or []),
    )
    if not comparisons:
        result.update(
            approve=True,
            skipped=True,
            reason="적용된 교정 구간이 없어 최종 재검토를 건너뛰었습니다.",
        )
        return result
    prompt = {
        "correction_comparisons": comparisons,
        "consensus": _compact_rereview_consensus(consensus),
        "arithmetic_guard": _compact_arithmetic_guard(arithmetic_guard),
        "instruction": (
            "전체 문서가 아니라 correction_comparisons에 있는 각 변경 구간만 최종 승인 검토하세요. "
            "각 original_text가 corrected_text로 바뀌는 것이 타당한지, 주변 문맥의 metric·기간·단위가 "
            "보존되는지, 자릿수 변경 근거가 있는지 확인하세요. 새 교정문을 만들거나 제안하지 말고 "
            "주어진 변경을 승인/거부만 하세요. arithmetic_guard.lock이 true이거나 identity_ok가 true이면 "
            "상반기+하반기=연간 합은 이미 검증된 것이므로 그 이유로 approve=false 하지 마세요. "
            "부호 있는 합(-406+610=204)도 유효합니다. 재검토는 톤·근거·문맥 보존만 보고, 잔차가 크면 "
            "reason에 residual을 적되 합이 맞으면 산술 때문에 거부하지 마세요. 하나라도 근거가 부족하거나 "
            "문맥이 깨졌으면 approve=false로 하세요. "
            'JSON으로만 출력: {"approve": boolean, "reason": string}. '
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=_APPLIED_REREVIEW_TIMEOUT_SECONDS) as cli:
            data = await _post_json(
                cli,
                base=factreasoner_base_url().rstrip("/"),
                api_key=factreasoner_api_key(),
                model=factreasoner_review_model(),
                system="You are a conservative financial Markdown approval auditor. Output JSON only.",
                prompt=prompt,
                max_tokens=_APPLIED_REREVIEW_MAX_TOKENS,
                schema_kind="approval",
            )
        raw_approve = data.get("approve")
        approve = raw_approve if isinstance(raw_approve, bool) else str(raw_approve or "").strip().lower() in {
            "true", "yes", "approve", "approved",
        }
        result.update(
            approve=bool(approve),
            online=True,
            reason=str(data.get("reason") or "").strip(),
        )
    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = f"LLM 최종 교정 검토 실패: {exc}"
    return result


def _compact_arithmetic_guard(guard: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only identity facts the reviewer must not overturn."""
    raw = guard if isinstance(guard, dict) else {}
    identities = raw.get("identities") if isinstance(raw.get("identities"), dict) else {}
    compact_identities = {}
    for year, report in list(identities.items())[:8]:
        if not isinstance(report, dict):
            continue
        compact_identities[str(year)] = {
            "h1": report.get("h1"),
            "h2": report.get("h2"),
            "annual": report.get("annual"),
            "sum": report.get("sum"),
            "identity_ok": report.get("identity_ok"),
            "residual": report.get("residual"),
        }
    compact: dict[str, Any] = {
        "lock": bool(raw.get("lock")),
        "manual_review": bool(raw.get("manual_review")),
        "identities": compact_identities,
    }
    if raw.get("max_residual") is not None:
        compact["max_residual"] = raw.get("max_residual")
    return compact


def _compact_rereview_consensus(consensus: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only consensus facts needed to review a numeric edit."""
    raw = consensus if isinstance(consensus, dict) else {}
    compact: dict[str, Any] = {}
    for key in ("value_won", "source", "consensus_won", "consensus_source"):
        if raw.get(key) is not None:
            compact[key] = raw.get(key)
    extraction = raw.get("extraction")
    if isinstance(extraction, dict):
        blocks = extraction.get("blocks")
        if isinstance(blocks, list):
            compact["blocks"] = [
                {
                    "forecast_year": block.get("forecast_year"),
                    "latest": {
                        "date": (block.get("latest") or {}).get("date"),
                        "amount_won": (block.get("latest") or {}).get("amount_won"),
                    },
                }
                for block in blocks[:4]
                if isinstance(block, dict)
            ]
    return compact


def _review_change_pairs(corrections: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
    """Flatten scale ``changes`` and direct atom/cascade edit records."""
    pairs: list[tuple[dict[str, Any], str, str]] = []
    for parent in corrections:
        if not isinstance(parent, dict):
            continue
        nested = parent.get("changes")
        entries = nested if isinstance(nested, list) and nested else [parent]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            original = str(
                entry.get("original")
                or entry.get("original_text")
                or entry.get("old_line")
                or entry.get("old_text")
                or ""
            ).strip()
            corrected = str(
                entry.get("corrected")
                or entry.get("corrected_text")
                or entry.get("new_line")
                or entry.get("new_text")
                or entry.get("suggested_text")
                or ""
            ).strip()
            if original and corrected and original != corrected:
                metadata = {
                    key: parent.get(key)
                    for key in (
                        "kind", "node_id", "pin_node_id", "mapping_id", "chunk_id", "direction",
                        "year", "period", "label", "factor", "reason", "source",
                    )
                    if parent.get(key) is not None
                }
                metadata.update({
                    key: entry.get(key)
                    for key in ("line", "label", "factor", "reason")
                    if entry.get(key) is not None
                })
                pairs.append((metadata, original, corrected))
                if len(pairs) >= _MAX_REREVIEW_COMPARISONS:
                    return pairs
    return pairs


def _nearby_context(text: str, needle: str) -> dict[str, str]:
    """Return bounded before/after context without duplicating the full span."""
    position = text.find(needle)
    if position < 0:
        return {"before": "", "after": ""}
    return {
        "before": text[max(0, position - _REREVIEW_CONTEXT_CHARS):position],
        "after": text[position + len(needle):position + len(needle) + _REREVIEW_CONTEXT_CHARS],
    }


def _build_rereview_comparisons(
    *,
    original_markdown: str,
    corrected_markdown: str,
    corrections: list[dict[str, Any]],
    fact_judgments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create a compact diff-only payload for the approval LLM."""
    comparisons: list[dict[str, Any]] = []
    for metadata, original, corrected in _review_change_pairs(corrections):
        related: list[dict[str, Any]] = []
        ids = {
            str(metadata.get("node_id") or ""),
            str(metadata.get("pin_node_id") or ""),
        } - {""}
        for judgment in fact_judgments:
            if not isinstance(judgment, dict):
                continue
            judgment_id = str(judgment.get("node_id") or "")
            statement = str(judgment.get("text") or judgment.get("statement") or "")
            if (ids and judgment_id in ids) or (statement and (statement in original or statement in corrected)):
                related.append({
                    key: judgment.get(key)
                    for key in ("node_id", "text", "verdict", "reason")
                    if judgment.get(key) is not None
                })
                if len(related) >= 3:
                    break
        comparisons.append({
            "original_text": original[:3000],
            "corrected_text": corrected[:3000],
            "original_context": _nearby_context(original_markdown, original),
            "corrected_context": _nearby_context(corrected_markdown, corrected),
            "metadata": metadata,
            "related_fact_judgments": related,
        })
    return comparisons


async def propagate_correction(
    *,
    markdown_text: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    target_node_id: str,
    corrected_statement: str | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    base = factreasoner_base_url().rstrip("/")
    api_key = factreasoner_api_key()
    model = factreasoner_correction_model()
    text = markdown_text or ""

    result: dict[str, Any] = {
        "model": model,
        "base_url": base,
        "online": False,
        "ran_llm": False,
        "error": None,
        "target": None,
        "propagations": [],
    }

    node_by_id = {str(node.get("id")): node for node in (nodes or []) if node.get("id")}
    target_node = node_by_id.get(str(target_node_id))
    if target_node is None:
        result["error"] = f"target_node_id '{target_node_id}' 를 그래프에서 찾을 수 없습니다."
        return result

    original_statement = _node_statement(target_node)
    target_chunk_id = _node_chunk_id(target_node)
    source_quote = str((target_node.get("properties") or {}).get("source_quote") or "").strip()
    corrected = (corrected_statement or "").strip()

    chunk_text_by_id: dict[str, dict[str, str]] = {}
    for chunk in _document_chunks(text):
        # raw_text is the chunk's ORIGINAL report text; rewrites anchored on it
        # can be applied to the document verbatim (table rows especially)
        chunk_text_by_id[str(chunk.get("id"))] = {
            "text": str(chunk.get("raw_text") or chunk.get("text") or ""),
            "section": str(chunk.get("section") or ""),
        }

    result["target"] = {
        "node_id": str(target_node.get("id")),
        "chunk_id": target_chunk_id,
        "section": _node_section(target_node),
        "original_statement": original_statement,
        "corrected_statement": corrected,
        "source_quote": source_quote,
    }

    downstream_adj, upstream_adj = _build_adjacency(edges)
    errors: list[str] = []

    # Per-chunk judgement cache + reached metadata. Propagation walks causal
    # edges, but only expands *past* a chunk when the correction actually
    # changes it. An unaffected chunk is a boundary: the rest of that branch
    # (further nodes/edges) is pruned and never judged.
    chunk_judgment: dict[str, dict[str, Any]] = {}
    chunk_directions: dict[str, set[str]] = {}
    chunk_atoms: dict[str, dict[str, str]] = {}

    async def judge_chunk(cli: httpx.AsyncClient | None, chunk_id: str) -> dict[str, Any]:
        cached = chunk_judgment.get(chunk_id)
        if cached is not None:
            return cached
        chunk_doc = chunk_text_by_id.get(chunk_id, {})
        chunk_text = chunk_doc.get("text", "")
        atoms_map = chunk_atoms.get(chunk_id, {})
        propagation: dict[str, Any] = {
            "chunk_id": chunk_id,
            "section": chunk_doc.get("section", ""),
            "direction": "downstream",
            "affected_atom_ids": list(atoms_map.keys()),
            "atoms": [{"id": nid, "statement": st} for nid, st in atoms_map.items()],
            "original_text": chunk_text,
            "suggested_text": "",
            "reason": "",
            "affected": False,
            "needs_manual": False,
        }
        if not chunk_text:
            propagation["reason"] = "chunk 원문 텍스트를 찾을 수 없습니다."
            propagation["needs_manual"] = True
            chunk_judgment[chunk_id] = propagation
            return propagation
        used_llm = False
        if cli is not None:
            try:
                data = await _post_json(
                    cli,
                    base=base,
                    api_key=api_key,
                    model=model,
                    system="You are a precise financial fact checker. Output JSON only.",
                    prompt=_chunk_prompt(
                        original_statement=original_statement,
                        corrected_statement=corrected,
                        chunk=propagation,
                        chunk_text=chunk_text,
                    ),
                    max_tokens=8192,
                    schema_kind="propagation",
                )
                result["online"] = True
                result["ran_llm"] = True
                affected_flag = bool(data.get("affected"))
                suggested = str(data.get("suggested_text") or "").strip()
                propagation["affected"] = affected_flag
                propagation["suggested_text"] = suggested if affected_flag else ""
                propagation["reason"] = str(data.get("reason") or "").strip() or (
                    "LLM 판단: 영향 없음" if not affected_flag else ""
                )
                used_llm = True
            except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"chunk {chunk_id} 교정 실패: {exc}")
        if not used_llm:
            propagation["affected"] = False
            propagation["suggested_text"] = ""
            propagation["needs_manual"] = True
            propagation["reason"] = "FactReasoner LLM corrector가 응답하지 않아 수동 검토가 필요합니다."
        chunk_judgment[chunk_id] = propagation
        return propagation

    async def propagate_branch(
        cli: httpx.AsyncClient | None, adjacency: dict[str, set[str]], direction: str
    ) -> None:
        start = str(target_node_id)
        visited = {start}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        while queue:
            node_id, depth = queue.popleft()
            node = node_by_id.get(node_id)
            chunk_id = _node_chunk_id(node) if node else ""
            # The target's own chunk (and atoms without a chunk) are the source
            # of the change, so they always propagate.
            origin = node_id == start or not chunk_id or chunk_id == target_chunk_id
            if not origin:
                chunk_atoms.setdefault(chunk_id, {})[node_id] = _node_statement(node)
                chunk_directions.setdefault(chunk_id, set()).add(direction)
            if origin:
                chunk_affected = True
            elif chunk_id not in chunk_judgment and len(chunk_judgment) >= _MAX_AFFECTED_CHUNKS:
                # Judgement budget exhausted: stop expanding new chunks.
                continue
            else:
                judged = await judge_chunk(cli, chunk_id)
                chunk_affected = bool(judged["affected"] or judged["needs_manual"])
            if not chunk_affected:
                # Unaffected boundary chunk: do not propagate past it.
                continue
            if max_depth is not None and depth >= max_depth:
                continue
            for nxt in adjacency.get(node_id, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, depth + 1))

    target_chunk_doc = chunk_text_by_id.get(target_chunk_id, {})
    target_chunk_text = target_chunk_doc.get("text", "")
    result["target"]["original_chunk_text"] = target_chunk_text
    rewrite_related_statements, rewrite_related_chunks = _related_context(
        node_by_id=node_by_id, edges=edges or [],
        target_node_id=str(target_node_id),
        chunk_text_lookup={
            cid: doc.get("text", "") for cid, doc in chunk_text_by_id.items()
        },
        exclude_chunk=target_chunk_id,
    )

    async def rewrite_target_chunk(cli: httpx.AsyncClient) -> None:
        """The corrected TEXT is authored by the LLM from (corrected fact,
        original chunk, graph-retrieved related context): it rewrites the
        report's own sentence minimally so wording that depends on the fact
        (e.g. '개선되었다') follows the new value."""
        if not target_chunk_text or not corrected or corrected == original_statement:
            return
        try:
            data = await _post_json(
                cli, base=base, api_key=api_key, model=model,
                system="You are a precise financial copy editor. Output JSON only.",
                prompt=_target_chunk_rewrite_prompt(
                    original_statement=original_statement,
                    corrected_statement=corrected,
                    chunk_text=target_chunk_text,
                    section=_node_section(target_node),
                    related_statements=rewrite_related_statements,
                    related_chunks=rewrite_related_chunks,
                ),
                max_tokens=8192,
                schema_kind="rewrite",
            )
            result["online"] = True
            result["ran_llm"] = True
            suggested = str(data.get("suggested_text") or "").strip()
            if suggested and suggested != target_chunk_text:
                result["target"]["suggested_quote"] = suggested
                result["target"]["quote_edit_applied"] = True
                result["target"]["quote_edit_source"] = "llm"
                result["target"]["quote_edit_reason"] = str(data.get("reason") or "").strip()
        except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"target 청크 재작성 실패: {exc}")

    try:
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as cli:
            if not corrected:
                judged = await _judge_atom_with_client(
                    cli, base=base, api_key=api_key, model=model,
                    target_node=target_node, chunk_text=target_chunk_text,
                )
                if judged["online"]:
                    result["online"] = True
                    result["ran_llm"] = True
                if judged["error"]:
                    errors.append(judged["error"])
                corrected = judged["corrected_statement"] or original_statement
                result["target"]["corrected_statement"] = corrected
                result["target"]["correction_reason"] = judged["reason"]
            await rewrite_target_chunk(cli)
            await propagate_branch(cli, downstream_adj, "downstream")
            await propagate_branch(cli, upstream_adj, "upstream")
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        errors.append(f"LLM 연결 실패: {exc}")

    if not corrected:
        corrected = original_statement
        result["target"]["corrected_statement"] = corrected

    if not result["target"].get("suggested_quote"):
        result["target"]["suggested_quote"] = ""
        result["target"]["quote_edit_applied"] = False
        result["target"]["quote_edit_source"] = "none"

    # Proposal-only backward repair: when the corrected atom is a derived
    # ratio, surface the ranked multi-gold premise revisions instead of
    # silently committing one upstream rewrite. LLM chunk suggestions above
    # remain available; this adds the verified alternatives for the author.
    try:
        premise = propose_premise_revisions(
            nodes=nodes, edges=edges, target_node_id=str(target_node_id),
            corrected_statement=corrected,
        )
        if premise:
            result["premise_proposals"] = premise
    except (ValueError, KeyError, ZeroDivisionError) as exc:
        errors.append(f"전제 수정 제안 실패: {exc}")

    # Materialise per-chunk results with merged direction + reached atoms.
    propagations: list[dict[str, Any]] = []
    for chunk_id, propagation in chunk_judgment.items():
        atoms_map = chunk_atoms.get(chunk_id, {})
        propagation["atoms"] = [{"id": nid, "statement": st} for nid, st in atoms_map.items()]
        propagation["affected_atom_ids"] = list(atoms_map.keys())
        directions = chunk_directions.get(chunk_id, set())
        if {"downstream", "upstream"} <= directions:
            propagation["direction"] = "both"
        elif "downstream" in directions:
            propagation["direction"] = "downstream"
        elif "upstream" in directions:
            propagation["direction"] = "upstream"
        propagations.append(propagation)

    _order = {"upstream": 0, "both": 1, "downstream": 2}
    propagations.sort(key=lambda item: (_order.get(item["direction"], 9), item["chunk_id"]))
    result["propagations"] = propagations[:_MAX_AFFECTED_CHUNKS]

    if errors:
        result["error"] = " / ".join(errors[:6])
    return result
