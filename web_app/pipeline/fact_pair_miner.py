"""Low-cost semantic pair mining for FactReasoner relation extraction.

The fast preset follows IBM FactReasoner's NLI cost-control idea while adapting
it to RippleRepair's atom-to-atom graph.  It never removes atoms: it only
reduces the pairs presented to the expensive relation LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any
import json
import os
import re
import sqlite3
import time

import numpy as np


_ROOT = Path(__file__).resolve().parents[2]
_MODEL_LOCK = Lock()
_EMBED_LOCK = Lock()
_CACHE_LOCK = Lock()
_EMBEDDING_MODEL: Any = None
_EMBEDDING_BACKEND = ""
_EMBEDDING_EFFECTIVE_BATCH_SIZE = 0
_EMBEDDING_ERROR = ""
_EMBEDDING_LOADED_AT = 0.0
_PROMPT_CACHE_VERSION = "fact-pair-v1"


@dataclass(frozen=True)
class PairMiningResult:
    pairs: list[dict[str, Any]]
    alias_groups: dict[str, list[str]]
    stats: dict[str, Any]
    warnings: list[str]


def configured_nli_mode(value: str | None = None) -> str:
    mode = str(value or os.getenv("FACTREASONER_NLI_MODE", "all_pairs")).strip().lower()
    return "fast" if mode == "fast" else "all_pairs"


def embedding_model_name() -> str:
    return os.getenv(
        "FACTREASONER_EMBEDDING_MODEL",
        "Qwen/Qwen3-Embedding-0.6B",
    ).strip()


def embedding_backend_name() -> str:
    configured = os.getenv("FACTREASONER_EMBEDDING_BACKEND", "auto").strip().lower()
    if configured in {"sentence-transformers", "fastembed-onnx"}:
        return configured
    return "sentence-transformers" if "qwen3-embedding" in embedding_model_name().lower() else "fastembed-onnx"


def embedding_device_name() -> str:
    return os.getenv("FACTREASONER_EMBEDDING_DEVICE", "cpu").strip() or "cpu"


def embedding_batch_size() -> int:
    return max(1, int(os.getenv("FACTREASONER_EMBEDDING_BATCH_SIZE", "256")))


def _embedding_cache_dir() -> Path:
    raw = os.getenv("FACTREASONER_EMBEDDING_CACHE_DIR", "").strip()
    return Path(raw).expanduser() if raw else _ROOT / ".cache" / "embeddings"


def _verdict_cache_path() -> Path:
    raw = os.getenv("FACTREASONER_NLI_CACHE_PATH", "").strip()
    return Path(raw).expanduser() if raw else _ROOT / ".cache" / "factreasoner" / "nli.sqlite3"


def _embedding_local_files_only() -> bool:
    return os.getenv("FACTREASONER_EMBEDDING_LOCAL_FILES_ONLY", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def warm_embedding_model() -> dict[str, Any]:
    """Load one multilingual embedding model into the web process."""
    global _EMBEDDING_MODEL, _EMBEDDING_BACKEND, _EMBEDDING_ERROR, _EMBEDDING_LOADED_AT
    if _EMBEDDING_MODEL is not None:
        return embedding_status()
    with _MODEL_LOCK:
        if _EMBEDDING_MODEL is not None:
            return embedding_status()
        try:
            model_name = embedding_model_name()
            backend = embedding_backend_name()
            cache_dir = _embedding_cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            threads = max(1, int(os.getenv("FACTREASONER_EMBEDDING_THREADS", "8")))
            if backend == "sentence-transformers":
                import torch
                from sentence_transformers import SentenceTransformer

                torch.set_num_threads(threads)
                _EMBEDDING_MODEL = SentenceTransformer(
                    model_name,
                    device=embedding_device_name(),
                    cache_folder=str(cache_dir),
                    local_files_only=_embedding_local_files_only(),
                )
                # Fact atoms are short. Bounding the context prevents a malformed
                # long atom from monopolizing CPU inference.
                _EMBEDDING_MODEL.max_seq_length = max(
                    64, int(os.getenv("FACTREASONER_EMBEDDING_MAX_LENGTH", "512")),
                )
                _EMBEDDING_MODEL.encode(
                    ["영업이익 전망"], batch_size=1, normalize_embeddings=True,
                    show_progress_bar=False, convert_to_numpy=True,
                )
            else:
                from fastembed import TextEmbedding
                from fastembed.common.model_description import ModelSource, PoolingType

                supported = {str(row.get("model")) for row in TextEmbedding.list_supported_models()}
                if model_name not in supported:
                    try:
                        TextEmbedding.add_custom_model(
                            model=model_name,
                            pooling=PoolingType.MEAN,
                            normalization=True,
                            sources=ModelSource(hf=model_name),
                            dim=384,
                            model_file="onnx/model.onnx",
                        )
                    except ValueError as exc:
                        if "already" not in str(exc).lower():
                            raise
                _EMBEDDING_MODEL = TextEmbedding(
                    model_name=model_name, cache_dir=str(cache_dir), threads=threads,
                )
                list(_EMBEDDING_MODEL.embed(["query: 영업이익 전망"], batch_size=1))
            _EMBEDDING_BACKEND = backend
            _EMBEDDING_ERROR = ""
            _EMBEDDING_LOADED_AT = time.time()
        except Exception as exc:  # optional dependency/model download boundary
            _EMBEDDING_MODEL = None
            _EMBEDDING_BACKEND = ""
            _EMBEDDING_ERROR = f"{type(exc).__name__}: {exc}"
    return embedding_status()


def embedding_status() -> dict[str, Any]:
    return {
        "model": embedding_model_name(),
        "backend": _EMBEDDING_BACKEND or embedding_backend_name(),
        "device": str(getattr(_EMBEDDING_MODEL, "device", embedding_device_name())),
        "batch_size": embedding_batch_size(),
        "effective_batch_size": _EMBEDDING_EFFECTIVE_BATCH_SIZE or None,
        "online": _EMBEDDING_MODEL is not None,
        "error": _EMBEDDING_ERROR or None,
        "loaded_at": _EMBEDDING_LOADED_AT or None,
        "cache_dir": str(_embedding_cache_dir()),
        "local_files_only": _embedding_local_files_only(),
    }


def _embed(texts: list[str]) -> np.ndarray:
    global _EMBEDDING_EFFECTIVE_BATCH_SIZE
    status = warm_embedding_model()
    if not status["online"]:
        raise RuntimeError(str(status.get("error") or "embedding model unavailable"))
    inputs = [text.strip() for text in texts]
    batch_size = min(embedding_batch_size(), max(1, len(inputs)))
    if _EMBEDDING_BACKEND == "sentence-transformers":
        while True:
            try:
                with _EMBED_LOCK:
                    encoded = _EMBEDDING_MODEL.encode(
                        inputs, batch_size=batch_size, normalize_embeddings=True,
                        show_progress_bar=False, convert_to_numpy=True,
                    )
                vectors = np.asarray(encoded, dtype=np.float32)
                _EMBEDDING_EFFECTIVE_BATCH_SIZE = batch_size
                break
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() or batch_size <= 1:
                    raise
                batch_size = max(1, batch_size // 2)
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
    else:
        # FastEmbed's legacy multilingual model was trained with this prefix.
        with _EMBED_LOCK:
            vectors = np.asarray(
                list(_EMBEDDING_MODEL.embed(
                    [f"query: {text}" for text in inputs], batch_size=batch_size,
                )),
                dtype=np.float32,
            )
        _EMBEDDING_EFFECTIVE_BATCH_SIZE = batch_size
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def _norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _numeric_fact_signature(atom: dict[str, Any]) -> tuple[str, ...]:
    """Return a conservative identity signature for near-duplicate merging.

    Embedding similarity must never merge two scenario/date facts merely
    because their prose template is alike. Exact value/unit/period identity is
    deliberately preferred over clever unit conversion here; relation mining
    can still connect equivalent differently formatted atoms without collapsing
    their audit anchors.
    """
    value = _norm(atom.get("value"))
    if value:
        return (
            re.sub(r"[,]", "", value),
            _norm(atom.get("unit")),
            _norm(atom.get("period")),
        )
    numbers = tuple(
        re.sub(r"[,]", "", token)
        for token in re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?", str(atom.get("statement") or ""))
    )
    return numbers


def _dedup_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_metric, right_metric = _norm(left.get("metric")), _norm(right.get("metric"))
    if left_metric and right_metric and left_metric != right_metric:
        return False
    left_signature, right_signature = _numeric_fact_signature(left), _numeric_fact_signature(right)
    if left_signature or right_signature:
        if left_signature != right_signature:
            return False
    left_polarity, right_polarity = _norm(left.get("polarity")), _norm(right.get("polarity"))
    if left_polarity and right_polarity and left_polarity != right_polarity:
        return False
    return True


def _chunk_positions(atoms: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for atom in atoms:
        chunk_id = str(atom.get("chunk_id") or "")
        if chunk_id and chunk_id not in out:
            out[chunk_id] = len(out)
    return out


def _provenance_reasons(
    left: dict[str, Any], right: dict[str, Any], *, chunk_positions: dict[str, int], neighbor_window: int,
) -> list[str]:
    reasons: list[str] = []
    left_chunk, right_chunk = str(left.get("chunk_id") or ""), str(right.get("chunk_id") or "")
    if left_chunk and left_chunk == right_chunk:
        reasons.append("same_chunk")
    elif left_chunk in chunk_positions and right_chunk in chunk_positions:
        if abs(chunk_positions[left_chunk] - chunk_positions[right_chunk]) <= neighbor_window:
            reasons.append("neighbor_chunk")
    left_metric, right_metric = _norm(left.get("metric")), _norm(right.get("metric"))
    if left_metric and left_metric == right_metric:
        reasons.append("same_metric")
    left_period, right_period = _norm(left.get("period")), _norm(right.get("period"))
    if left_period and left_period == right_period:
        reasons.append("same_period")
    left_subject, right_subject = _norm(left.get("subject")), _norm(right.get("subject"))
    if left_subject and left_subject == right_subject:
        reasons.append("same_subject")
    return reasons


def mine_relation_pairs(atom_items: list[dict[str, Any]], *, nli_mode: str | None) -> PairMiningResult:
    """Return LLM relation candidates while preserving every original atom."""
    started = time.monotonic()
    mode = configured_nli_mode(nli_mode)
    count = len(atom_items)
    enumerated = count * (count - 1) // 2
    identity = {str(atom.get("id") or ""): [str(atom.get("id") or "")] for atom in atom_items}
    if mode != "fast" or count < 2:
        pairs = [
            {"source": str(atom_items[i].get("id") or ""), "target": str(atom_items[j].get("id") or ""),
             "similarity": None, "reasons": ["all_pairs"]}
            for i in range(count) for j in range(i + 1, count)
        ]
        return PairMiningResult(pairs, identity, {
            "requested_mode": mode, "effective_mode": "all_pairs", "enumerated_pairs": enumerated,
            "representative_pairs": enumerated, "kept_pairs": len(pairs), "pruned_pairs": 0,
            "deduplicated_atoms": 0, "embedding": embedding_status(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }, [])

    warnings: list[str] = []
    try:
        vectors = _embed([str(atom.get("statement") or "") for atom in atom_items])
    except Exception as exc:
        warning = f"embedding gate unavailable; all_pairs fallback: {type(exc).__name__}: {exc}"
        warnings.append(warning)
        fallback = mine_relation_pairs(atom_items, nli_mode="all_pairs")
        stats = dict(fallback.stats)
        stats.update({"requested_mode": "fast", "fallback_reason": warning, "embedding": embedding_status()})
        return PairMiningResult(fallback.pairs, fallback.alias_groups, stats, warnings)

    # IBM's 0.20 default is model-specific. Qwen has a much higher cosine floor
    # inside one financial document, so thresholds are calibrated separately.
    gate = float(os.getenv("FACTREASONER_NLI_GATE_THRESHOLD", "0.70"))
    strong_gate = float(os.getenv("FACTREASONER_NLI_STRONG_THRESHOLD", "0.80"))
    dedup = float(os.getenv("FACTREASONER_NLI_DEDUP_THRESHOLD", "0.92"))
    neighbor_window = max(0, int(os.getenv("FACTREASONER_NLI_NEIGHBOR_WINDOW", "1")))
    similarities = vectors @ vectors.T

    # Union near-duplicate statements for relation mining only. Original atoms
    # remain in the graph and receive expanded edges after the LLM turn.
    parent = list(range(count))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    dedup_blocked_fact_mismatch = 0
    for i in range(count):
        for j in range(i + 1, count):
            if float(similarities[i, j]) < dedup:
                continue
            if _dedup_compatible(atom_items[i], atom_items[j]):
                union(i, j)
            else:
                dedup_blocked_fact_mismatch += 1

    groups_by_index: dict[int, list[int]] = {}
    for index in range(count):
        groups_by_index.setdefault(find(index), []).append(index)
    representatives = sorted(groups_by_index)
    alias_groups = {
        str(atom_items[rep].get("id") or ""): [str(atom_items[idx].get("id") or "") for idx in members]
        for rep, members in groups_by_index.items()
    }
    chunk_positions = _chunk_positions(atom_items)
    pairs: list[dict[str, Any]] = []
    for pos, left_rep in enumerate(representatives):
        for right_rep in representatives[pos + 1:]:
            best_similarity = max(
                float(similarities[i, j])
                for i in groups_by_index[left_rep] for j in groups_by_index[right_rep]
            )
            reasons: list[str] = []
            for i in groups_by_index[left_rep]:
                for j in groups_by_index[right_rep]:
                    reasons.extend(_provenance_reasons(
                        atom_items[i], atom_items[j], chunk_positions=chunk_positions,
                        neighbor_window=neighbor_window,
                    ))
            reasons = list(dict.fromkeys(reasons))
            hard_provenance = [reason for reason in reasons if reason in {"same_chunk", "neighbor_chunk"}]
            shared_features = [
                reason for reason in reasons
                if reason in {"same_metric", "same_period", "same_subject"}
            ]
            kept_reasons = list(hard_provenance)
            if best_similarity >= gate and shared_features:
                kept_reasons.extend(shared_features)
                kept_reasons.append("embedding_gate")
            elif best_similarity >= max(gate, strong_gate):
                # Strong semantic match can recover causal/derived relations
                # even when atom metadata is incomplete or uses other metrics.
                kept_reasons.append("strong_embedding_gate")
            if not kept_reasons:
                continue
            pairs.append({
                "source": str(atom_items[left_rep].get("id") or ""),
                "target": str(atom_items[right_rep].get("id") or ""),
                "similarity": round(best_similarity, 6),
                "reasons": list(dict.fromkeys(kept_reasons)),
            })
    representative_pairs = len(representatives) * (len(representatives) - 1) // 2
    pair_similarities = sorted(
        max(float(similarities[i, j]) for i in groups_by_index[left] for j in groups_by_index[right])
        for pos, left in enumerate(representatives) for right in representatives[pos + 1:]
    )

    def percentile(fraction: float) -> float | None:
        if not pair_similarities:
            return None
        index = min(len(pair_similarities) - 1, round((len(pair_similarities) - 1) * fraction))
        return round(pair_similarities[index], 6)

    stats = {
        "requested_mode": "fast", "effective_mode": "fast",
        "enumerated_pairs": enumerated, "representative_pairs": representative_pairs,
        "kept_pairs": len(pairs), "pruned_pairs": max(0, representative_pairs - len(pairs)),
        "deduplicated_atoms": count - len(representatives), "gate_threshold": gate,
        "strong_gate_threshold": strong_gate,
        "dedup_blocked_fact_mismatch": dedup_blocked_fact_mismatch,
        "dedup_threshold": dedup, "neighbor_window": neighbor_window,
        "similarity_distribution": {
            "min": percentile(0.0), "p50": percentile(0.5),
            "p90": percentile(0.9), "max": percentile(1.0),
        },
        "embedding": embedding_status(),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }
    return PairMiningResult(pairs, alias_groups, stats, warnings)


def expand_alias_edges(edges: list[dict[str, Any]], alias_groups: dict[str, list[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        sources = alias_groups.get(str(edge.get("source") or ""), [str(edge.get("source") or "")])
        targets = alias_groups.get(str(edge.get("target") or ""), [str(edge.get("target") or "")])
        for source in sources:
            for target in targets:
                relation = str(edge.get("relation") or "")
                key = (source, target, relation)
                if not source or not target or source == target or key in seen:
                    continue
                seen.add(key)
                expanded = dict(edge)
                expanded["source"], expanded["target"] = source, target
                out.append(expanded)
    return out


def alias_link_edges(alias_groups: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Reconnect original near-duplicate atoms after representative mining."""
    edges: list[dict[str, Any]] = []
    for representative, members in alias_groups.items():
        for member in members:
            if member == representative:
                continue
            edges.extend([
                {
                    "source": member, "target": representative, "relation": "supports",
                    "reason": "embedding near-duplicate atom mention", "confidence": 0.92,
                },
                {
                    "source": member, "target": representative, "relation": "same_metric",
                    "reason": "embedding near-duplicate atom mention", "confidence": 0.92,
                },
            ])
    return edges


def verdict_cache_key(kind: str, payload: Any, *, model: str) -> str:
    raw = json.dumps(
        {"version": _PROMPT_CACHE_VERSION, "kind": kind, "model": model, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _init_cache(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS verdict_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
    )


def get_cached_verdict(cache_key: str) -> dict[str, Any] | None:
    path = _verdict_cache_path()
    if not path.exists():
        return None
    ttl = max(60, int(os.getenv("FACTREASONER_NLI_CACHE_TTL_SECONDS", "604800")))
    try:
        with _CACHE_LOCK, sqlite3.connect(path, timeout=10) as connection:
            _init_cache(connection)
            row = connection.execute(
                "SELECT payload, created_at FROM verdict_cache WHERE cache_key = ?", (cache_key,),
            ).fetchone()
        if row is None or time.time() - float(row[1]) > ttl:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except (sqlite3.Error, ValueError, json.JSONDecodeError):
        return None


def put_cached_verdict(cache_key: str, payload: dict[str, Any]) -> None:
    path = _verdict_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK, sqlite3.connect(path, timeout=10) as connection:
            _init_cache(connection)
            connection.execute(
                "INSERT OR REPLACE INTO verdict_cache(cache_key, payload, created_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            connection.commit()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return
