"""RippleRepair forecast-correction API.

Deterministic operating-profit scale repair, literal restatement ripples,
FactReasoner atom/cascade correction, arithmetic guard, and advisory rereview.

    uv run uvicorn web_app.main:app --reload --port 8200
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

import os
import uuid

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any, Optional

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from web_app.pipeline.forecast_scale_correction import (
    correct_operating_profit_forecast_scale,
    scale_cluster_is_locked,
)
from web_app.pipeline.fact_graph_preview import run_fact_graph_preview
from web_app.pipeline.fact_graph_correct import (
    batch_correct_atoms,
    batch_judge_atoms,
    judge_atom,
    propagate_correction,
    review_llm_corrected_markdown,
    _normalize_judged_correction,
    _validate_rewrite_suggestion,
)
from web_app.pipeline.fact_pair_miner import embedding_status, warm_embedding_model
from hallucination_verifier.llm_config import (
    chat_api_key,
    chat_base_url,
    factreasoner_model,
    verifier_model,
)

app = FastAPI(
    title="RippleRepair API",
    description="재무 리서치 Markdown의 영업이익 전망 자릿수 오류와 재인용 문장을 교정합니다.",
    version="0.1.0",
)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.on_event("startup")
async def _warm_factreasoner_embedding() -> None:
    """Keep one multilingual ONNX embedding model warm for fast NLI gating."""
    asyncio.create_task(asyncio.to_thread(warm_embedding_model))

_FORECAST_JOB_TTL_SECONDS = 60 * 60
_FORECAST_JOB_LIMIT = 100
_FORECAST_JOBS: dict[str, dict[str, Any]] = {}
_FORECAST_JOB_TASKS: dict[str, asyncio.Task] = {}
_forecast_job_logger = logging.getLogger("web_app.forecast_jobs")
_FACT_GRAPH_CACHE_TTL_SECONDS = 60 * 60
_FACT_GRAPH_CACHE_LIMIT = 64
_FACT_GRAPH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FACT_GRAPH_CACHE_LOCK = asyncio.Lock()
# llama.cpp is started with four inference slots. Keep the independent atom
# judgements at that concurrency; document splicing remains ordered below.
_FACTREASONER_JUDGEMENT_CONCURRENCY = 16
# Scale-pin cascades are independent; match extraction fan-out so FlashInfer
# can batch them instead of waiting on a 4-slot llama.cpp-era cap.
_FACTREASONER_CASCADE_CONCURRENCY = 16


async def _run_sync_in_worker(callable_obj):
    """Run blocking work in a bounded executor with an explicit lifecycle."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ripple-api")
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, callable_obj)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


