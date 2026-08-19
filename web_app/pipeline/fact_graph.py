"""LLM fact-atom graph extraction for fact correction reports."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
import ast
import json
import os
import re
import time

import httpx

from hallucination_verifier.llm_config import (
    factreasoner_api_key,
    factreasoner_base_url,
    factreasoner_graph_model,
    with_local_chat_template,
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
from web_app.pipeline.fact_pair_miner import (
    alias_link_edges,
    configured_nli_mode,
    expand_alias_edges,
    get_cached_verdict,
    mine_relation_pairs,
    put_cached_verdict,
    verdict_cache_key,
)

_MAX_CHUNK_CHARS = 20000
_MAX_SEED_FACTS = 80
_MAX_RELATION_PROMPT_ATOMS = 128
_MAX_RELATION_PROMPT_CHARS = 24000
_MAX_RELATION_PROMPT_EDGES = 200
# Output budgets: the served model has a 262k context, so give extraction
# calls room to emit every atom instead of truncating dense chunks.
_ATOM_MAX_TOKENS = 8192
_ATOM_RETRY_MAX_TOKENS = 4096
_RELATION_MAX_TOKENS = 4096
_LLM_HTTP_TIMEOUT = 300.0
# Concurrent chunk/relation calls fill vLLM continuous batching (FlashInfer).
_EXTRACTION_WORKERS = max(1, int(os.getenv("FACT_GRAPH_EXTRACTION_WORKERS", "16")))
_HTTP_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=32)
_ALLOWED_RELATIONS = {
    "supports",
    "contradicts",
    "depends_on",
    "causes",
    "same_metric",
    "same_period",
    "derived_from",
}
_FACT_UNIT_SCHEMA: list[dict[str, Any]] = [
    {
        "unit_type": "metric_value",
        "required_fields": ["metric", "value"],
        "optional_fields": ["subject", "period", "unit"],
        "mandatory_when": "table cell or sentence contains a recognized metric and an explicit numeric/percentage/money value",
    },
    {
        "unit_type": "forecast_or_opinion",
        "required_fields": ["statement"],
        "optional_fields": ["subject", "period", "metric", "value"],
        "mandatory_when": "sentence states forecast, outlook, target price, investment opinion, recommendation, or guidance",
    },
    {
        "unit_type": "trend_or_comparison",
        "required_fields": ["statement"],
        "optional_fields": ["subject", "period", "metric", "value"],
        "mandatory_when": "sentence states increase/decrease, QoQ/YoY, above/below, improvement/deterioration, or consecutive trend",
    },
    {
        "unit_type": "causal_driver",
        "required_fields": ["statement"],
        "optional_fields": ["subject", "period", "metric"],
        "mandatory_when": "sentence states cause, driver, basis, premise, dependency, or reason",
    },
]
_KNOWN_METRICS = (
    "목표주가", "투자의견", "매출액", "매출", "영업이익률", "영업이익", "순이익", "당기순이익",
    "EPS", "BPS", "PER", "PBR", "ROE", "OPM", "컨센서스", "주가", "시가총액",
)
_MIXED_AMOUNT_RE = re.compile(
    r"(?P<major>[-+]?\d[\d,]*(?:\.\d+)?)\s*조\s*"
    r"(?P<minor>\d[\d,]*(?:\.\d+)?)\s*억(?:원)?"
)
# Keep a mixed ``조 + 억원`` amount together.  Without this alternative,
# ``97조1467억원`` becomes ``97조`` and ``1467억원`` and both values are
# incorrectly attached to every nearby metric.
_VALUE_RE = re.compile(
    rf"(?:{_MIXED_AMOUNT_RE.pattern}|"
    r"[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|조원|억원|원|배|십억원|백만원|만원|조|억)?"
    r")(?:\s*\([A-Za-z가-힣]+\))?"
)
# A bare four-digit year is allowed, but a four-digit amount component such as
# ``2063억원`` must not become a period.
_PERIOD_RE = re.compile(
    r"(?<![\d,])20\d{2}(?:년|/Q[1-4])"
    r"|(?<![\d,])20\d{2}(?!\d)(?!\s*(?:조원|억원|십억원|백만원|만원|원|조|억))"
    r"|(?:[1-4]Q\d{2})"
    r"|(?:20\d{2}\s*년\s*[1-4]\s*분기)"
)
_MALFORMED_PERCENT_AMOUNT_RE = re.compile(r"(?<![\d])\d[\d,]*\s*%\s*\d")
_FORECAST_MARKERS = re.compile(r"전망|예상|추정|가이던스|목표주가|투자의견|매수|중립|매도|유지|상향|하향|recommendation|target price|outlook|guidance", re.IGNORECASE)
_TREND_MARKERS = re.compile(r"증가|감소|개선|악화|상회|하회|QoQ|YoY|qoq|yoy|연속|대비|상승|하락")
_CAUSAL_MARKERS = re.compile(r"때문|영향|기인|전제|기반|근거|요인|driver|due to|because|based on", re.IGNORECASE)


def extract_fact_atom_graph(
    *,
    markdown_text: str,
    seed_facts: list[dict[str, Any]] | None = None,
    llm_only: bool = False,
    nli_mode: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Extract atomic facts and atom-to-atom relations with an LLM.

    The function is deliberately offline-safe: transport and parse failures
    return a fallback graph derived from existing finding atoms instead of
    raising. When ``llm_only`` is true, deterministic mandatory atoms,
    suspect annotations, relation edges, and fallback atoms are disabled;
    an LLM failure returns an empty graph with ``error`` instead.
    """
    text = str(markdown_text or "").strip()
    base = factreasoner_base_url().rstrip("/")
    model = factreasoner_graph_model()
    requested_nli_mode = configured_nli_mode(nli_mode)
    result: dict[str, Any] = {
        "enabled": True,
        "mode": "llm",
        "online": False,
        "model": model,
        "base_url": base,
        "fact_unit_schema": _FACT_UNIT_SCHEMA,
        "nodes": [],
        "edges": [],
        "warnings": [],
        "error": None,
        "nli_mode": requested_nli_mode,
    }
    if not text:
        result["mode"] = "empty"
        result["error"] = "분석할 텍스트가 없습니다."
        return result

    chunks = _document_chunks(text)

    def emit_progress(**event: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event)
        except Exception:
            # Progress is observability only and must never fail graph creation.
            return

    mandatory_atoms = [] if llm_only else _mandatory_atoms_from_chunks(chunks)
    graph_started = time.monotonic()
    try:
        warnings: list[str] = []
        with httpx.Client(timeout=_LLM_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as cli:
            headers = {
                "Authorization": f"Bearer {factreasoner_api_key()}",
                "Content-Type": "application/json",
            }
            chunk_atoms: list[dict[str, Any]] = list(mandatory_atoms)
            llm_chunks: list[tuple[dict[str, Any], bool]] = []
            for chunk in chunks:
                chunk_mandatory = [
                    atom for atom in mandatory_atoms
                    if atom.get("chunk_id") == chunk.get("id")
                ]
                if _chunk_llm_not_needed(chunk, chunk_mandatory):
                    continue
                llm_chunks.append((chunk, bool(chunk_mandatory)))

            chunk_units = [
                max(1, len(_split_sentences(str(chunk.get("text") or ""))))
                for chunk, _ in llm_chunks
            ]
            total_units = sum(chunk_units)
            emit_progress(
                phase="atom_extraction", completed=0, total=total_units,
                chunks_completed=0, chunks_total=len(llm_chunks),
                message=(
                    f"Fact atom 추출: 문장/표 행 0/{total_units} "
                    f"(묶음 0/{len(llm_chunks)}, 최대 {_EXTRACTION_WORKERS}개 병렬)"
                ),
            )

            # One Client per worker: a shared httpx.Client serializes HTTP/1.1
            # requests, so vLLM never sees a batch even with many threads.
            def _chunk_worker(item: tuple[dict[str, Any], bool]) -> list[dict[str, Any]]:
                chunk, has_mandatory = item
                with httpx.Client(timeout=_LLM_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as worker_cli:
                    return _extract_chunk_atoms_with_retry(
                        worker_cli, base=base, headers=headers, model=model, chunk=chunk,
                        seed_facts=seed_facts or [], warnings=warnings,
                        has_mandatory=has_mandatory,
                    )

            atom_started = time.monotonic()
            if llm_chunks:
                with ThreadPoolExecutor(max_workers=_EXTRACTION_WORKERS) as pool:
                    futures = {
                        pool.submit(_chunk_worker, item): index
                        for index, item in enumerate(llm_chunks)
                    }
                    atoms_by_chunk: list[list[dict[str, Any]]] = [[] for _ in llm_chunks]
                    completed_units = 0
                    completed_chunks = 0
                    for future in as_completed(futures):
                        index = futures[future]
                        atoms_by_chunk[index] = future.result()
                        completed_units += chunk_units[index]
                        completed_chunks += 1
                        emit_progress(
                            phase="atom_extraction", completed=completed_units, total=total_units,
                            chunks_completed=completed_chunks, chunks_total=len(llm_chunks),
                            message=(
                                f"Fact atom 추출: 문장/표 행 {completed_units}/{total_units} "
                                f"(묶음 {completed_chunks}/{len(llm_chunks)})"
                            ),
                        )
                    for atoms in atoms_by_chunk:
                        chunk_atoms.extend(atoms)
            atom_items = _dedupe_atom_items(chunk_atoms)
            emit_progress(
                phase="pair_mining", completed=total_units, total=total_units,
                chunks_completed=len(llm_chunks), chunks_total=len(llm_chunks),
                atoms_total=len(atom_items),
                message=(
                    f"문장/표 행 {total_units}/{total_units} 분석 완료 · "
                    f"Fact atom {len(atom_items)}개, 관계 후보를 준비 중"
                ),
            )
            atom_elapsed_ms = round((time.monotonic() - atom_started) * 1000, 1)
            if not llm_only:
                annotate_deterministic_suspects(atom_items)
            llm_edges: list[dict[str, Any]] = []
            relation_errors: list[str] = []
            relation_error = ""
            nli_stats: dict[str, Any] = {}
            suspect_stats: dict[str, Any] = {}
            if atom_items:
                mining = mine_relation_pairs(atom_items, nli_mode=requested_nli_mode)
                warnings.extend(mining.warnings)
                nli_stats.update(mining.stats)
                by_id = {str(item.get("id") or ""): item for item in atom_items}

                def _relation_worker(batch: Any):
                    try:
                        prompt = (
                            _build_relation_pair_prompt(batch, by_id)
                            if mining.stats.get("effective_mode") == "fast"
                            else _build_relation_prompt(batch)
                        )
                        cache_key = verdict_cache_key("relations", prompt, model=model)
                        cached = get_cached_verdict(cache_key)
                        if cached is not None:
                            return list(cached.get("edges") or []), None, True
                        with httpx.Client(timeout=_LLM_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as worker_cli:
                            relation_data = _post_llm_json(
                                worker_cli,
                                base_url=base,
                                headers=headers,
                                model=model,
                                system="You extract typed relations between supplied fact atoms. Output JSON only.",
                                prompt=prompt,
                                max_tokens=_RELATION_MAX_TOKENS,
                                schema_kind="relations",
                            )
                        if isinstance(relation_data, dict) and isinstance(relation_data.get("edges"), list):
                            put_cached_verdict(cache_key, {"edges": relation_data["edges"]})
                            return relation_data["edges"], None, False
                        return [], None, False
                    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
                        return [], str(exc), False

                batches: list[Any] = (
                    _relation_pair_batches(mining.pairs)
                    if mining.stats.get("effective_mode") == "fast"
                    else _relation_prompt_batches(atom_items)
                )

                def _run_relations() -> tuple[list[dict[str, Any]], list[str], int]:
                    collected: list[dict[str, Any]] = []
                    errors: list[str] = []
                    cache_hits = 0
                    total_batches = len(batches)
                    emit_progress(
                        phase="relations", completed=0, total=total_batches,
                        atoms_total=len(atom_items),
                        message=f"Fact atom {len(atom_items)}개 · 관계 배치 0/{total_batches}",
                    )
                    with ThreadPoolExecutor(max_workers=_EXTRACTION_WORKERS) as pool:
                        futures = {
                            pool.submit(_relation_worker, batch): index
                            for index, batch in enumerate(batches)
                        }
                        results: list[tuple[list[dict[str, Any]], str | None, bool] | None] = [
                            None for _ in batches
                        ]
                        completed_batches = 0
                        for future in as_completed(futures):
                            results[futures[future]] = future.result()
                            completed_batches += 1
                            emit_progress(
                                phase="relations", completed=completed_batches, total=total_batches,
                                atoms_total=len(atom_items),
                                message=(
                                    f"Fact atom {len(atom_items)}개 · "
                                    f"관계 배치 {completed_batches}/{total_batches}"
                                ),
                            )
                        for result_part in results:
                            if result_part is None:
                                continue
                            edges_part, err, cache_hit = result_part
                            collected.extend(edges_part)
                            cache_hits += int(cache_hit)
                            if err:
                                errors.append(err)
                    return collected, errors, cache_hits

                # Suspect auditing and relation mining read the same atom set
                # but are independent; launch both phases together.
                relation_phase_started = time.monotonic()
                with ThreadPoolExecutor(max_workers=2) as phase_pool:
                    suspect_future = phase_pool.submit(
                        annotate_llm_suspects,
                        cli, base=base, headers=headers, model=model,
                        atom_items=atom_items, warnings=warnings,
                    )
                    relation_future = phase_pool.submit(_run_relations)
                    suspect_stats = suspect_future.result()
                    llm_edges, relation_errors, relation_cache_hits = relation_future.result()
                relation_phase_elapsed_ms = round(
                    (time.monotonic() - relation_phase_started) * 1000, 1
                )
                if mining.stats.get("effective_mode") == "fast":
                    llm_edges = [
                        *alias_link_edges(mining.alias_groups),
                        *expand_alias_edges(llm_edges, mining.alias_groups),
                    ]
                nli_stats.update({
                    "relation_batches": len(batches),
                    "relation_cache_hits": relation_cache_hits,
                    "relation_cache_misses": max(0, len(batches) - relation_cache_hits),
                    "suspect": suspect_stats,
                    "phase_timings_ms": {
                        "atom_extraction": atom_elapsed_ms,
                        "suspect_and_relations": relation_phase_elapsed_ms,
                    },
                })
                if relation_errors:
                    relation_error = f"relation LLM parse failed in {len(relation_errors)} batch(es): {relation_errors[0]}"
                emit_progress(
                    phase="graph_assembly", completed=len(batches), total=len(batches),
                    atoms_total=len(atom_items), edges_total=len(llm_edges),
                    message=(
                        f"Fact atom {len(atom_items)}개와 관계 {len(llm_edges)}개를 "
                        "graph로 조립 중"
                    ),
                )
        deterministic_edges = [] if llm_only else _deterministic_relation_edges(atom_items)
        graph = _coerce_fact_atom_graph({"atoms": atom_items, "edges": deterministic_edges + llm_edges})
        result["online"] = True
        result["mode"] = "llm_chunked"
        result["chunks_total"] = len(chunks)
        result["chunks"] = _chunk_payload(chunks)
        result["mandatory_atoms"] = _mandatory_node_count(graph)
        result["warnings"] = warnings
        result["nli_stats"] = nli_stats
        result["nli_stats"]["phase_timings_ms"] = {
            **(result["nli_stats"].get("phase_timings_ms") or {}),
            "fact_graph_total": round((time.monotonic() - graph_started) * 1000, 1),
        }
        if relation_error:
            result["relation_error"] = relation_error
        result.update(graph)
        return result
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        if llm_only:
            result["mode"] = "llm_failed"
            result["error"] = f"LLM fact graph 실패: {exc}"
            result["chunks_total"] = len(chunks)
            result["chunks"] = _chunk_payload(chunks)
            return result
        result["mode"] = "finding_fallback"
        result["error"] = f"LLM fact graph 실패: {exc}"
        fallback = _fallback_graph(seed_facts or [])
        mandatory_graph = _coerce_fact_atom_graph({"atoms": _dedupe_atom_items(mandatory_atoms), "edges": []})
        fallback["nodes"] = _unique_node_ids(mandatory_graph["nodes"] + fallback.get("nodes", []))
        fallback["edges"] = mandatory_graph["edges"]
        result["mandatory_atoms"] = _mandatory_node_count(fallback)
        result.update(fallback)
        return result


def _extract_chunk_atoms_with_retry(
    cli: httpx.Client,
    *,
    base: str,
    headers: dict[str, str],
    model: str,
    chunk: dict[str, Any],
    seed_facts: list[dict[str, Any]],
    warnings: list[str],
    has_mandatory: bool,
) -> list[dict[str, Any]]:
    """Atomize one chunk without losing content on failure.

    Ladder: (1) full prompt with a generous output budget; (2) strict-JSON
    retry with the FULL chunk text; (3) split the chunk into sentences and
    atomize the halves separately. Every rung sees the whole text, so a
    dense chunk degrades to more calls, never to dropped tail content.
    """
    def attempt(prompt: dict[str, Any], *, max_tokens: int, strict: bool,
                system: str) -> list[dict[str, Any]] | None:
        try:
            data = _post_llm_json(
                cli, base_url=base, headers=headers, model=model,
                system=system, prompt=prompt, max_tokens=max_tokens,
                response_format_json=strict,
                schema_kind="atom_graph",
            )
            return _coerce_atom_items(
                data.get("atoms") if isinstance(data, dict) else None,
                source_chunk=chunk,
            )
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
            return None

    atoms = attempt(
        _build_chunk_atom_prompt(chunk, seed_facts), max_tokens=_ATOM_MAX_TOKENS,
        strict=False,
        system="You extract atomic financial facts from one document chunk. Output JSON only.",
    )
    if atoms is not None:
        return atoms
    atoms = attempt(
        _build_chunk_atom_retry_prompt(chunk), max_tokens=_ATOM_RETRY_MAX_TOKENS, strict=True,
        system="Return compact JSON only. No markdown. No explanation.",
    )
    if atoms is not None:
        return atoms

    # last resort: halve by sentences so each call carries less output load
    sentences = _split_sentences(str(chunk.get("text") or ""))
    if len(sentences) >= 2:
        mid = len(sentences) // 2
        collected: list[dict[str, Any]] = []
        recovered = True
        for i, part in enumerate((" ".join(sentences[:mid]), " ".join(sentences[mid:]))):
            sub = dict(chunk)
            sub["text"] = part
            part_atoms = attempt(
                _build_chunk_atom_retry_prompt(sub), max_tokens=_ATOM_RETRY_MAX_TOKENS, strict=True,
                system="Return compact JSON only. No markdown. No explanation.",
            )
            if part_atoms is None:
                recovered = False
                warnings.append(f"{chunk.get('id')}: atom LLM parse failed on split part {i + 1}")
            else:
                collected.extend(part_atoms)
        if collected or recovered:
            return collected
    if not has_mandatory:
        warnings.append(f"{chunk.get('id')}: atom LLM parse failed after retries")
    return []


def extract_fast_fact_atom_graph(*, markdown_text: str) -> dict[str, Any]:
    """Build an immediate dependency graph without LLM calls.

    The output uses the same graph shape as `extract_fact_atom_graph`, but is
    intentionally limited to mandatory metric/value atoms and deterministic
    relations so the UI can paint a graph before the full LLM graph completes.
    """
    text = str(markdown_text or "").strip()
    base = factreasoner_base_url().rstrip("/")
    model = factreasoner_graph_model()
    result: dict[str, Any] = {
        "enabled": True,
        "mode": "fast_deterministic",
        "online": False,
        "model": model,
        "base_url": base,
        "fact_unit_schema": _FACT_UNIT_SCHEMA,
        "nodes": [],
        "edges": [],
        "warnings": [],
        "error": None,
        "preview": True,
    }
    if not text:
        result["mode"] = "empty"
        result["error"] = "분석할 텍스트가 없습니다."
        return result
    chunks = _document_chunks(text)
    atom_items = _dedupe_atom_items(_mandatory_atoms_from_chunks(chunks))
    annotate_deterministic_suspects(atom_items)
    graph = _coerce_fact_atom_graph(
        {
            "atoms": atom_items,
            "edges": _deterministic_relation_edges(atom_items),
        }
    )
    result["chunks_total"] = len(chunks)
    result["chunks"] = _chunk_payload(chunks)
    result["mandatory_atoms"] = _mandatory_node_count(graph)
    result["warnings"] = [
        "빠른 미리보기: LLM atom/edge 추출 전의 결정론적 graph입니다."
    ]
    result.update(graph)
    return result


# 값 불일치 판정 문턱: 반올림·범위 차이는 허용하고 자릿수급 오류만 잡는다
_VALUE_MISMATCH_REL_TOL = 0.20


def _parse_comparable_value(item: dict[str, Any]) -> float | None:
    """단위가 명시된 값만 비교 대상으로 파싱 (금액→억원 정규화, %는 그대로).

    연도(2026 등)나 단위 없는 맨 숫자는 비교 근거가 약해 제외한다 — 이런 값이
    섞이면 '같은 지표·기간 값 불일치'가 과탐된다.
    """
    for source in (str(item.get("value") or ""), str(item.get("statement") or "")):
        amount_value = _parse_amount_eokwon(source)
        if amount_value is not None:
            return amount_value
        pct = _PCT_VALUE_RE.search(source)
        if pct:
            try:
                return float(pct.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _mark_suspect(item: dict[str, Any], reason: str) -> None:
    if item.get("suspect"):
        existing = str(item.get("suspect_reason") or "")
        if reason not in existing:
            item["suspect_reason"] = f"{existing} / {reason}".strip(" /")
        return
    item["suspect"] = True
    item["suspect_reason"] = reason


def annotate_deterministic_suspects(atom_items: list[dict[str, Any]]) -> int:
    """그래프 기반 결정론 의심 판정 (in-place). 반환값: 의심 atom 수.

    1) 같은 지표·같은 기간의 atom 값 불일치 — 자릿수/오타 오류의 전형.
    2) 영업이익률 산식 불일치 — 같은 기간 매출액·영업이익으로 계산한 값과
       표기가 다르면 해당 마진 atom 을 의심.
    """
    # 1) same metric+period, different numeric values.
    #    단위가 명시된 값(금액·%)만 비교하고, 자릿수급 차이(>20%)만 플래그한다.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in atom_items:
        metric = _norm_key(str(item.get("metric") or ""))
        period = _norm_key(str(item.get("period") or ""))
        # period 미표기(표 행 등)끼리도 같은 그룹으로 비교한다 — 한 보고서에서
        # 기간 없이 재진술된 같은 지표는 같은 기간을 가리키는 것이 보통이다.
        if metric and _parse_comparable_value(item) is not None:
            groups.setdefault((metric, period), []).append(item)
    for (metric, _period), members in groups.items():
        values = [_parse_comparable_value(m) for m in members]
        values = [v for v in values if v is not None]
        if len(values) < 2:
            continue
        base = max((abs(v) for v in values), default=0.0)
        if base <= 0:
            continue
        if (max(values) - min(values)) / base > _VALUE_MISMATCH_REL_TOL:
            rendered = ", ".join(f"{v:,.4g}" for v in sorted(set(values)))
            # 산술 중재: 매출액 x 영업이익률로 정합한 값을 판별할 수 있으면
            # 틀린 쪽만 의심 표시하고 정합값을 사유에 명시한다 (LLM 이 반대로
            # 교정하는 것을 막는 결정적 힌트).
            arbitration = _arbitrate_profit_mismatch(metric, members, atom_items)
            if arbitration is not None:
                certified, good_value_text, wrong_members = arbitration
                for member in wrong_members:
                    _mark_suspect(
                        member,
                        f"같은 지표·기간의 값 불일치({rendered}) — 산술 검증"
                        f"(매출액x영업이익률≈{certified:,.0f}억원)상 문서 기재 "
                        f"{good_value_text} 이(가) 정합. 이 atom 의 값이 오류로 "
                        f"의심되며, 교정 시 문서 기재 정합값({good_value_text})을 "
                        f"사용할 것",
                    )
                continue
            for member in members:
                _mark_suspect(member, f"같은 지표·기간의 값 불일치({rendered})")

    # 2) margin formula mismatch within the same period
    for item in atom_items:
        statement = str(item.get("statement") or "")
        if not _is_margin_atom(statement):
            continue
        margin_val = _parse_pct_value(statement)
        if margin_val is None:
            continue
        period = _norm_key(str(item.get("period") or ""))
        same_period = [
            other for other in atom_items
            if other is not item
            and (not period or _norm_key(str(other.get("period") or "")) == period)
        ]
        best: tuple[float, float] | None = None  # (residual, computed)
        for rev in same_period:
            rev_text = str(rev.get("statement") or "")
            if "매출액" not in rev_text:
                continue
            rev_val = _amount_near_keyword(rev_text, r"매출액")
            if not rev_val:
                continue
            for op in same_period:
                op_text = str(op.get("statement") or "")
                if "영업이익" not in op_text or "영업이익률" in op_text:
                    continue
                op_val = _amount_near_keyword(op_text, r"영업이익(?!률)")
                if op_val is None:
                    continue
                computed = op_val / rev_val * 100.0
                residual = abs(computed - margin_val)
                if best is None or residual < best[0]:
                    best = (residual, computed)
        if best is not None and best[0] > 0.5:
            _mark_suspect(
                item,
                f"영업이익률 산식 불일치: 계산값 {best[1]:.1f}% vs 표기 {margin_val:.1f}%",
            )
    return sum(1 for item in atom_items if item.get("suspect"))


def _arbitrate_profit_mismatch(
    metric: str, members: list[dict[str, Any]], atom_items: list[dict[str, Any]],
) -> tuple[float, str, list[dict[str, Any]]] | None:
    """영업이익 값 충돌을 매출액 x 영업이익률 산술로 중재한다.

    정합값과 5% 이내로 맞는 후보가 있고 나머지가 어긋나면
    (산술 정합값, 문서 기재 정합값 문자열, 틀린 멤버들)을 반환. 판별 불가면 None.
    교정은 산술 계산값이 아니라 '문서에 실제 기재된' 정합값을 써야 한다.
    """
    if "영업이익" not in metric or "률" in metric or "율" in metric:
        return None
    revenue = margin = None
    for item in atom_items:
        item_metric = _norm_key(str(item.get("metric") or ""))
        if revenue is None and "매출액" in item_metric:
            revenue = _parse_comparable_value(item)
        if margin is None and ("영업이익률" in item_metric or "영업이익율" in item_metric):
            margin = _parse_comparable_value(item)
    if not revenue or not margin:
        return None
    certified = revenue * margin / 100.0
    if certified <= 0:
        return None
    good = [
        m for m in members
        if abs((_parse_comparable_value(m) or 0.0) - certified) / certified <= 0.05
    ]
    wrong = [m for m in members if m not in good]
    if good and wrong:
        good_value_text = str(
            good[0].get("value") or good[0].get("statement") or ""
        ).strip()
        return certified, good_value_text, wrong
    return None


def _build_suspect_prompt(atom_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "atoms": [
            {
                "id": str(item.get("id") or ""),
                "statement": str(item.get("statement") or "")[:300],
                "source_quote": str(item.get("source_quote") or "")[:300],
                "subject": str(item.get("subject") or ""),
                "metric": str(item.get("metric") or ""),
                "value": str(item.get("value") or ""),
                "unit": str(item.get("unit") or ""),
                "period": str(item.get("period") or ""),
                "unit_type": str(item.get("unit_type") or ""),
            }
            for item in atom_items
        ],
        "instruction": (
            "위 atom 들 사이에서 사실 오류가 의심되는 atom 을 찾으세요. 대상: "
            "(1) 산술 불일치(합계·비율·증감률이 다른 atom 들과 안 맞음), "
            "(2) 같은 지표·기간인데 값이 서로 다른 경우, "
            "(3) 숫자 규모·단위 오류 의심(회사 규모, 업종 상식, 한국 재무제표에서 통상 쓰는 원/억원/조원 "
            "스케일, 주가·목표주가·영업이익·컨센서스의 일반적 범위와 맞지 않는 값), "
            "(4) 자릿수 오류 의심(주변 값 또는 도메인 상식 대비 10배/100배/1000배 차이), "
            "(5) 서로 모순되는 서술, "
            "(6) 수치에서 도출된 전망·판단이 금융 상식상 과도한 경우. "
            "문서 내부 근거가 있으면 우선 사용하고, 내부 근거가 부족해도 LLM의 금융 도메인 지식과 "
            "상식으로 명백히 이상한 숫자는 의심으로 표시하세요. 다만 확실하지 않은 경우에는 "
            "reason 에 '검토 필요'와 불확실성 이유를 명시하세요. "
            "가능하면 correction_hint 에 같은 문장 형식의 교정 후보나 올바른 단위 후보를 쓰세요. "
            '오직 JSON 만 출력: {"suspects":[{"id":"a001","reason":string,'
            '"suspected_error_type":"arithmetic|conflict|magnitude|digit_scale|unit|logic|contradiction|other",'
            '"correction_hint":string}]}. '
            '의심이 없으면 {"suspects":[]}.'
        ),
    }


def annotate_llm_suspects(
    cli: httpx.Client, *, base: str, headers: dict[str, str], model: str,
    atom_items: list[dict[str, Any]], warnings: list[str],
) -> dict[str, Any]:
    """LLM 의심 스윕 (배치·병렬). atom_items 를 in-place 로 표시."""
    by_id = {str(item.get("id") or ""): item for item in atom_items}

    def worker(batch: list[dict[str, Any]]):
        try:
            prompt = _build_suspect_prompt(batch)
            cache_key = verdict_cache_key("suspects", prompt, model=model)
            cached = get_cached_verdict(cache_key)
            if cached is not None:
                return cached.get("suspects"), True
            with httpx.Client(timeout=_LLM_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as worker_cli:
                data = _post_llm_json(
                    worker_cli, base_url=base, headers=headers, model=model,
                    system="You audit financial fact atoms for inconsistencies. Output JSON only.",
                    prompt=prompt,
                    max_tokens=_RELATION_MAX_TOKENS,
                    response_format_json=True,
                    schema_kind="suspects",
                )
            suspects = data.get("suspects") if isinstance(data, dict) else None
            if isinstance(suspects, list):
                put_cached_verdict(cache_key, {"suspects": suspects})
            return suspects, False
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"suspect sweep failed: {exc}")
            return None, False

    batches = _relation_prompt_batches(atom_items)
    marked = 0
    cache_hits = 0
    with ThreadPoolExecutor(max_workers=_EXTRACTION_WORKERS) as pool:
        for suspects, cache_hit in pool.map(worker, batches):
            cache_hits += int(cache_hit)
            for entry in suspects or []:
                if not isinstance(entry, dict):
                    continue
                item = by_id.get(str(entry.get("id") or ""))
                if item is None:
                    continue
                reason = str(entry.get("reason") or "").strip() or "LLM 의심 판정"
                error_type = str(entry.get("suspected_error_type") or "").strip()
                correction_hint = str(entry.get("correction_hint") or "").strip()
                parts = [f"LLM: {reason}"]
                if error_type:
                    parts.append(f"유형={error_type}")
                if correction_hint:
                    parts.append(f"교정 힌트={correction_hint[:500]}")
                _mark_suspect(item, " / ".join(parts))
                marked += 1
    return {
        "batches": len(batches),
        "cache_hits": cache_hits,
        "cache_misses": max(0, len(batches) - cache_hits),
        "marked": marked,
    }


def _chunk_payload(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chunk source passthrough for graph consumers: the ORIGINAL report text
    each atom came from (atoms are normalized reconstructions, so editing the
    report must anchor on the chunk's raw text, not on the atom quote)."""
    return [
        {
            "id": str(c.get("id") or ""),
            "kind": str(c.get("kind") or ""),
            "section": str(c.get("section") or ""),
            "raw_text": str(c.get("raw_text") or c.get("text") or ""),
        }
        for c in chunks
    ]


def _post_llm_json(
    cli: httpx.Client,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    system: str,
    prompt: dict[str, Any],
    max_tokens: int,
    response_format_json: bool = False,
    schema_kind: str | None = None,
) -> dict[str, Any]:
    """Synchronous counterpart of the corrector's structured JSON boundary."""
    if schema_kind:
        response_format_for(schema_kind)
    last_error: Exception | None = None
    attempts = 2 if schema_kind else 1
    for attempt in range(attempts):
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
        }
        if schema_kind and attempt == 0:
            payload["response_format"] = structured_response_format(schema_kind, attempt=0)
        elif response_format_json or schema_kind:
            payload["response_format"] = structured_response_format(schema_kind, attempt=1)
        with_local_chat_template(payload)
        try:
            resp = cli.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"LLM HTTP {resp.status_code}: {resp.text[:300]}",
                    request=resp.request,
                    response=resp,
                )
            body = resp.json()
            choice = body["choices"][0]
            if str(choice.get("finish_reason") or "").lower() in {"length", "content_filter"}:
                raise StructuredJSONError("LLM 응답이 토큰 한도에서 잘렸습니다.")
            return parse_object(choice["message"]["content"], kind=schema_kind)
        except (httpx.HTTPError, KeyError, TypeError, StructuredJSONError, json.JSONDecodeError) as exc:
            last_error = exc
            if is_json_schema_rejection(exc):
                mark_json_schema_unsupported()
            if attempt == attempts - 1:
                raise
    raise StructuredJSONError(f"LLM structured response failed: {last_error}")


def _document_chunks(markdown_text: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    lines = markdown_text.splitlines()
    section = "root"
    paragraph: list[str] = []
    paragraph_raw: list[str] = []
    table_header: list[str] | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph, paragraph_raw
        text = "\n".join(line.strip() for line in paragraph if line.strip()).strip()
        raw = "\n".join(paragraph_raw).strip("\n")
        pairs = list(zip(paragraph, paragraph_raw))
        paragraph = []
        paragraph_raw = []
        if not text:
            return
        if len(text) <= _MAX_CHUNK_CHARS:
            _append_chunk(chunks, kind="paragraph", section=section, text=text,
                          raw_text=raw)
            return
        # 한도를 넘는 문단: 문장 파편이 아니라 원본 줄 단위의 큰 조각으로 묶어
        # 맥락과 원문(raw_text) 충실도를 유지한다.
        seg: list[tuple[str, str]] = []
        seg_len = 0

        def emit_segment() -> None:
            nonlocal seg, seg_len
            if not seg:
                return
            seg_text = "\n".join(l.strip() for l, _ in seg if l.strip()).strip()
            seg_raw = "\n".join(r for _, r in seg).strip("\n")
            if seg_text:
                _append_chunk(chunks, kind="paragraph", section=section,
                              text=seg_text, raw_text=seg_raw)
            seg = []
            seg_len = 0

        for line, raw_line in pairs:
            if seg and seg_len + len(line) > _MAX_CHUNK_CHARS:
                emit_segment()
            seg.append((line, raw_line))
            seg_len += len(line) + 1
        emit_segment()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_paragraph()
            table_header = None
            continue
        if line.startswith("#") or re.match(r"^\d+(?:\.\d+)*\.", line):
            flush_paragraph()
            table_header = None
            section = re.sub(r"^[#\s]+", "", line)
            section = re.sub(r"^\d+(?:\.\d+)*\.\s*", "", section).strip() or section
            continue
        if line.startswith("|"):
            flush_paragraph()
            cells = _split_table_row(line)
            if len(cells) <= 1:
                continue
            if all(re.fullmatch(r":?-+:?", cell or "") for cell in cells):
                continue
            if table_header is None:
                table_header = cells
                continue
            pairs = [
                f"{header}: {cell}"
                for header, cell in zip(table_header, cells)
                if header and cell
            ]
            text = " | ".join(pairs) if pairs else line
            # raw_text keeps the ORIGINAL markdown row so callers can locate
            # and edit the report verbatim (text is the LLM-friendly rewrite)
            _append_chunk(chunks, kind="table_row", section=section, text=text,
                          raw_text=raw)
            continue
        table_header = None
        paragraph.append(line)
        paragraph_raw.append(raw)

    flush_paragraph()
    return chunks


def _append_chunk(chunks: list[dict[str, Any]], *, kind: str, section: str,
                  text: str, raw_text: str | None = None) -> None:
    text = text.strip()
    if not text:
        return
    chunks.append({
        "id": f"chunk_{len(chunks) + 1:03d}",
        "kind": kind,
        "section": section,
        "text": text,
        "raw_text": (raw_text or text).strip("\n"),
    })


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _split_sentences(text: str) -> list[str]:
    """줄 우선 분리 후 각 줄을 종결어미로 재분리.

    불릿 리스트('- 매출 성장률: …')처럼 종결어미 없이 끝나는 줄이 많은 문서에서
    청크 전체가 '한 문장'으로 뭉치는 것을 막는다 — atom 은 줄/문장 단위의
    원자적 사실이어야 한다.
    """
    out: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.!?。]|함|됨|임|음|다)\s+", line)
        out.extend(part.strip() for part in parts if part.strip())
    return out


def _build_chunk_atom_prompt(chunk: dict[str, Any], seed_facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "chunk": chunk,
        "fact_unit_schema": _FACT_UNIT_SCHEMA,
        "seed_findings": _seed_fact_payload(seed_facts),
        "instruction": (
            "chunk 에서 검증 가능한 atomic fact 를 최대한 빠짐없이 추출하세요. "
            "fact_unit_schema 의 mandatory_when 조건에 해당하는 단위는 반드시 atoms 에 포함하세요. "
            "atomic fact 는 하나의 주어/기간/지표/값/판단만 담아야 합니다. 한 atom에 여러 연도, 여러 지표, "
            "뉴스/주가/컨센서스 묶음처럼 복합 주장을 넣지 마세요. "
            "chunk.kind 가 table_row 이면 각 헤더-값 쌍을 가능한 별도 atom으로 분해하세요. "
            "문서에 없는 사실을 만들지 말고, source_quote 는 chunk.text 에 실제 존재하는 짧은 근거 구절이어야 합니다. "
            "오직 다음 JSON 형태만 출력하세요: "
            '{"atoms":[{"id":"a001","statement":string,"source_quote":string,'
            '"subject":string,"metric":string,"value":string,"unit":string,"period":string,'
            '"polarity":"positive|negative|neutral|unknown","confidence":number}]}. '
            "가능한 모든 명시적 atom을 포함하세요."
        ),
    }


def _build_chunk_atom_retry_prompt(chunk: dict[str, Any]) -> dict[str, Any]:
    # NOTE: the retry must keep the FULL chunk text — truncating the input
    # here permanently drops every atom past the cut.
    return {
        "chunk": {
            "id": chunk.get("id"),
            "kind": chunk.get("kind"),
            "section": chunk.get("section"),
            "text": str(chunk.get("text") or ""),
        },
        "instruction": (
            "Extract only explicit atomic financial facts from chunk.text. "
            "Return exactly one JSON object with key atoms. "
            "Schema: {\"atoms\":[{\"statement\":\"...\",\"source_quote\":\"...\","
            "\"subject\":\"\",\"metric\":\"\",\"value\":\"\",\"unit\":\"\",\"period\":\"\","
            "\"polarity\":\"neutral\",\"confidence\":0.0}]}. "
            "If no additional facts are needed, return {\"atoms\":[]}."
        ),
    }


def _build_relation_prompt(atom_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "atoms": [
            {
                "id": item.get("id"),
                "statement": item.get("statement"),
                "metric": item.get("metric"),
                "value": item.get("value"),
                "period": item.get("period"),
            }
            for item in atom_items[:_MAX_RELATION_PROMPT_ATOMS]
        ],
        "allowed_relations": sorted(_ALLOWED_RELATIONS),
        "fact_unit_schema": _FACT_UNIT_SCHEMA,
        "instruction": (
            "supplied atoms 사이의 관계만 추출하세요. 새 atom을 만들지 마세요. "
            "depends_on 의 방향은 반드시 source atom 이 target atom 에 의존한다는 뜻입니다. "
            "계산식은 derived_from 또는 depends_on, 동일 기간은 same_period, 동일 지표는 same_metric, "
            "모순은 contradicts, 근거는 supports, 인과 설명은 causes 를 쓰세요. "
            "오직 다음 JSON 형태만 출력하세요: "
            '{"edges":[{"source":"a001","target":"a002","relation":"depends_on",'
            '"reason":string,"confidence":number}]}. '
            f"edges 는 최대 {_MAX_RELATION_PROMPT_EDGES}개로 제한하세요."
        ),
    }


def _build_relation_pair_prompt(
    pairs: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Ask only about embedding/provenance-gated pairs in fast mode."""
    atoms_by_id: dict[str, dict[str, Any]] = {}
    pair_rows: list[dict[str, Any]] = []
    for pair in pairs:
        source, target = str(pair.get("source") or ""), str(pair.get("target") or "")
        for atom_id in (source, target):
            item = by_id.get(atom_id)
            if item is not None and atom_id not in atoms_by_id:
                atoms_by_id[atom_id] = {
                    "id": atom_id,
                    "statement": item.get("statement"),
                    "metric": item.get("metric"),
                    "value": item.get("value"),
                    "period": item.get("period"),
                }
        pair_rows.append({
            "source": source,
            "target": target,
            "similarity": pair.get("similarity"),
            "candidate_reasons": pair.get("reasons") or [],
        })
    return {
        "atoms": list(atoms_by_id.values()),
        "candidate_pairs": pair_rows,
        "allowed_relations": sorted(_ALLOWED_RELATIONS),
        "instruction": (
            "candidate_pairs에 명시된 atom 쌍만 판정하세요. 관계가 없으면 해당 쌍을 출력하지 마세요. "
            "각 쌍은 한 번만 주어지지만 방향성 관계는 실제 의미에 맞게 source/target을 뒤집어도 됩니다. "
            "depends_on은 source가 target에 의존한다는 뜻입니다. supports, contradicts, depends_on, "
            "causes, same_metric, same_period, derived_from 중 하나만 사용하세요. "
            '오직 JSON: {"edges":[{"source":"a001","target":"a002",'
            '"relation":"depends_on","reason":string,"confidence":number}]}.'
        ),
    }


def _relation_pair_batches(pairs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Bound explicit fast-mode pair prompts by count and approximate size."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for pair in pairs:
        pair_chars = len(json.dumps(pair, ensure_ascii=False)) + 400
        if current and (len(current) >= 96 or current_chars + pair_chars > _MAX_RELATION_PROMPT_CHARS):
            batches.append(current)
            current, current_chars = [], 0
        current.append(pair)
        current_chars += pair_chars
    if current:
        batches.append(current)
    return batches


def _chunk_llm_not_needed(chunk: dict[str, Any], mandatory_atoms: list[dict[str, Any]]) -> bool:
    kind = str(chunk.get("kind") or "")
    if kind == "table_row" and mandatory_atoms:
        return True
    rules = {str(atom.get("extraction_rule") or "") for atom in mandatory_atoms}
    if "structured_list_metric_value" in rules and len(mandatory_atoms) >= 8:
        return True
    return False


def _seed_fact_payload(seed_facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for fact in seed_facts[:_MAX_SEED_FACTS]:
        if not isinstance(fact, dict):
            continue
        atom = fact.get("factreasoner_atom")
        statement = ""
        display = True
        if isinstance(atom, dict):
            display = atom.get("display") is not False
            statement = str(atom.get("statement") or "").strip()
        if not display:
            continue
        claim = str(fact.get("claim") or "").strip()
        text = statement or claim
        if not text:
            continue
        out.append({
            "claim": claim[:300],
            "atom_statement": text[:500],
            "badge": str(fact.get("badge") or "")[:40],
            "evidence": str(fact.get("evidence") or "")[:500],
        })
    return out


def _relation_prompt_batches(atom_items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Batch relation prompts without excluding atoms from LLM relation review."""
    priority: list[dict[str, Any]] = []
    regular: list[dict[str, Any]] = []
    for item in atom_items:
        unit_type = str(item.get("unit_type") or "")
        statement = str(item.get("statement") or "")
        if unit_type in {"forecast_or_opinion", "trend_or_comparison", "causal_driver"} or _is_conclusion_like_atom(statement):
            priority.append(item)
        else:
            regular.append(item)
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for item in priority + regular:
        atom_id = str(item.get("id") or "")
        if not atom_id or atom_id in seen:
            continue
        seen.add(atom_id)
        ordered.append(item)
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in ordered:
        item_chars = _relation_prompt_atom_chars(item)
        should_flush = (
            current
            and (
                len(current) >= _MAX_RELATION_PROMPT_ATOMS
                or current_chars + item_chars > _MAX_RELATION_PROMPT_CHARS
            )
        )
        if should_flush:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _relation_prompt_atom_chars(item: dict[str, Any]) -> int:
    return sum(
        len(str(item.get(key) or ""))
        for key in ("id", "statement", "metric", "value", "period")
    ) + 32


def _deterministic_relation_edges(atom_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(source: str, target: str, relation: str, reason: str, confidence: float = 0.72) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, relation)
        if key in seen:
            return
        seen.add(key)
        edges.append({
            "source": source,
            "target": target,
            "relation": relation,
            "reason": reason,
            "confidence": confidence,
        })

    by_metric: dict[str, list[dict[str, Any]]] = {}
    by_period: dict[str, list[dict[str, Any]]] = {}
    for item in atom_items:
        atom_id = str(item.get("id") or "")
        metric = str(item.get("metric") or "").strip()
        period = str(item.get("period") or "").strip()
        if atom_id and metric:
            by_metric.setdefault(_norm_key(metric), []).append(item)
        if atom_id and period:
            by_period.setdefault(_norm_key(period), []).append(item)

    for item in atom_items:
        statement = str(item.get("statement") or "")
        if not _is_margin_atom(statement):
            continue
        period = _norm_key(str(item.get("period") or ""))
        same_period = [
            candidate for candidate in atom_items
            if str(candidate.get("id") or "") != str(item.get("id") or "")
            and (not period or _norm_key(str(candidate.get("period") or "")) == period)
        ]
        # Executable verification first: when the margin and a (매출액, 영업이익)
        # pair are numerically parseable and the formula checks out, wire only
        # the verified derivation (exact-residual context cue) at high
        # confidence instead of every keyword-matched input.
        verified = _margin_verified_pair(item, same_period)
        if verified:
            for parent in verified:
                add(
                    str(item.get("id") or ""),
                    str(parent.get("id") or ""),
                    "derived_from",
                    "실행 검증: 영업이익률 = 영업이익/매출액 x 100 이 수치로 확인되었습니다.",
                    0.95,
                )
            continue
        for candidate in same_period:
            if _is_margin_input_atom(str(candidate.get("statement") or "")):
                add(
                    str(item.get("id") or ""),
                    str(candidate.get("id") or ""),
                    "derived_from",
                    "영업이익률/OPM은 같은 기간의 매출액 또는 영업이익 수치에서 계산됩니다.",
                    0.86,
                )

    for item in atom_items:
        if str(item.get("unit_type") or "") not in {"forecast_or_opinion", "trend_or_comparison"}:
            continue
        item_metric = _norm_key(str(item.get("metric") or ""))
        item_period = _norm_key(str(item.get("period") or ""))
        for candidate in atom_items:
            if str(candidate.get("id") or "") == str(item.get("id") or ""):
                continue
            candidate_metric = _norm_key(str(candidate.get("metric") or ""))
            candidate_period = _norm_key(str(candidate.get("period") or ""))
            if item_metric and candidate_metric and item_metric != candidate_metric:
                continue
            if item_period and candidate_period and item_period != candidate_period:
                continue
            if str(candidate.get("unit_type") or "") in {"metric_value", "causal_driver", "trend_or_comparison"}:
                add(
                    str(item.get("id") or ""),
                    str(candidate.get("id") or ""),
                    "depends_on",
                    "전망/판단 atom이 같은 지표 또는 기간의 근거 atom에 의존합니다.",
                    0.69,
                )
                break

    driver_items = [
        item for item in atom_items
        if str(item.get("unit_type") or "") == "causal_driver"
    ]
    conclusion_items = [
        item for item in atom_items
        if str(item.get("unit_type") or "") in {"forecast_or_opinion", "trend_or_comparison"}
        or _is_conclusion_like_atom(str(item.get("statement") or ""))
    ]
    for driver in driver_items:
        driver_metric = _norm_key(str(driver.get("metric") or ""))
        driver_period = _norm_key(str(driver.get("period") or ""))
        for target in conclusion_items:
            if str(driver.get("id") or "") == str(target.get("id") or ""):
                continue
            target_metric = _norm_key(str(target.get("metric") or ""))
            target_period = _norm_key(str(target.get("period") or ""))
            if driver_metric and target_metric and driver_metric != target_metric:
                continue
            if driver_period and target_period and driver_period != target_period:
                continue
            add(
                str(driver.get("id") or ""),
                str(target.get("id") or ""),
                "supports",
                "원문에서 원인/전제 성격의 문장이 전망 또는 추세 판단을 뒷받침합니다.",
                0.7,
            )
            break

    structural_edges: list[dict[str, Any]] = []

    def add_structural(source: str, target: str, relation: str, reason: str, confidence: float) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, relation)
        if key in seen:
            return
        seen.add(key)
        structural_edges.append({
            "source": source,
            "target": target,
            "relation": relation,
            "reason": reason,
            "confidence": confidence,
        })

    for group in by_metric.values():
        for prev, curr in zip(group, group[1:]):
            add_structural(
                str(curr.get("id") or ""),
                str(prev.get("id") or ""),
                "same_metric",
                "두 fact atom이 같은 지표를 다룹니다.",
                0.78,
            )

    for group in by_period.values():
        for prev, curr in zip(group, group[1:]):
            add_structural(
                str(curr.get("id") or ""),
                str(prev.get("id") or ""),
                "same_period",
                "두 fact atom이 같은 기간을 다룹니다.",
                0.74,
            )

    return edges + structural_edges


def _mandatory_atoms_from_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for chunk in chunks:
        kind = str(chunk.get("kind") or "")
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        if kind == "table_row":
            atoms.extend(_mandatory_table_atoms(chunk))
            continue
        atoms.extend(_mandatory_text_atoms(chunk))
    return atoms


def _mandatory_table_atoms(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(chunk.get("text") or "")
    pairs: list[tuple[str, str]] = []
    for part in text.split("|"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            pairs.append((key, value))
    if len(pairs) < 2:
        return []
    metric = pairs[0][1] if pairs[0][0] in {"항목", "구분", "계정", "Metric"} else pairs[0][0]
    atoms: list[dict[str, Any]] = []
    for header, value in pairs[1:]:
        if _is_empty_table_value(value):
            continue
        # 컬럼 헤더가 기간 패턴(연도/분기)일 때만 period 로 인정한다 —
        # '값', '금액' 같은 일반 헤더가 period 로 오염되면 같은 지표·기간
        # 비교(자릿수 의심)가 어긋난다.
        period = header if _PERIOD_RE.search(header) else ""
        statement = (
            f"{period} {metric}은 {value}이다." if period
            else f"{metric}은 {value}이다."
        )
        atoms.append(_atom_item(
            statement=statement,
            source_quote=text,
            metric=metric,
            value=value,
            unit=_infer_unit(value),
            period=period,
            unit_type="metric_value",
            extraction_rule="table_metric_value",
            source_chunk=chunk,
            confidence=1.0,
        ))
    return atoms


def _mandatory_text_atoms(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = _mandatory_structured_list_atoms(chunk)
    for sentence in _split_sentences(str(chunk.get("text") or "").strip()):
        periods = _extract_periods(sentence)
        metric_sentence = _strip_structured_literals(sentence)
        if metric_sentence:
            for metric in _metrics_in_text(metric_sentence):
                for value in _values_near_metric(metric_sentence, metric):
                    atoms.append(_atom_item(
                        statement=_metric_value_statement(metric, value, periods),
                        source_quote=metric_sentence,
                        metric=metric,
                        value=value,
                        unit=_infer_unit(value),
                        period=", ".join(periods),
                        unit_type="metric_value",
                        extraction_rule="text_metric_value",
                        source_chunk=chunk,
                        confidence=0.95,
                    ))
        if _FORECAST_MARKERS.search(sentence):
            atoms.append(_atom_item(
                statement=sentence,
                source_quote=sentence,
                metric=_first_metric(sentence),
                period=", ".join(periods),
                unit_type="forecast_or_opinion",
                extraction_rule="forecast_or_opinion_statement",
                source_chunk=chunk,
                confidence=0.9,
            ))
        if _TREND_MARKERS.search(sentence):
            atoms.append(_atom_item(
                statement=sentence,
                source_quote=sentence,
                metric=_first_metric(sentence),
                period=", ".join(periods),
                unit_type="trend_or_comparison",
                extraction_rule="trend_or_comparison_statement",
                source_chunk=chunk,
                confidence=0.9,
            ))
        if _CAUSAL_MARKERS.search(sentence):
            atoms.append(_atom_item(
                statement=sentence,
                source_quote=sentence,
                metric=_first_metric(sentence),
                period=", ".join(periods),
                unit_type="causal_driver",
                extraction_rule="causal_driver_statement",
                source_chunk=chunk,
                confidence=0.9,
            ))
    return atoms


def _mandatory_structured_list_atoms(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(chunk.get("text") or "")
    atoms: list[dict[str, Any]] = []
    for list_src in _structured_list_sources(text):
        try:
            rows = ast.literal_eval(list_src)
        except (SyntaxError, ValueError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            period = str(row.get("Date") or row.get("date") or row.get("기간") or "").strip()
            if not period:
                continue
            for key, raw_value in row.items():
                metric = str(key or "").strip()
                if metric.lower() == "date" or metric == "기간":
                    continue
                value = str(raw_value).strip()
                if not metric or not value or _is_empty_table_value(value):
                    continue
                atoms.append(_atom_item(
                    statement=f"{period} {metric}은 {value}이다.",
                    source_quote=str(row),
                    metric=metric,
                    value=value,
                    unit=_infer_unit(value),
                    period=period,
                    unit_type="metric_value",
                    extraction_rule="structured_list_metric_value",
                    source_chunk=chunk,
                    confidence=1.0,
                ))
    return atoms


def _structured_list_sources(text: str) -> list[str]:
    sources: list[str] = []
    for match in re.finditer(r"\[\s*\{.*?\}\s*\]", str(text or ""), flags=re.DOTALL):
        src = match.group(0).strip()
        if "'Date'" in src or '"Date"' in src or "'date'" in src or '"date"' in src:
            sources.append(src)
    return sources


def _strip_structured_literals(text: str) -> str:
    return re.sub(r"\[\s*\{.*?\}\s*\]", " ", str(text or ""), flags=re.DOTALL).strip()


def _atom_item(
    *,
    statement: str,
    source_quote: str,
    metric: str = "",
    value: str = "",
    unit: str = "",
    period: str = "",
    unit_type: str,
    extraction_rule: str,
    source_chunk: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    return {
        "id": "",
        "statement": statement.strip(),
        "source_quote": source_quote.strip(),
        "subject": "",
        "metric": metric.strip(),
        "value": value.strip(),
        "unit": unit.strip(),
        "period": period.strip(),
        "polarity": "unknown",
        "confidence": confidence,
        "chunk_id": str(source_chunk.get("id") or ""),
        "section": str(source_chunk.get("section") or ""),
        "unit_type": unit_type,
        "extraction_rule": extraction_rule,
        "mandatory": True,
    }


def _coerce_fact_atom_graph(data: Any) -> dict[str, Any]:
    raw_atoms = data.get("atoms") if isinstance(data, dict) else None
    raw_edges = data.get("edges") if isinstance(data, dict) else None
    nodes: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    if isinstance(raw_atoms, list):
        for idx, item in enumerate(raw_atoms, start=1):
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement") or "").strip()
            if not statement:
                continue
            node_id = _safe_atom_id(item.get("id"), idx, used_ids)
            used_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "labels": ["fact_atom"],
                "properties": {
                    "entity_name": statement[:500],
                    "entity_type": "fact_atom",
                    "statement": statement[:1000],
                    "source_quote": str(item.get("source_quote") or "").strip()[:1000],
                    "subject": str(item.get("subject") or "").strip()[:200],
                    "metric": str(item.get("metric") or "").strip()[:160],
                    "value": str(item.get("value") or "").strip()[:120],
                    "unit": str(item.get("unit") or "").strip()[:80],
                    "period": str(item.get("period") or "").strip()[:120],
                    "polarity": _clean_polarity(item.get("polarity")),
                    "confidence": _confidence(item.get("confidence")),
                    "chunk_id": str(item.get("chunk_id") or "").strip()[:80],
                    "section": str(item.get("section") or "").strip()[:200],
                    "unit_type": str(item.get("unit_type") or "").strip()[:80],
                    "extraction_rule": str(item.get("extraction_rule") or "").strip()[:120],
                    "mandatory": bool(item.get("mandatory", False)),
                    "source": "mandatory" if item.get("mandatory") else "llm",
                    "suspect": bool(item.get("suspect", False)),
                    "suspect_reason": str(item.get("suspect_reason") or "").strip()[:300],
                },
            })

    valid_ids = {node["id"] for node in nodes}
    conclusion_like = {
        node["id"]: (
            _is_conclusion_like_atom(node["properties"]["statement"])
            or node["properties"].get("unit_type") == "forecast_or_opinion"
        )
        for node in nodes
    }
    # 결론성 atom(전망·추정·투자의견 등)은 교정의 최종 목표이므로 속성으로 노출
    for node in nodes:
        node["properties"]["conclusion_like"] = bool(conclusion_like.get(node["id"]))
    statements = {
        node["id"]: node["properties"]["statement"]
        for node in nodes
    }
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    if isinstance(raw_edges, list):
        for idx, item in enumerate(raw_edges, start=1):
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            relation = str(item.get("relation") or "").strip()
            if source not in valid_ids or target not in valid_ids or source == target:
                continue
            if relation not in _ALLOWED_RELATIONS:
                continue
            source, target = _normalize_edge_direction(
                source,
                target,
                relation,
                statements=statements,
                conclusion_like=conclusion_like,
            )
            key = (source, target, relation)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({
                "id": f"fact_edge_{idx:03d}",
                "source": source,
                "target": target,
                "type": relation,
                "properties": {
                    "relation": relation,
                    "reason": str(item.get("reason") or "").strip()[:1000],
                    "confidence": _confidence(item.get("confidence")),
                    "source": "llm",
                },
            })

    return {"nodes": nodes, "edges": edges}


def _coerce_atom_items(raw_atoms: Any, *, source_chunk: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(raw_atoms, list):
        return items
    chunk_id = str((source_chunk or {}).get("id") or "")
    section = str((source_chunk or {}).get("section") or "")
    for item in raw_atoms:
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue
        items.append({
            "id": "",
            "statement": statement[:1000],
            "source_quote": str(item.get("source_quote") or "").strip()[:1000],
            "subject": str(item.get("subject") or "").strip()[:200],
            "metric": str(item.get("metric") or "").strip()[:160],
            "value": str(item.get("value") or "").strip()[:120],
            "unit": str(item.get("unit") or "").strip()[:80],
            "period": str(item.get("period") or "").strip()[:120],
            "polarity": _clean_polarity(item.get("polarity")),
            "confidence": _confidence(item.get("confidence")),
            "chunk_id": chunk_id,
            "section": section,
            "unit_type": str(item.get("unit_type") or "").strip()[:80],
            "extraction_rule": "llm_chunk_atom",
            "mandatory": False,
        })
    return items


def _dedupe_atom_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by (statement, chunk): mandatory(regex) atoms and LLM atoms of
    the same sentence merge into ONE atom keeping the richer fields, while the
    same sentence restated in a different chunk stays a separate mention."""
    def richness(item: dict[str, Any]) -> int:
        return sum(
            1 for field in ("unit_type", "metric", "value", "period", "source_quote")
            if str(item.get(field) or "").strip()
        )

    def backfill(target: dict[str, Any], source: dict[str, Any]) -> None:
        for field, value in source.items():
            if field == "id":
                continue
            if not str(target.get(field) or "").strip() and str(value or "").strip():
                target[field] = value

    out: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for item in items:
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue
        key = (_norm_key(statement), str(item.get("chunk_id") or ""))
        if key in index_by_key:
            idx = index_by_key[key]
            if richness(item) > richness(out[idx]):
                merged = dict(item)
                backfill(merged, out[idx])
                merged["id"] = out[idx]["id"]
                out[idx] = merged
            else:
                backfill(out[idx], item)
            continue
        item = dict(item)
        item["id"] = f"a{len(out) + 1:03d}"
        index_by_key[key] = len(out)
        out.append(item)
    return out


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _is_empty_table_value(value: str) -> bool:
    text = str(value or "").strip()
    return not text or text in {"-", "—", "–", "N/A", "n/a"}


def _infer_unit(value: str) -> str:
    if _MIXED_AMOUNT_RE.search(str(value or "")):
        return "조+억원"
    match = re.search(r"(조원|억원|원|십억원|백만원|만원|%|배|조|억)", str(value or ""))
    return match.group(1) if match else ""


def _extract_periods(text: str) -> list[str]:
    out: list[str] = []
    for match in _PERIOD_RE.finditer(str(text or "")):
        value = match.group(0).strip()
        if value and value not in out:
            out.append(value)
    return out


def _metrics_in_text(text: str) -> list[str]:
    out: list[str] = []
    for metric in sorted(_KNOWN_METRICS, key=len, reverse=True):
        if metric in text and metric not in out:
            # Keep a shorter metric when it also occurs as a standalone term:
            # ``영업이익`` can coexist with ``영업이익률`` in one sentence,
            # while ``매출`` inside ``매출액`` must remain suppressed.
            if any(metric in selected for selected in out) and not re.search(
                _metric_pattern(metric), text,
            ):
                continue
            out.append(metric)
    return out


def _first_metric(text: str) -> str:
    metrics = _metrics_in_text(text)
    return metrics[0] if metrics else ""


def _metric_pattern(metric: str) -> str:
    """Return a metric regex that does not match inside a longer metric."""
    suffix_guard = {"영업이익": "률", "매출": "액"}.get(metric, "")
    return re.escape(metric) + (rf"(?!{suffix_guard})" if suffix_guard else "")


def _values_near_metric(text: str, metric: str) -> list[str]:
    values: list[str] = []
    metric_matches = list(re.finditer(_metric_pattern(metric), text))
    # Find the next *different* metric too. Otherwise ``매출`` would consume
    # the following 영업이익 amount because it has no second 매출 occurrence.
    all_metric_starts = sorted(
        match.start()
        for candidate in _KNOWN_METRICS
        for match in re.finditer(_metric_pattern(candidate), text)
    )
    # Stop at the next metric occurrence instead of using a broad fixed window.
    # A sentence such as ``매출 ..., 영업이익 ..., 영업이익률 ...`` must keep
    # each value attached to its own metric.
    for index, metric_match in enumerate(metric_matches):
        start = metric_match.end()
        next_starts = [position for position in all_metric_starts if position > metric_match.start()]
        end = min(next_starts, default=len(text))
        window = text[start:end]
        for value_match in _VALUE_RE.finditer(window):
            value = value_match.group(0).strip()
            if re.fullmatch(r"20\d{2}", value) and f"{value}년" in window:
                continue
            if _is_spurious_context_value(value, window):
                continue
            if value and value not in values and value != metric:
                values.append(value)
    return values


def _is_spurious_context_value(value: str, window: str) -> bool:
    compact = str(value or "").replace(",", "").strip()
    if not compact:
        return True
    if re.fullmatch(r"20\d{2}", compact):
        return True
    if re.fullmatch(r"[1-4]", compact) and re.search(rf"Q\s*{re.escape(compact)}|{re.escape(compact)}\s*분기", window, re.IGNORECASE):
        return True
    has_unit = re.search(r"%|조원|억원|원|배|십억원|백만원|만원|조|억", value) is not None
    if not has_unit and re.fullmatch(r"\d+(?:\.\d+)?", compact):
        try:
            number = float(compact)
        except ValueError:
            return False
        if number < 10:
            return True
    return False


def _metric_value_statement(metric: str, value: str, periods: list[str]) -> str:
    period = ", ".join(periods)
    prefix = f"{period} " if period else ""
    particle = "는" if metric.endswith(("가", "표")) else "은"
    return f"{prefix}{metric}{particle} {value}이다."


def _normalize_edge_direction(
    source: str,
    target: str,
    relation: str,
    *,
    statements: dict[str, str],
    conclusion_like: dict[str, bool],
) -> tuple[str, str]:
    if relation == "depends_on" and conclusion_like.get(target) and not conclusion_like.get(source):
        return target, source
    if relation in {"depends_on", "derived_from"}:
        source_text = statements.get(source, "")
        target_text = statements.get(target, "")
        if _is_margin_atom(target_text) and _is_margin_input_atom(source_text):
            return target, source
    return source, target


def _is_margin_atom(statement: str) -> bool:
    return "영업이익률" in str(statement or "") or re.search(r"\b(OPM|margin)\b", str(statement or ""), flags=re.IGNORECASE) is not None


def _is_margin_input_atom(statement: str) -> bool:
    text = str(statement or "")
    return "매출액" in text or ("영업이익" in text and "영업이익률" not in text)


# --- numeric parsing for executable edge verification ----------------------- #
# Amounts are normalised to 억원 so 조원-scale revenue and 억원-scale operating
# profit can be checked against a percentage in one formula.
_AMOUNT_UNIT_EOKWON = {"조원": 10_000.0, "조": 10_000.0, "십억원": 10.0,
                       "억원": 1.0, "억": 1.0, "백만원": 0.01}
_AMOUNT_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*(조원|십억원|백만원|억원|조|억)")
_PCT_VALUE_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%")


def _parse_amount_eokwon(text: str) -> float | None:
    """Parse the first Korean monetary amount into 억원.

    Mixed forms like ``49조2063억원`` need a dedicated conversion because the
    ordinary unit regex intentionally captures only one suffix.
    """
    source = str(text or "")
    mixed = _MIXED_AMOUNT_RE.search(source)
    if mixed:
        try:
            major = float(mixed.group("major").replace(",", ""))
            minor = float(mixed.group("minor").replace(",", ""))
            return major * _AMOUNT_UNIT_EOKWON["조"] + minor * _AMOUNT_UNIT_EOKWON["억"]
        except (TypeError, ValueError):
            return None
    amount = _AMOUNT_RE.search(source)
    if not amount:
        return None
    try:
        return float(amount.group(1).replace(",", "")) * _AMOUNT_UNIT_EOKWON[amount.group(2)]
    except (KeyError, TypeError, ValueError):
        return None


def _parse_pct_value(text: str) -> float | None:
    match = _PCT_VALUE_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _amount_near_keyword(text: str, keyword_re: str) -> float | None:
    """First 금액 (normalised to 억원) within a short window after a keyword."""
    text = str(text or "")
    for kw in re.finditer(keyword_re, text):
        window = text[kw.end():kw.end() + 40]
        value = _parse_amount_eokwon(window)
        if value is not None:
            return value
    return None


def _margin_verified_pair(
    margin_item: dict[str, Any], same_period: list[dict[str, Any]],
    *, tol: float = 0.15,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Executable verification of 영업이익률 = 영업이익/매출액 x 100.

    Among same-period candidates, return the (매출액, 영업이익) atom pair whose
    parsed values reproduce the stated margin with the smallest residual (the
    exact-derivation context cue); None when nothing parses or verifies.
    """
    margin_val = _parse_pct_value(str(margin_item.get("statement") or ""))
    if margin_val is None:
        return None
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for rev in same_period:
        rev_text = str(rev.get("statement") or "")
        if "매출액" not in rev_text:
            continue
        rev_val = _amount_near_keyword(rev_text, r"매출액")
        if not rev_val:
            continue
        for op in same_period:
            op_text = str(op.get("statement") or "")
            if "영업이익" not in op_text or "영업이익률" in op_text:
                continue
            op_val = _amount_near_keyword(op_text, r"영업이익(?!률)")
            if op_val is None:
                continue
            residual = abs(op_val / rev_val * 100.0 - margin_val)
            if residual <= tol and (best is None or residual < best[0]):
                best = (residual, rev, op)
    return (best[1], best[2]) if best else None


def _safe_atom_id(raw: Any, idx: int, used_ids: set[str]) -> str:
    text = str(raw or "").strip()
    if not re.fullmatch(r"a\d{3,}", text):
        text = f"a{idx:03d}"
    while text in used_ids:
        idx += 1
        text = f"a{idx:03d}"
    return text


def _clean_polarity(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in {"positive", "negative", "neutral", "unknown"} else "unknown"


def _is_conclusion_like_atom(statement: str) -> bool:
    return bool(re.search(
        r"전망|결론|투자의견|목표주가|판단|시나리오|전제|기반|유지|상향|하향|outlook|recommendation|target price",
        str(statement or ""),
        flags=re.IGNORECASE,
    ))


def _confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _fallback_graph(seed_facts: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for idx, fact in enumerate(_seed_fact_payload(seed_facts), start=1):
        statement = fact["atom_statement"]
        nodes.append({
            "id": f"a{idx:03d}",
            "labels": ["fact_atom"],
            "properties": {
                "entity_name": statement[:500],
                "entity_type": "fact_atom",
                "statement": statement,
                "source_quote": fact.get("evidence", ""),
                "subject": "",
                "metric": "",
                "value": "",
                "unit": "",
                "period": "",
                "polarity": "unknown",
                "confidence": 0.0,
                "source": "finding_fallback",
            },
        })
    return {"nodes": nodes, "edges": []}


def _unique_node_ids(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node = dict(node)
        node["id"] = f"a{len(out) + 1:03d}"
        props = dict(node.get("properties") or {})
        props["entity_id"] = node["id"]
        node["properties"] = props
        out.append(node)
    return out


def _mandatory_node_count(graph: dict[str, Any]) -> int:
    return sum(
        1
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and (node.get("properties") or {}).get("mandatory")
    )