async def _get_factreasoner_graph(
    markdown_text: str, *, nli_mode: str = "all_pairs",
    progress_callback: Optional[Any] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the LLM graph, reusing byte-identical source Markdown in-process."""
    key = hashlib.sha256(f"{nli_mode}\0{markdown_text}".encode("utf-8")).hexdigest()
    now = time.time()
    async with _FACT_GRAPH_CACHE_LOCK:
        for cache_key, (created_at, _) in list(_FACT_GRAPH_CACHE.items()):
            if now - created_at > _FACT_GRAPH_CACHE_TTL_SECONDS:
                _FACT_GRAPH_CACHE.pop(cache_key, None)
        cached = _FACT_GRAPH_CACHE.get(key)
        if cached is not None:
            created_at, result = cached
            if progress_callback is not None:
                await progress_callback("fact_graph", "동일 문서의 Fact Graph 캐시를 재사용했습니다.")
            return deepcopy(result), {
                "hit": True, "key": key[:16], "age_seconds": round(now - created_at, 3),
                "ttl_seconds": _FACT_GRAPH_CACHE_TTL_SECONDS,
            }
        loop = asyncio.get_running_loop()

        def forward_graph_progress(event: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            message = str(event.get("message") or "Fact Graph를 생성하고 있습니다.")
            future = asyncio.run_coroutine_threadsafe(
                progress_callback("fact_graph", message), loop,
            )
            try:
                future.result(timeout=5)
            except Exception:
                return

        result = await _run_sync_in_worker(
            lambda: run_fact_graph_preview(
                markdown_text=markdown_text, mode="llm", nli_mode=nli_mode,
                progress_callback=forward_graph_progress,
            )
        )
        if len(_FACT_GRAPH_CACHE) >= _FACT_GRAPH_CACHE_LIMIT:
            oldest_key = min(_FACT_GRAPH_CACHE, key=lambda value: _FACT_GRAPH_CACHE[value][0])
            _FACT_GRAPH_CACHE.pop(oldest_key, None)
        _FACT_GRAPH_CACHE[key] = (now, deepcopy(result))
        return result, {
            "hit": False, "key": key[:16], "age_seconds": 0.0,
            "ttl_seconds": _FACT_GRAPH_CACHE_TTL_SECONDS,
        }


class ForecastScaleCorrectionRequest(BaseModel):
    """Input for LLM-based operating-profit forecast correction."""
    markdown_text: str
    consensus_won: Optional[float] = Field(
        None,
        description="최신 영업이익 컨센서스(원 단위, 영업손실은 음수). 생략하면 Markdown에서 추출합니다.",
    )


class ForecastCorrectionPreviewRequest(ForecastScaleCorrectionRequest):
    graph_mode: str = Field(
        "llm",
        description="호환용 모드 값. 사실 판단·교정·검토는 항상 LLM이 수행합니다.",
    )
    nli_mode: Optional[str] = Field(
        None,
        description="관계 후보 비용 모드(all_pairs 또는 fast). 생략하면 graph_mode에서 파생합니다.",
    )


class ForecastCorrectRequest(ForecastCorrectionPreviewRequest):
    max_factreasoner_candidates: int = Field(
        10,
        ge=1,
        le=10,
        description="자동 판정할 영업이익 관련 의심 atom의 최대 수입니다(기본 10).",
    )
    review_applied_corrections: bool = Field(
        True,
        description="자동 적용 직후 결과를 재검토하고, 불확실한 변경은 되돌립니다.",
    )


def _validate_optional_choice(value: object, *, field: str, choices: set[str]) -> str | None:
    if value is None:
        return None
    clean = str(value).strip().lower()
    if not clean:
        return None
    if clean not in choices:
        allowed = ", ".join(sorted(choices))
        raise HTTPException(
            status_code=400,
            detail=f"{field}는 다음 중 하나여야 합니다: {allowed}.",
        )
    return clean


def _require_nonblank(value: object, *, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail=f"{field}는 비어 있을 수 없습니다.")
    return clean


def _clean_optional_string(value: object) -> str | None:
    clean = str(value or "").strip()
    return clean or None


def _validate_optional_consensus(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or value == 0):
        raise HTTPException(status_code=400, detail="consensus_won은 유한한 0이 아닌 숫자여야 합니다.")
    return value

@app.post("/api/forecasts/operating-profit/scale-correction")
async def api_operating_profit_scale_correction(req: ForecastScaleCorrectionRequest):
    """결정론 배율 제안은 항상 반환하고, LLM 재검토는 advisory로만 붙인다."""
    markdown_text = _require_nonblank(req.markdown_text, field="markdown_text")
    consensus_won = _validate_optional_consensus(req.consensus_won)
    correction = correct_operating_profit_forecast_scale(
        markdown_text,
        consensus_won=consensus_won,
    )
    candidate = str(correction.get("corrected_text") or markdown_text)
    corrections = list(correction.get("corrections") or [])
    guard = scale_cluster_is_locked(
        candidate,
        [
            {
                "kind": "scale_correction",
                "year": item.get("year"),
                "residual": item.get("residual_ratio"),
            }
            for item in corrections
        ],
    )
    if corrections:
        audit = await review_llm_corrected_markdown(
            original_markdown=markdown_text,
            corrected_markdown=candidate,
            corrections=corrections,
            consensus={
                "value_won": correction.get("consensus_won"),
                "source": correction.get("consensus_source"),
                "extraction": correction.get("consensus_extraction"),
            },
            arithmetic_guard=guard,
        )
    else:
        audit = {
            "approve": True,
            "online": False,
            "skipped": True,
            "reason": "적용된 배율 교정이 없어 최종 재검토를 건너뛰었습니다.",
            "model": factreasoner_model(),
            "error": None,
        }
    approved = bool(audit.get("approve")) and not audit.get("error")
    review_items = [item for item in correction.get("review_items") or [] if isinstance(item, dict)]
    if not approved:
        review_items.append({
            "reason": audit.get("reason") or audit.get("error") or "LLM 최종 검토에서 승인되지 않았습니다.",
            "rereview": audit,
            "rereview_advisory": True,
        })
    return JSONResponse({
        "mode": "deterministic_scale_llm_review",
        "online": bool(audit.get("online")),
        "model": audit.get("model") or factreasoner_model(),
        "error": audit.get("error"),
        "consensus_won": correction.get("consensus_won") or consensus_won,
        "consensus_source": correction.get("consensus_source"),
        "consensus_extraction": correction.get("consensus_extraction"),
        "corrected_text": candidate,
        "corrections": corrections,
        "review_items": review_items,
        "needs_manual_review": bool(
            review_items or correction.get("error") or guard.get("manual_review")
        ),
        "arithmetic_guard": guard,
        "rereview": {
            "mode": "llm_approval",
            "approve": approved,
            "advisory": True,
            **audit,
        },
        "stats": correction.get("stats") or {"corrections_applied": 0, "numeric_cells_changed": 0},
    })


@app.post("/api/forecasts/operating-profit/correction-preview")
async def api_operating_profit_correction_preview(req: ForecastCorrectionPreviewRequest):
    """Scale correction 뒤의 문서로 FactReasoner graph를 준비한다.

    이 endpoint는 탐색용 preview입니다. atom 교정·영향 전파는 반환된 graph를 사용해
    ``/api/fact-graph/judge-atom`` 및 ``/api/fact-graph/propagate-correction``에서
    명시적으로 수행해야 하며, 이 호출 자체는 어떤 파일도 변경하지 않습니다.
    """
    markdown_text = _require_nonblank(req.markdown_text, field="markdown_text")
    graph_mode = _validate_optional_choice(
        req.graph_mode,
        field="graph_mode",
        choices={"fast", "llm"},
    ) or "llm"
    nli_mode = _validate_optional_choice(
        req.nli_mode,
        field="nli_mode",
        choices={"all_pairs", "fast"},
    ) or ("fast" if graph_mode == "fast" else "all_pairs")
    scale_correction = correct_operating_profit_forecast_scale(
        markdown_text,
        consensus_won=_validate_optional_consensus(req.consensus_won),
    )
    fact_graph, graph_cache = await _get_factreasoner_graph(
        str(scale_correction.get("corrected_text") or markdown_text),
        nli_mode=nli_mode,
    )
    return JSONResponse({
        "scale_correction": scale_correction,
        "fact_graph": fact_graph,
        "fact_graph_cache": graph_cache,
        "next_steps": {
            "judge_atom": "/api/fact-graph/judge-atom",
            "propagate_correction": "/api/fact-graph/propagate-correction",
        },
    })


async def _apply_factreasoner_markdown_corrections(
    markdown_text: str,
    *,
    graph: dict,
    max_candidates: int,
    enable_judgment: bool = True,
    excluded_node_ids: set[str] | None = None,
    batch_turns: bool = False,
) -> tuple[str, list[dict], list[dict]]:
    """Apply only atom proposals that map exactly once to the current Markdown.

    The graph can contain broad qualitative relations.  This automatic endpoint
    deliberately limits itself to suspect operating-profit atoms and retains
    every skipped proposal for human review instead of risking broad rewrites.
    """
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    excluded = excluded_node_ids or set()
    all_candidates = [
        node for node in nodes
        if bool((node.get("properties") or {}).get("suspect"))
        and "영업이익" in str((node.get("properties") or {}).get("statement") or "")
        and "영업이익률" not in str((node.get("properties") or {}).get("statement") or "")
        and str(node.get("id") or "") not in excluded
    ]
    candidates = all_candidates[:max_candidates]
    corrected = markdown_text
    applied: list[dict] = []
    review: list[dict] = [
        {
            "node_id": str(node.get("id") or ""),
            "statement": (node.get("properties") or {}).get("statement") or "",
            "reason": f"자동 판정 상한({max_candidates})을 초과해 수동 검토로 보냈습니다.",
        }
        for node in all_candidates[max_candidates:]
    ]
    if not enable_judgment:
        review.extend({
            "node_id": str(node.get("id") or ""),
            "statement": (node.get("properties") or {}).get("statement") or "",
            "reason": "graph_mode=fast에서는 LLM atom 판정을 실행하지 않아 수동 검토로 보냈습니다.",
        } for node in candidates)
        return corrected, applied, review
    if batch_turns:
        return await _apply_factreasoner_markdown_corrections_batched(
            corrected,
            nodes=nodes,
            edges=edges,
            candidates=candidates,
            review=review,
        )
    judgment_markdown = corrected
    semaphore = asyncio.Semaphore(_FACTREASONER_JUDGEMENT_CONCURRENCY)

    async def _judge_candidate(node: dict[str, Any]):
        node_id = str(node.get("id") or "")
        async with semaphore:
            return node, await judge_atom(
                nodes=nodes,
                edges=edges,
                target_node_id=node_id,
                markdown_text=judgment_markdown,
            )

    # Judgements are independent LLM calls. Run them in bounded batches so
    # four llama.cpp slots can be used while preserving the original candidate
    # order when applying safe exact Markdown rewrites.
    judged_candidates = await asyncio.gather(
        *(_judge_candidate(node) for node in candidates),
        return_exceptions=True,
    )
    for judged_item in judged_candidates:
        if isinstance(judged_item, Exception):
            review.append({
                "reason": f"병렬 atom 판정 실패: {type(judged_item).__name__}: {judged_item}",
            })
            continue
        node, judgment = judged_item
        node_id = str(node.get("id") or "")
        original = str(judgment.get("original_quote_text") or "")
        suggested = str(judgment.get("suggested_quote") or "")
        safe, safety_reason = _safe_exact_markdown_rewrite(
            corrected, original=original, suggested=suggested,
        )
        if (
            judgment.get("changed")
            and not judgment.get("needs_review")
            and safe
        ):
            corrected = corrected.replace(original, suggested, 1)
            applied.append({
                "kind": "atom_judgment",
                "node_id": node_id,
                "original": original,
                "corrected": suggested,
                "reason": judgment.get("reason") or judgment.get("quote_edit_reason") or "",
                "source": judgment.get("correction_source") or judgment.get("quote_edit_source") or "factreasoner",
            })
        else:
            review.append({
                "node_id": node_id,
                "statement": (node.get("properties") or {}).get("statement") or "",
                "reason": judgment.get("rejection_reason") or judgment.get("error") or safety_reason or judgment.get("reason") or "자동 문서 적용 조건을 만족하지 않았습니다.",
            })
    return corrected, applied, review


async def _apply_factreasoner_markdown_corrections_batched(
    markdown_text: str,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    review: list[dict[str, Any]],
) -> tuple[str, list[dict], list[dict]]:
    """Run judgment and correction as bounded multi-atom LLM turns."""
    corrected = markdown_text
    applied: list[dict] = []
    judgment_result = await batch_judge_atoms(
        nodes=nodes,
        edges=edges,
        markdown_text=markdown_text,
        candidates=candidates,
    )
    judgments = judgment_result.get("judgments") or {}
    for error in judgment_result.get("errors") or []:
        review.append({"reason": str(error)})

    correction_candidates: list[dict[str, Any]] = []
    for node in candidates:
        node_id = str(node.get("id") or "")
        judgment = judgments.get(node_id)
        statement = (node.get("properties") or {}).get("statement") or ""
        if not judgment:
            review.append({
                "node_id": node_id,
                "statement": statement,
                "reason": "batch 사실 판단 결과에서 atom id가 누락되어 수동 검토로 보냈습니다.",
            })
            continue
        verdict = judgment.get("verdict")
        if verdict == "keep":
            review.append({
                "node_id": node_id,
                "statement": statement,
                "reason": judgment.get("reason") or "batch LLM이 원문을 유지하라고 판단했습니다.",
                "verdict": "keep",
            })
        elif verdict == "review":
            review.append({
                "node_id": node_id,
                "statement": statement,
                "reason": judgment.get("reason") or "batch LLM이 확정할 수 없어 수동 검토가 필요합니다.",
                "verdict": "review",
            })
        else:
            correction_candidates.append(node)

    correction_result = await batch_correct_atoms(
        nodes=nodes,
        edges=edges,
        markdown_text=markdown_text,
        candidates=correction_candidates,
        judgments=judgments,
    )
    for error in correction_result.get("errors") or []:
        review.append({"reason": str(error)})
    correction_rows = correction_result.get("corrections") or {}
    for node in correction_candidates:
        node_id = str(node.get("id") or "")
        row = correction_rows.get(node_id)
        statement = str((node.get("properties") or {}).get("statement") or "")
        if not row:
            review.append({"node_id": node_id, "statement": statement, "reason": "batch 교정 결과가 누락되었습니다."})
            continue
        reason = str(row.get("reason") or judgments.get(node_id, {}).get("reason") or "").strip()
        proposed = str(row.get("corrected_statement") or statement).strip()
        normalized, accepted, rejection_reason = _normalize_judged_correction(
            original_statement=statement,
            proposed=proposed,
            reason=reason,
        )
        target_text = str(row.get("target_text") or "").strip()
        suggested = str(row.get("suggested_quote") or "").strip()
        if accepted and normalized != statement:
            suggested, quote_ok, quote_reason = _validate_rewrite_suggestion(
                original_text=target_text,
                suggested_text=suggested,
                corrected_statement=normalized,
            )
            if quote_ok:
                safe, safety_reason = _safe_exact_markdown_rewrite(
                    corrected, original=target_text, suggested=suggested,
                )
                if safe:
                    corrected = corrected.replace(target_text, suggested, 1)
                    applied.append({
                        "kind": "atom_judgment_batch",
                        "node_id": node_id,
                        "original": target_text,
                        "corrected": suggested,
                        "original_statement": statement,
                        "corrected_statement": normalized,
                        "reason": reason,
                        "source": "factreasoner_batch_turn",
                        "tree_group_id": row.get("tree_group_id") or "",
                        "tree_size": row.get("tree_size") or 1,
                    })
                    continue
                rejection_reason = safety_reason
            else:
                rejection_reason = quote_reason
        review.append({
            "node_id": node_id,
            "statement": statement,
            "reason": rejection_reason or reason or "batch 교정이 자동 적용 조건을 만족하지 않았습니다.",
            "verdict": "correct",
        })
    return corrected, applied, review


def _safe_exact_markdown_rewrite(
    markdown_text: str, *, original: str, suggested: str,
) -> tuple[bool, str]:
    """Guard the final document splice independently of the LLM judge."""
    if not original or not suggested or original == suggested:
        return False, "원문 또는 교정문이 없거나 두 문장이 동일합니다."
    occurrences = markdown_text.count(original)
    if occurrences != 1:
        return False, f"교정 대상이 Markdown에 정확히 한 번 존재하지 않습니다(count={occurrences})."
    if len(suggested) < max(1, int(len(original) * 0.5)):
        return False, "교정문이 원문보다 과도하게 짧아 문맥 손실 위험이 있습니다."
    if original.count("\n") != suggested.count("\n"):
        return False, "교정 전후 줄 수가 달라 Markdown 구조 보존을 확인할 수 없습니다."

    def shape(value: str) -> tuple[int, int, int, int]:
        lines = value.splitlines()
        return (
            value.count("```"),
            sum(line.lstrip().startswith("#") for line in lines),
            sum("|" in line for line in lines),
            sum(bool(re.match(r"^\s*[-+*]\s+", line)) for line in lines),
        )

    if shape(original) != shape(suggested):
        return False, "heading/table/fence/list 구조가 바뀌어 자동 적용하지 않았습니다."
    return True, ""


async def _rereview_applied_corrections(
    markdown_text: str,
    *,
    applied: list[dict],
    graph: dict,
    graph_mode: str,
    enabled: bool,
    original_markdown: str | None = None,
    fact_judgments: list[dict] | None = None,
    consensus: dict | None = None,
    arithmetic_guard: dict | None = None,
) -> tuple[str, list[dict], list[dict], dict]:
    """Run LLM approval after scale/cascade/atom edits.

    A rejection restores the original only when a scale block exists and
    H1+H2=annual is broken. Valid identities and non-digit FactReasoner
    repairs stay in the document; the review becomes advisory.
    """
    meta = {
        "enabled": bool(enabled),
        "checked": len(applied),
        "passed": 0,
        "reverted": 0,
        "unresolved": 0,
        "mode": "llm_approval" if enabled else "disabled",
        "online": False,
        "reason": "",
        "locked": False,
        "advisory_reject": False,
    }
    if not enabled:
        return markdown_text, list(applied), [], meta
    if not applied:
        meta["mode"] = "skipped_no_changes"
        meta["reason"] = "적용된 교정이 없어 최종 재검토를 건너뛰었습니다."
        return markdown_text, [], [], meta
    guard = arithmetic_guard if arithmetic_guard is not None else scale_cluster_is_locked(
        markdown_text, applied,
    )
    audit = await review_llm_corrected_markdown(
        original_markdown=str(original_markdown if original_markdown is not None else markdown_text),
        corrected_markdown=markdown_text,
        corrections=applied,
        fact_judgments=fact_judgments,
        consensus=consensus,
        arithmetic_guard=guard,
    )
    meta["online"] = bool(audit.get("online"))
    meta["reason"] = audit.get("reason") or audit.get("error") or ""
    if audit.get("approve") and not audit.get("error"):
        meta["passed"] = meta["checked"]
        meta["locked"] = bool(guard.get("lock"))
        retained = []
        for item in applied:
            item_copy = dict(item)
            item_copy["rereview"] = {
                "status": "passed",
                "reason": audit.get("reason") or "LLM 최종 검토 승인",
                "mode": "llm_approval",
            }
            retained.append(item_copy)
        return markdown_text, retained, [], meta

    has_scale = any(item.get("kind") == "scale_correction" for item in applied)
    if has_scale and not guard.get("lock"):
        meta["unresolved"] = meta["checked"]
        meta["reverted"] = meta["checked"]
        return (
            str(original_markdown if original_markdown is not None else markdown_text),
            [],
            [{
                "reason": audit.get("reason") or audit.get("error") or "LLM 최종 검토에서 승인되지 않았습니다.",
                "rereview_reverted": True,
                "rereview": audit,
                "arithmetic_guard": guard,
            }],
            meta,
        )

    meta["unresolved"] = meta["checked"]
    meta["locked"] = True
    meta["advisory_reject"] = True
    retained = []
    for item in applied:
        item_copy = dict(item)
        item_copy["rereview"] = {
            "status": "advisory_reject",
            "reason": audit.get("reason") or audit.get("error") or "LLM 최종 검토에서 승인되지 않았습니다.",
            "mode": "llm_approval",
        }
        retained.append(item_copy)
    return (
        markdown_text,
        retained,
        [{
            "reason": audit.get("reason") or audit.get("error") or "LLM 최종 검토에서 승인되지 않았습니다.",
            "rereview_reverted": False,
            "rereview_advisory": True,
            "rereview": audit,
            "arithmetic_guard": guard,
        }],
        meta,
    )


_MONEY_TOKEN_RE = re.compile(
    r"(?<![\d,.])(?P<value>[+-]?[0-9][0-9,]*(?:\.[0-9]+)?)"
    r"(?P<space>\s*)(?P<unit>조원|억원|조|억|원)"
)
_MONEY_DIVISORS = {
    "조원": 1_0000_0000_0000.0,
    "조": 1_0000_0000_0000.0,
    "억원": 1_0000_0000.0,
    "억": 1_0000_0000.0,
    "원": 1.0,
}


def _money_match_won(match: re.Match[str]) -> float:
    return float(match.group("value").replace(",", "")) * _MONEY_DIVISORS[match.group("unit")]


def _same_won(left: float, right: float) -> bool:
    return abs(left - right) <= max(1.0, abs(left), abs(right)) * 1e-10


def _render_money_like(value_won: float, match: re.Match[str]) -> str:
    value = value_won / _MONEY_DIVISORS[match.group("unit")]
    grouped = "," in match.group("value")
    if float(value).is_integer():
        token = f"{int(value):,}" if grouped else str(int(value))
    else:
        token = (f"{value:,.6f}" if grouped else f"{value:.6f}").rstrip("0").rstrip(".")
    return f"{token}{match.group('space')}{match.group('unit')}"


def _first_money_won(text: str) -> float | None:
    match = _MONEY_TOKEN_RE.search(str(text or ""))
    if match is None:
        return None
    return _money_match_won(match)


def _cascade_is_pin_restatement(intervention: dict, original: str, suggested: str) -> bool:
    """Accept only pin old→new money substitutions, not new calculations."""
    old_won = intervention.get("old_value_won")
    new_won = intervention.get("new_value_won")
    if old_won is None or new_won is None:
        old_won = _first_money_won(intervention.get("original_statement") or "")
        new_won = _first_money_won(intervention.get("corrected_statement") or "")
    if old_won is None or new_won is None:
        return False
    rewritten, replacements = _replace_equivalent_money(
        original, old_value_won=float(old_won), new_value_won=float(new_won),
    )
    return bool(replacements) and rewritten.rstrip("\r\n") == suggested.rstrip("\r\n")


def _replace_equivalent_money(
    text: str, *, old_value_won: float, new_value_won: float,
) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if not _same_won(_money_match_won(match), old_value_won):
            return match.group(0)
        count += 1
        return _render_money_like(new_value_won, match)

    return _MONEY_TOKEN_RE.sub(replace, text), count


def _scale_value_mappings(scale_correction: dict) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float, float]] = set()
    for correction in scale_correction.get("corrections") or []:
        year = str(correction.get("year") or "")
        for change in correction.get("changes") or []:
            old_value = float(change.get("old_value_won") or 0)
            new_value = float(change.get("new_value_won") or 0)
            label = str(change.get("label") or correction.get("period") or "연간")
            key = (year, label, old_value, new_value)
            if not old_value or _same_won(old_value, new_value) or key in seen:
                continue
            seen.add(key)
            mappings.append({
                "mapping_id": f"scale_{len(mappings) + 1:03d}",
                "year": year,
                "label": label,
                "period": correction.get("period") or "annual",
                "factor": change.get("factor", correction.get("factor")),
                "old_value_won": old_value,
                "new_value_won": new_value,
                "source_line": change.get("line"),
                "old_line": change.get("old_line") or "",
                "new_line": change.get("new_line") or "",
                "consensus_won": correction.get("consensus_won"),
                "consensus_date": correction.get("consensus_date"),
                "consensus_source": correction.get("consensus_source"),
                "reason": correction.get("reason") or "",
            })
    return mappings


def _mapping_matches_context(mapping: dict, text: str, active_year: str | None) -> bool:
    years = set(re.findall(r"20\d{2}", text))
    year = str(mapping.get("year") or "")
    if year and ((years and year not in years) or (not years and active_year and year != active_year)):
        return False
    label = str(mapping.get("label") or "")
    present_labels = {item for item in ("상방", "중간", "하방") if item in text}
    if label in {"상방", "중간", "하방"} and present_labels and label not in present_labels:
        return False
    return True


def _apply_literal_scale_ripples(
    markdown_text: str, mappings: list[dict[str, Any]],
) -> tuple[str, list[dict], list[dict]]:
    """Repair exact monetary restatements while keeping their original units."""
    lines = markdown_text.splitlines(keepends=True)
    active_year: str | None = None
    in_fence = False
    applied: list[dict] = []
    review: list[dict] = []
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if "영업이익" in line and "전망" in line:
            year_match = re.search(r"20\d{2}", line)
            if year_match:
                active_year = year_match.group(0)
        if "영업이익" not in line:
            continue
        eligible = [m for m in mappings if _mapping_matches_context(m, line, active_year)]
        by_old: dict[float, list[dict]] = {}
        for mapping in eligible:
            if any(_same_won(_money_match_won(token), mapping["old_value_won"]) for token in _MONEY_TOKEN_RE.finditer(line)):
                by_old.setdefault(float(mapping["old_value_won"]), []).append(mapping)
        current = line
        for old_value, candidates in by_old.items():
            distinct_new = {float(item["new_value_won"]) for item in candidates}
            if len(distinct_new) != 1:
                review.append({
                    "line": idx + 1,
                    "reason": "같은 영업이익 금액에 서로 다른 scale 교정이 대응해 재인용문 자동 수정을 보류했습니다.",
                    "mapping_ids": [item["mapping_id"] for item in candidates],
                })
                continue
            new_value = next(iter(distinct_new))
            revised, replacements = _replace_equivalent_money(
                current, old_value_won=old_value, new_value_won=new_value,
            )
            if replacements:
                applied.append({
                    "kind": "literal_scale_ripple",
                    "line": idx + 1,
                    "mapping_ids": [item["mapping_id"] for item in candidates],
                    "mapping_context": [
                        {
                            key: item.get(key)
                            for key in (
                                "mapping_id", "year", "label", "period", "factor",
                                "old_value_won", "new_value_won", "consensus_won",
                                "consensus_date", "consensus_source", "old_line", "new_line",
                                "reason",
                            )
                            if item.get(key) is not None
                        }
                        for item in candidates
                    ],
                    "old_value_won": old_value,
                    "new_value_won": new_value,
                    "original": current.rstrip("\r\n"),
                    "corrected": revised.rstrip("\r\n"),
                    "replacements": replacements,
                })
                current = revised
        lines[idx] = current
    return "".join(lines), applied, review


def _map_scale_changes_to_fact_atoms(
    *, graph: dict, mappings: list[dict[str, Any]], max_candidates: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    nodes = list(graph.get("nodes") or [])
    used_node_ids: set[str] = set()
    interventions: list[dict] = []
    skipped: list[dict] = []
    review: list[dict] = []
    for mapping in mappings:
        ranked: list[tuple[int, dict, str]] = []
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not node_id or node_id in used_node_ids:
                continue
            props = node.get("properties") or {}
            statement = str(props.get("statement") or "")
            source_quote = str(props.get("source_quote") or "")
            metric = str(props.get("metric") or "")
            if "영업이익률" in statement or "영업이익률" in metric:
                continue
            if "영업이익" not in statement and "영업이익" not in metric and "영업이익" not in source_quote:
                continue
            if not _mapping_matches_context(mapping, f"{statement}\n{source_quote}\n{props.get('period') or ''}", None):
                continue
            corrected_statement, statement_hits = _replace_equivalent_money(
                statement,
                old_value_won=mapping["old_value_won"],
                new_value_won=mapping["new_value_won"],
            )
            if statement_hits != 1 or corrected_statement == statement:
                continue
            score = 2
            value_text = str(props.get("value") or "")
            if any(
                _same_won(_money_match_won(token), mapping["old_value_won"])
                for token in _MONEY_TOKEN_RE.finditer(value_text)
            ):
                score += 5
            if any(
                _same_won(_money_match_won(token), mapping["old_value_won"])
                for token in _MONEY_TOKEN_RE.finditer(source_quote)
            ):
                score += 2
            if str(mapping.get("label") or "") in statement:
                score += 1
            if str(mapping.get("old_line") or "").strip() and source_quote.strip() in str(mapping["old_line"]):
                score += 3
            ranked.append((score, node, corrected_statement))
        ranked.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        if not ranked:
            skipped.append({
                "mapping_id": mapping["mapping_id"],
                "reason": "이 scale 변경값을 재인용한 FactReasoner atom이 없습니다.",
            })
            continue
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            review.append({
                "mapping_id": mapping["mapping_id"],
                "reason": (
                    "scale 교정을 FactReasoner atom에 유일하게 매핑하지 못해 cascade를 자동 실행하지 않았습니다."
                ),
                "candidate_node_ids": [str(item[1].get("id") or "") for item in ranked[:5]],
            })
            continue
        if len(interventions) >= max_candidates:
            review.append({
                "mapping_id": mapping["mapping_id"],
                "reason": f"FactReasoner scale pin 상한({max_candidates})을 초과했습니다.",
            })
            continue
        _, node, corrected_statement = ranked[0]
        node_id = str(node.get("id") or "")
        used_node_ids.add(node_id)
        interventions.append({
            "mapping_id": mapping["mapping_id"],
            "node_id": node_id,
            "original_statement": str((node.get("properties") or {}).get("statement") or ""),
            "corrected_statement": corrected_statement,
            "old_value_won": mapping["old_value_won"],
            "new_value_won": mapping["new_value_won"],
            "year": mapping.get("year"),
            "label": mapping.get("label"),
        })
    return interventions, skipped, review


async def _apply_scale_factreasoner_cascades(
    markdown_text: str,
    *,
    original_markdown: str,
    graph: dict,
    interventions: list[dict],
    enable_cascade: bool,
) -> tuple[str, list[dict], list[dict], list[dict]]:
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    cascades: list[dict] = []
    applied: list[dict] = []
    review: list[dict] = []
    proposals: list[dict] = []
    semaphore = asyncio.Semaphore(_FACTREASONER_CASCADE_CONCURRENCY)

    async def _run_cascade(intervention: dict) -> tuple[dict, dict]:
        async with semaphore:
            try:
                cascade = await propagate_correction(
                    markdown_text=original_markdown,
                    nodes=nodes,
                    edges=edges,
                    target_node_id=intervention["node_id"],
                    corrected_statement=intervention["corrected_statement"],
                    max_depth=2,
                )
            except Exception as exc:  # keep one failed pin reviewable
                cascade = {"error": f"병렬 cascade 실패: {type(exc).__name__}: {exc}"}
            return intervention, cascade

    # Each pin is grounded in the same original graph/document and can be
    # judged independently. Keep the returned list in intervention order so
    # audit output and deterministic Markdown merge order do not change.
    cascade_results = await asyncio.gather(
        *(_run_cascade(intervention) for intervention in interventions),
    )
    for intervention, cascade in cascade_results:
        cascades.append({"intervention": intervention, "result": cascade})
        if cascade.get("error"):
            review.append({
                "node_id": intervention["node_id"],
                "mapping_id": intervention["mapping_id"],
                "reason": str(cascade.get("error")),
            })
        target = cascade.get("target") or {}
        target_original = str(
            target.get("original_chunk_text") or target.get("source_quote") or ""
        )
        target_suggested = str(target.get("suggested_quote") or "")
        if target_original and target_suggested and target_suggested != target_original:
            proposals.append({
                "kind": "scale_pin_target",
                "pin_node_id": intervention["node_id"],
                "mapping_id": intervention["mapping_id"],
                "chunk_id": target.get("chunk_id"),
                "direction": "target",
                "original": target_original,
                "corrected": target_suggested,
                "reason": target.get("reason") or "FactReasoner LLM target correction",
            })
        for propagation in cascade.get("propagations") or []:
            if not propagation.get("affected"):
                if propagation.get("needs_manual"):
                    review.append({
                        "node_id": intervention["node_id"],
                        "chunk_id": propagation.get("chunk_id"),
                        "reason": propagation.get("reason") or "cascade 수동 검토가 필요합니다.",
                    })
                continue
            original = str(propagation.get("original_text") or "")
            suggested = str(propagation.get("suggested_text") or "")
            if propagation.get("needs_manual") or not original or not suggested:
                review.append({
                    "node_id": intervention["node_id"],
                    "chunk_id": propagation.get("chunk_id"),
                    "reason": (
                        propagation.get("reason")
                        or "FactReasoner LLM cascade가 자동 적용 가능한 교정문을 반환하지 않았습니다."
                    ),
                    "suggested_text": suggested,
                })
                continue
            if not _cascade_is_pin_restatement(intervention, original, suggested):
                review.append({
                    "node_id": intervention["node_id"],
                    "chunk_id": propagation.get("chunk_id"),
                    "reason": (
                        propagation.get("reason")
                        or "FactReasoner LLM cascade가 확정 pin과 다른 값을 제안해 자동 적용하지 않았습니다."
                    ),
                    "suggested_text": suggested,
                })
                continue
            proposals.append({
                "kind": "propagation",
                "pin_node_id": intervention["node_id"],
                "mapping_id": intervention["mapping_id"],
                "chunk_id": propagation.get("chunk_id"),
                "direction": propagation.get("direction"),
                "original": original,
                "corrected": suggested,
            })

    by_original: dict[str, list[dict]] = {}
    for proposal in proposals:
        by_original.setdefault(proposal["original"], []).append(proposal)
    corrected = markdown_text
    for original, same_anchor in by_original.items():
        suggestions = {item["corrected"] for item in same_anchor}
        if len(suggestions) != 1:
            review.append({
                "reason": "같은 Markdown 범위에 충돌하는 cascade 제안이 있어 자동 적용하지 않았습니다.",
                "chunk_ids": [item.get("chunk_id") for item in same_anchor],
            })
            continue
        suggested = next(iter(suggestions))
        if suggested and suggested in corrected:
            continue
        safe, reason = _safe_exact_markdown_rewrite(corrected, original=original, suggested=suggested)
        if not safe:
            # The literal ripple stage may already have produced this exact
            # chunk, in which case there is no unresolved edit to apply.
            if suggested and suggested in corrected:
                continue
            review.append({
                "reason": reason,
                "chunk_ids": [item.get("chunk_id") for item in same_anchor],
            })
            continue
        corrected = corrected.replace(original, suggested, 1)
        applied.append(same_anchor[0])
    return corrected, cascades, applied, review


async def _execute_operating_profit_correction(
    req: ForecastCorrectRequest,
    *,
    progress_callback: Optional[Any] = None,
) -> dict[str, Any]:
    """Run deterministic consensus/scale repair plus LLM FactReasoner repair.

    Consensus extraction and the numeric scale calculation remain
    deterministic by design. Fact atom judgment, target/cascade correction,
    and the final approval review are all performed by the LLM.
    """
    async def report(stage: str, message: str) -> None:
        if progress_callback is not None:
            await progress_callback(stage, message)

    markdown_text = _require_nonblank(req.markdown_text, field="markdown_text")
    graph_mode = _validate_optional_choice(
        req.graph_mode, field="graph_mode", choices={"fast", "llm"},
    ) or "llm"
    nli_mode = _validate_optional_choice(
        req.nli_mode, field="nli_mode", choices={"all_pairs", "fast"},
    ) or ("fast" if graph_mode == "fast" else "all_pairs")
    await report("scale_analysis", "컨센서스와 숫자 자릿수 오류를 계산하고 있습니다.")
    scale_correction = correct_operating_profit_forecast_scale(
        markdown_text,
        consensus_won=_validate_optional_consensus(req.consensus_won),
    )
    scale_corrected = str(scale_correction["corrected_text"])
    scale_mappings = _scale_value_mappings(scale_correction)
    ripple_markdown, ripple_applied, ripple_review = _apply_literal_scale_ripples(
        scale_corrected, scale_mappings,
    )

    # Atom extraction remains complete in both modes. Fast only gates the
    # expensive relation candidate pairs after every chunk has been atomized.
    await report("fact_graph", "FactReasoner가 원문에서 fact graph를 만들고 있습니다.")
    fact_graph_result, graph_cache = await _get_factreasoner_graph(
        markdown_text, nli_mode=nli_mode, progress_callback=progress_callback,
    )
    graph = dict(fact_graph_result.get("fact_atom_graph") or {})
    graph_nli_stats = graph.get("nli_stats") or {}
    if graph_nli_stats:
        await report(
            "pair_gate",
            "관계 후보 pair를 "
            f"{graph_nli_stats.get('enumerated_pairs', 0)}개에서 "
            f"{graph_nli_stats.get('kept_pairs', 0)}개로 정리했습니다 "
            f"({graph_nli_stats.get('effective_mode', nli_mode)}).",
        )
    interventions, intervention_skips, intervention_review = _map_scale_changes_to_fact_atoms(
        graph=graph,
        max_candidates=req.max_factreasoner_candidates,
        mappings=scale_mappings,
    )
    await report("cascade", "자릿수 교정이 연결된 사실에 미치는 영향을 검사하고 있습니다.")
    cascaded_markdown, cascades, cascade_applied, cascade_review = await _apply_scale_factreasoner_cascades(
        ripple_markdown,
        original_markdown=markdown_text,
        graph=graph,
        interventions=interventions,
        enable_cascade=True,
    )
    remaining_candidates = max(0, req.max_factreasoner_candidates - len(interventions))
    await report("atom_correction", "영향 받은 fact atom을 판단하고 교정안을 만들고 있습니다.")
    corrected_markdown, atom_applied, atom_review = await _apply_factreasoner_markdown_corrections(
        cascaded_markdown,
        graph=graph,
        max_candidates=remaining_candidates,
        enable_judgment=True,
        excluded_node_ids={item["node_id"] for item in interventions},
        batch_turns=True,
    )
    scale_applied: list[dict[str, Any]] = []
    for correction in scale_correction.get("corrections") or []:
        for change in correction.get("changes") or []:
            original_line = str(change.get("old_line") or "")
            corrected_line = str(change.get("new_line") or "")
            if original_line and corrected_line and original_line != corrected_line:
                scale_applied.append({
                    "kind": "scale_correction",
                    "year": correction.get("year"),
                    "period": correction.get("period") or "annual",
                    "label": change.get("label"),
                    "original": original_line,
                    "corrected": corrected_line,
                    "residual": correction.get("residual_ratio"),
                    "factor": change.get("factor", correction.get("factor")),
                    "reason": correction.get("reason") or "deterministic consensus/scale calculation",
                })
    factreasoner_applied = [*scale_applied, *ripple_applied, *cascade_applied, *atom_applied]
    manual_review = [
        *(scale_correction.get("review_items") or []),
        *ripple_review,
        *intervention_review,
        *cascade_review,
        *atom_review,
    ]
    fact_judgments = [
        {
            "node_id": node.get("id") or "",
            "text": (node.get("properties") or {}).get("statement") or node.get("id") or "",
            "verdict": "incorrect_candidate" if (node.get("properties") or {}).get("suspect") else "context",
            "reason": (node.get("properties") or {}).get("suspect_reason") or "FactReasoner LLM graph context",
        }
        for node in graph.get("nodes") or []
    ]
    consensus_context = {
        "value_won": scale_correction.get("consensus_won"),
        "source": scale_correction.get("consensus_source"),
        "extraction": scale_correction.get("consensus_extraction"),
    }
    arithmetic_guard = scale_cluster_is_locked(corrected_markdown, factreasoner_applied)
    await report("final_review", "적용된 변경을 LLM으로 최종 재검토하고 있습니다.")
    corrected_markdown, factreasoner_applied, rereview_review, rereview = (
        await _rereview_applied_corrections(
            corrected_markdown,
            applied=factreasoner_applied,
            graph=graph,
            graph_mode=graph_mode,
            enabled=req.review_applied_corrections,
            original_markdown=markdown_text,
            fact_judgments=fact_judgments,
            consensus=consensus_context,
            arithmetic_guard=arithmetic_guard,
        )
    )
    manual_review.extend(rereview_review)
    factreasoner_error = str(graph.get("error") or "") or None
    cascade_errors = [
        str((item.get("result") or {}).get("error"))
        for item in cascades if (item.get("result") or {}).get("error")
    ]
    if cascade_errors and not factreasoner_error:
        factreasoner_error = " / ".join(cascade_errors[:6])
    graph_online = bool(graph.get("online"))
    review_online = bool(rereview.get("online"))
    rereview_rejected = bool(rereview.get("reverted"))
    scale_correction["fact_judgments"] = fact_judgments
    scale_correction["needs_manual_review"] = bool(
        manual_review
        or scale_correction.get("needs_manual_review")
        or factreasoner_error
        or arithmetic_guard.get("manual_review")
    )
    return {
        "original_markdown": markdown_text,
        "corrected_markdown": corrected_markdown,
        "scale_correction": scale_correction,
        "factreasoner": {
            "graph": graph,
            "mode": graph.get("mode") or graph_mode,
            "online": bool(graph_online or review_online),
            "error": factreasoner_error,
            "cascade_errors": cascade_errors,
            "graph_basis": "original_markdown",
            "graph_cache": graph_cache,
            "nli_mode": nli_mode,
            "nli_stats": graph.get("nli_stats") or {},
            "scale_interventions": interventions,
            "scale_intervention_skips": intervention_skips,
            "cascades": cascades,
            "literal_ripple_corrections": ripple_applied,
            "llm_fact_judgments": fact_judgments,
            "applied_corrections": factreasoner_applied,
            "manual_review": manual_review,
            "rereview": rereview,
            "arithmetic_guard": arithmetic_guard,
        },
        "stats": {
            "scale_corrections_applied": 0 if rereview_rejected else scale_correction["stats"]["corrections_applied"],
            "scale_cells_changed": 0 if rereview_rejected else scale_correction["stats"].get("numeric_cells_changed", 0),
            "literal_ripple_corrections_applied": 0 if rereview_rejected else len(ripple_applied),
            "factreasoner_cascades_run": len(cascades),
            "factreasoner_corrections_applied": len(factreasoner_applied),
            "rereview_checked": rereview["checked"],
            "rereview_passed": rereview["passed"],
            "rereview_reverted": rereview["reverted"],
            "manual_review_required": bool(
                scale_correction.get("needs_manual_review")
                or manual_review
                or factreasoner_error
                or arithmetic_guard.get("manual_review")
            ),
        },
    }


@app.post("/api/forecasts/operating-profit/correct")
async def api_operating_profit_correct(req: ForecastCorrectRequest):
    """Markdown을 받아 scale + FactReasoner ripple 교정 Markdown을 반환한다.

    기존 동기 계약이다. 장시간 걸릴 수 있는 ``graph_mode=llm``를 외부에서
    호출할 때는 ``/correct/async``와 job polling 계약을 사용한다.
    """
    return JSONResponse(await _execute_operating_profit_correction(req))


def _prune_forecast_jobs() -> None:
    now = time.time()
    expired = [
        job_id for job_id, job in _FORECAST_JOBS.items()
        if job.get("status") in {"completed", "failed"}
        and now - float(job.get("updated_at") or job.get("created_at") or now) > _FORECAST_JOB_TTL_SECONDS
    ]
    for job_id in expired:
        _FORECAST_JOBS.pop(job_id, None)
        _FORECAST_JOB_TASKS.pop(job_id, None)
    if len(_FORECAST_JOBS) <= _FORECAST_JOB_LIMIT:
        return
    removable = sorted(
        (
            (float(job.get("updated_at") or job.get("created_at") or now), job_id)
            for job_id, job in _FORECAST_JOBS.items()
            if job.get("status") in {"completed", "failed"}
        ),
    )
    for _, job_id in removable[: max(0, len(_FORECAST_JOBS) - _FORECAST_JOB_LIMIT)]:
        _FORECAST_JOBS.pop(job_id, None)
        _FORECAST_JOB_TASKS.pop(job_id, None)


def _forecast_job_public(job: dict[str, Any], *, include_result: bool = True) -> dict[str, Any]:
    payload = {
        "job_id": job["job_id"],
        "status": job["status"],
        "graph_mode": job.get("graph_mode"),
        "nli_mode": job.get("nli_mode"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "poll_after_ms": 2000,
        "progress": deepcopy(job.get("progress") or []),
    }
    if job.get("status") == "failed":
        payload["error"] = job.get("error") or "교정 작업이 실패했습니다."
    if include_result and job.get("status") == "completed":
        payload["result"] = job.get("result")
    return payload


async def _update_forecast_job_progress(job_id: str, stage: str, message: str) -> None:
    """Store concise stage boundaries for the polling UI; never expose LLM prompts."""
    job = _FORECAST_JOBS.get(job_id)
    if job is None:
        return
    now = time.time()
    progress = job.setdefault("progress", [])
    if (
        progress
        and progress[-1].get("state") == "active"
        and progress[-1].get("stage") == stage
    ):
        progress[-1]["message"] = message
        progress[-1]["updated_at"] = now
        job["updated_at"] = now
        return
    if progress and progress[-1].get("state") == "active":
        progress[-1]["state"] = "completed"
        progress[-1]["completed_at"] = now
    progress.append({
        "stage": stage,
        "message": message,
        "state": "active",
        "started_at": now,
    })
    job["updated_at"] = now


async def _run_forecast_job(job_id: str, req: ForecastCorrectRequest) -> None:
    job = _FORECAST_JOBS.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    job["updated_at"] = time.time()
    try:
        await _update_forecast_job_progress(job_id, "queued", "작업을 시작하고 입력을 확인하고 있습니다.")
        job["result"] = await _execute_operating_profit_correction(
            req,
            progress_callback=lambda stage, message: _update_forecast_job_progress(job_id, stage, message),
        )
        job["status"] = "completed"
        await _update_forecast_job_progress(job_id, "completed", "교정과 검토가 완료되었습니다.")
    except HTTPException as exc:
        job["status"] = "failed"
        job["error"] = str(exc.detail)
        await _update_forecast_job_progress(job_id, "failed", job["error"])
    except Exception as exc:  # pragma: no cover - defensive boundary for background jobs
        _forecast_job_logger.exception("forecast correction job failed: %s", job_id)
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        await _update_forecast_job_progress(job_id, "failed", job["error"])
    finally:
        job["updated_at"] = time.time()


@app.post("/api/forecasts/operating-profit/correct/async", status_code=202)
async def api_operating_profit_correct_async(req: ForecastCorrectRequest):
    """Submit a long-running correction and return a polling job ID immediately."""
    _require_nonblank(req.markdown_text, field="markdown_text")
    graph_mode = _validate_optional_choice(
        req.graph_mode, field="graph_mode", choices={"fast", "llm"},
    ) or "llm"
    nli_mode = _validate_optional_choice(
        req.nli_mode, field="nli_mode", choices={"all_pairs", "fast"},
    ) or ("fast" if graph_mode == "fast" else "all_pairs")
    _prune_forecast_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    _FORECAST_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "graph_mode": graph_mode,
        "nli_mode": nli_mode,
        "created_at": now,
        "updated_at": now,
        "result": None,
        "error": None,
        "progress": [{
            "stage": "queued",
            "message": "교정 작업을 대기열에 등록했습니다.",
            "state": "active",
            "started_at": now,
        }],
    }
    task = asyncio.create_task(_run_forecast_job(job_id, req))
    _FORECAST_JOB_TASKS[job_id] = task
    task.add_done_callback(lambda _: _FORECAST_JOB_TASKS.pop(job_id, None))
    return JSONResponse(
        status_code=202,
        content={
            **_forecast_job_public(_FORECAST_JOBS[job_id], include_result=False),
            "status_url": f"/api/forecasts/operating-profit/correct/jobs/{job_id}",
        },
    )


@app.get("/api/forecasts/operating-profit/correct/jobs/{job_id}")
async def api_operating_profit_correct_job(job_id: str):
    """Return the current state/result of an async correction job."""
    _prune_forecast_jobs()
    job = _FORECAST_JOBS.get((job_id or "").strip())
    if job is None:
        raise HTTPException(status_code=404, detail="교정 작업을 찾을 수 없거나 만료되었습니다.")
    return JSONResponse(_forecast_job_public(job))


@app.get("/")
async def index():
    return RedirectResponse("/forecast-correction")


@app.get("/forecast-correction")
async def forecast_correction_page():
    return FileResponse(str(_STATIC / "forecast_correction.html"))


class FactGraphPreviewRequest(BaseModel):
    markdown_text: str
    graph_mode: Optional[str] = None
    nli_mode: Optional[str] = None
    company: Optional[str] = None
    date_label: Optional[str] = None


@app.post("/api/fact-graph-preview")
async def api_fact_graph_preview(req: FactGraphPreviewRequest):
    markdown_text = _require_nonblank(req.markdown_text, field="markdown_text")
    graph_mode = "llm" if (req.graph_mode or "").strip().lower() == "llm" else "fast"
    nli_mode = _validate_optional_choice(
        req.nli_mode, field="nli_mode", choices={"all_pairs", "fast"},
    ) or ("fast" if graph_mode == "fast" else "all_pairs")
    payload, graph_cache = await _get_factreasoner_graph(markdown_text, nli_mode=nli_mode)
    payload["company"] = _clean_optional_string(req.company) or payload.get("company")
    payload["date_label"] = _clean_optional_string(req.date_label) or payload.get("date_label")
    payload["graph_cache"] = graph_cache
    return JSONResponse(payload)


class FactGraphPropagateRequest(BaseModel):
    markdown_text: str
    nodes: list[dict] = []
    edges: list[dict] = []
    target_node_id: str
    corrected_statement: Optional[str] = None
    max_depth: Optional[int] = None


@app.post("/api/fact-graph/propagate-correction")
async def api_fact_graph_propagate_correction(req: FactGraphPropagateRequest):
    markdown_text = _require_nonblank(req.markdown_text, field="markdown_text")
    target_node_id = (req.target_node_id or "").strip()
    if not target_node_id:
        raise HTTPException(status_code=400, detail="target_node_id는 필수입니다.")
    result = await propagate_correction(
        markdown_text=markdown_text,
        nodes=req.nodes,
        edges=req.edges,
        target_node_id=target_node_id,
        corrected_statement=req.corrected_statement,
        max_depth=req.max_depth,
    )
    return JSONResponse(result)


class FactGraphJudgeRequest(BaseModel):
    nodes: list[dict] = []
    edges: list[dict] = []
    target_node_id: str
    markdown_text: Optional[str] = None
    corrected_statement: Optional[str] = None


@app.post("/api/fact-graph/judge-atom")
async def api_fact_graph_judge_atom(req: FactGraphJudgeRequest):
    target_node_id = (req.target_node_id or "").strip()
    if not target_node_id:
        raise HTTPException(status_code=400, detail="target_node_id는 필수입니다.")
    result = await judge_atom(
        nodes=req.nodes,
        target_node_id=target_node_id,
        markdown_text=req.markdown_text,
        edges=req.edges,
        corrected_statement=req.corrected_statement,
    )
    return JSONResponse(result)


@app.get("/api/factreasoner/embedding/status")
async def api_factreasoner_embedding_status():
    return JSONResponse(embedding_status())


@app.get("/api/llm/status")
async def api_llm_status():
    base_url = chat_base_url().rstrip("/")
    model = verifier_model()
    status: dict[str, object] = {
        "base_url": base_url,
        "model": model,
        "online": False,
        "status": "offline",
    }
    try:
        async with httpx.AsyncClient(timeout=2.5) as cli:
            resp = await cli.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {chat_api_key()}"},
            )
        status["http_status"] = resp.status_code
        if resp.status_code < 400:
            status["online"] = True
            status["status"] = "online"
        else:
            status["error"] = resp.text[:500]
    except Exception as exc:
        status["error"] = str(exc)
    return JSONResponse(status)


@app.get("/api/status")
async def api_status():
    llm = await api_llm_status()
    return JSONResponse({
        "service": "ripple-repair-api",
        "llm": json.loads(llm.body.decode("utf-8")),
        "embedding": embedding_status(),
        "factreasoner_model": factreasoner_model(),
    })


if __name__ == "__main__":
    host = os.getenv("WEB_APP_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_APP_PORT", "8200"))
    uvicorn.run(
        "web_app.main:app",
        host=host,
        port=port,
        reload=True,
        app_dir=str(_ROOT),
    )
