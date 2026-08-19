"""Deterministic operating-profit forecast digit-scale correction.

The parser accepts the Markdown/table/dataframe forms found in generated
finance reports.  It preserves the original Markdown and changes only numeric
tokens when one power-of-ten scale factor is strongly supported by consensus.
"""
from __future__ import annotations

from datetime import date
import math
import re
from typing import Any


_LABELS = ("상방", "중간", "하방")
_FORECAST_HEADER_RE = re.compile(
    r"(?P<year>20\d{2})년(?:\s*연간)?\s*영업이익\s*전망", re.IGNORECASE,
)
_PERIOD_FORECAST_HEADER_RE = re.compile(
    r"(?P<year>20\d{2})년\s*(?:"
    r"(?P<period>상반기|하반기)\s*(?:영업이익\s*)?전망"
    r"|(?:연간\s*)?영업이익\s*전망)",
    re.IGNORECASE,
)
_SCENARIO_LINE_RE = re.compile(
    r"^(?P<prefix>.*?(?P<label>상방|중간|하방)(?:\s*시나리오)?"
    r"(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*)"
    r"(?P<value>[+-]?[0-9][0-9,]*(?:\.\d+)?)"
    r"(?P<unit>\s*(?:조원|억원|원)?)"
    r"(?P<trailing>.*)$"
)
_ANNUAL_VALUE_RE = re.compile(
    r"^(?P<prefix>.*?20\d{2}년(?:\s*연간)?\s*영업이익\s*전망"
    r"(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*)"
    r"(?P<value>[+-]?[0-9][0-9,]*(?:\.\d+)?)"
    r"(?P<unit>\s*(?:조원|억원|원)?)"
    r"(?P<trailing>.*)$",
    re.IGNORECASE,
)
_HALF_YEAR_VALUE_RE = re.compile(
    r"^(?P<prefix>.*?(?P<year>20\d{2})년\s*(?P<period>상반기|하반기)\s*전망"
    r"(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*)"
    r"(?P<value>[+-]?[0-9][0-9,]*(?:\.\d+)?)"
    r"(?P<unit>\s*(?:조원|억원|원)?)"
    r"(?P<trailing>.*)$",
    re.IGNORECASE,
)
_CONSENSUS_HEADING_RE = re.compile(r"최근\s*컨센서스\s*전망치\s*분석")
_DATED_AMOUNT_RE = re.compile(
    r"(?P<date>20\d{2}-\d{2}-\d{2})"
    r"(?:\s*\|\s*|\s+)"
    r"(?P<amount>[+-]?(?:[0-9]+(?:\.[0-9]+)?[eE][+-]?\d+|[0-9][0-9,]*(?:\.\d+)?))"
)
_PROSE_DATED_AMOUNT_RE = re.compile(
    r"(?P<year>20\d{2})년\s*(?P<month>1[0-2]|0?[1-9])월"
    r"(?:\s*(?P<day>3[01]|[12]\d|0?[1-9])일)?"
    r"\s*(?P<amount>[+-]?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>조원|억원|원)"
)
_PROSE_CONSENSUS_AMOUNT_RE = re.compile(
    r"(?:현재\s*)?컨센서스(?:는|은|이)?\s*"
    r"(?P<amount>[+-]?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>조원|억원|원)"
)
_FACTORS = (0.001, 0.01, 0.1, 10.0, 100.0, 1000.0)
_INVARIANT_FACTORS = (0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)


def _number(text: str) -> float:
    value = float(text.replace(",", ""))
    if not math.isfinite(value):
        raise ValueError("non-finite monetary value")
    return value


def _to_won(value: float, unit: str) -> float:
    clean = unit.strip()
    if clean == "조원":
        return value * 1_0000_0000_0000
    if clean == "억원":
        return value * 1_0000_0000
    return value


def _render_won(value_won: float, unit: str, template: str = "") -> str:
    clean = unit.strip()
    value = value_won
    if clean == "조원":
        value /= 1_0000_0000_0000
    elif clean == "억원":
        value /= 1_0000_0000
    grouped = "," in template
    if float(value).is_integer():
        return f"{int(value):,}" if grouped else str(int(value))
    rendered = (f"{value:,.4f}" if grouped else f"{value:.4f}").rstrip("0").rstrip(".")
    return rendered


def _unit_from_header(header: str) -> str:
    """Return the monetary unit declared by a table/dataframe column header."""
    compact = re.sub(r"\s+", "", header or "").lower()
    if "조원" in compact:
        return "조원"
    if "억원" in compact:
        return "억원"
    # ``Amount (원)`` and Korean equivalents are common in archived reports.
    if re.search(r"(?:amount|금액|영업이익).*?[\[(]원[\])]", compact):
        return "원"
    return ""


def _explicit_year(text: str) -> str | None:
    match = re.search(r"(?<![-\d])(20\d{2})(?:년|[EF](?![A-Za-z]))?", text or "", re.IGNORECASE)
    return match.group(1) if match else None


def _consensus_target_year(text: str) -> str | None:
    """Return the fiscal year nearest an operating-profit consensus phrase."""
    clean = re.sub(r"20\d{2}-\d{2}-\d{2}", "", text or "")
    direct = re.findall(
        r"(?<![-\d])(20\d{2})(?:년|[EF])?\s*(?:연간\s*)?영업이익\s*(?:컨센서스|전망치)",
        clean,
        re.IGNORECASE,
    )
    if direct:
        return direct[-1]
    reverse = re.search(
        r"컨센서스(?:\s*대상)?\s*[:\-]?\s*(20\d{2})(?:년|[EF])?\s*(?:연간\s*)?영업이익",
        clean,
        re.IGNORECASE,
    )
    if reverse:
        return reverse.group(1)
    return None


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _leading_forecast_match(
    line: str, pattern: re.Pattern[str] = _FORECAST_HEADER_RE,
) -> re.Match[str] | None:
    """Match a forecast block header/value, not a news sentence mentioning one."""
    match = pattern.search(line or "")
    if not match:
        return None
    prefix = line[:match.start()]
    prefix = re.sub(r"^\s*[#>*+\-]*\s*", "", prefix)
    prefix = re.sub(r"^\d+\.\s*", "", prefix)
    prefix = prefix.replace("*", "").strip()
    return match if not prefix else None


def extract_consensus_blocks(markdown_text: str) -> list[dict[str, Any]]:
    """Extract dated consensus series grouped by their Markdown section."""
    lines = markdown_text.splitlines()
    starts = [idx for idx, line in enumerate(lines) if _CONSENSUS_HEADING_RE.search(line)]
    blocks: list[dict[str, Any]] = []
    for start_pos, start in enumerate(starts):
        explicit_year = _explicit_year(lines[start])
        explicit_year_source = "heading" if explicit_year else None
        year_conflict = False
        next_consensus = starts[start_pos + 1] if start_pos + 1 < len(starts) else len(lines)
        end = next_consensus
        for idx in range(start + 1, next_consensus):
            if _leading_forecast_match(lines[idx]):
                end = idx
                break
        entries: list[dict[str, Any]] = []
        table_value_index: int | None = None
        table_crosscheck_indexes: list[int] = []
        table_units: dict[int, str] = {}
        table_value_unit = ""
        table_value_year: str | None = None
        dataframe_value_unit = ""
        issues: list[dict[str, Any]] = []
        crosscheck_mismatch_lines: list[int] = []
        for idx in range(start + 1, end):
            line = lines[idx]
            candidate_year = _consensus_target_year(line)
            if candidate_year:
                if explicit_year and candidate_year != explicit_year:
                    year_conflict = True
                    issues.append({
                        "line": idx + 1,
                        "reason": f"컨센서스 대상 연도가 {explicit_year}와 {candidate_year}로 충돌합니다.",
                        "source": "consensus_extraction",
                    })
                elif not explicit_year:
                    explicit_year = candidate_year
                    explicit_year_source = "section_text"
            if "|" in line:
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if any(cell.lower() == "date" or "날짜" in cell for cell in cells):
                    op_indexes = [
                        pos for pos, cell in enumerate(cells)
                        if "영업이익" in cell and "률" not in cell
                    ]
                    if len(op_indexes) == 1:
                        table_value_index = op_indexes[0]
                    else:
                        amount_indexes = [
                            pos for pos, cell in enumerate(cells)
                            if "amount" in cell.lower() or "금액" in cell
                        ]
                        if len(amount_indexes) == 1:
                            table_value_index = amount_indexes[0]
                        elif amount_indexes:
                            won_indexes = [
                                pos for pos in amount_indexes if _unit_from_header(cells[pos]) == "원"
                            ]
                            table_value_index = won_indexes[0] if len(won_indexes) == 1 else amount_indexes[0]
                            table_crosscheck_indexes = [
                                pos for pos in amount_indexes if pos != table_value_index
                            ]
                        else:
                            table_value_index = None
                    if table_value_index is not None:
                        table_units = {
                            pos: _unit_from_header(cells[pos])
                            for pos in [table_value_index, *table_crosscheck_indexes]
                        }
                        table_value_unit = _unit_from_header(cells[table_value_index])
                        table_value_year = _explicit_year(cells[table_value_index])
                    continue
                if table_value_index is not None and len(cells) > table_value_index:
                    try:
                        parsed_date = date.fromisoformat(cells[0])
                        raw_amount = _number(cells[table_value_index])
                        amount = _to_won(raw_amount, table_value_unit)
                        crosscheck_values = [
                            _to_won(_number(cells[pos]), table_units.get(pos, ""))
                            for pos in table_crosscheck_indexes
                            if pos < len(cells) and cells[pos] and cells[pos] != "-"
                        ]
                    except (ValueError, OverflowError):
                        pass
                    else:
                        inconsistent = any(
                            abs(other - amount) / max(1.0, abs(other), abs(amount)) > 0.001
                            for other in crosscheck_values
                        )
                        if inconsistent:
                            crosscheck_mismatch_lines.append(idx + 1)
                            continue
                        if amount != 0:
                            entries.append({
                                "date": parsed_date.isoformat(), "amount_won": amount,
                                "line": idx + 1, "raw": line.strip(),
                                "declared_unit": table_value_unit or "원",
                            })
                        continue
                # Do not guess the value column in an unrecognised multi-column table.
                if re.search(r"20\d{2}-\d{2}-\d{2}", line):
                    continue
            if re.search(r"\bdate\b|날짜", line, re.IGNORECASE) and re.search(r"\bamount\b|금액|영업이익", line, re.IGNORECASE):
                dataframe_value_unit = _unit_from_header(line)
                if explicit_year is None:
                    explicit_year = _explicit_year(re.sub(r"20\d{2}-\d{2}-\d{2}", "", line))
            match = _DATED_AMOUNT_RE.search(line)
            if match:
                try:
                    parsed_date = date.fromisoformat(match.group("date"))
                    amount = _to_won(_number(match.group("amount")), dataframe_value_unit)
                except (ValueError, OverflowError):
                    continue
                if amount == 0:
                    continue
                entries.append({
                    "date": parsed_date.isoformat(),
                    "amount_won": amount,
                    "line": idx + 1,
                    "raw": line.strip(),
                    "declared_unit": dataframe_value_unit or "원",
                })
                continue
            for prose in _PROSE_DATED_AMOUNT_RE.finditer(line):
                try:
                    parsed_date = date(
                        int(prose.group("year")),
                        int(prose.group("month")),
                        int(prose.group("day") or 1),
                    )
                    amount = _to_won(_number(prose.group("amount")), prose.group("unit"))
                except (ValueError, OverflowError):
                    continue
                if amount == 0:
                    continue
                entries.append({
                    "date": parsed_date.isoformat(),
                    "amount_won": amount,
                    "line": idx + 1,
                    "raw": line.strip(),
                    "declared_unit": prose.group("unit"),
                    "source": "prose_dated",
                })
        if not entries:
            for idx in range(start + 1, end):
                prose = _PROSE_CONSENSUS_AMOUNT_RE.search(lines[idx])
                if not prose:
                    continue
                try:
                    amount = _to_won(_number(prose.group("amount")), prose.group("unit"))
                except (ValueError, OverflowError):
                    continue
                if amount == 0:
                    continue
                year = explicit_year or "1970"
                entries.append({
                    "date": f"{year}-01-01",
                    "amount_won": amount,
                    "line": idx + 1,
                    "raw": lines[idx].strip(),
                    "declared_unit": prose.group("unit"),
                    "source": "prose_consensus",
                })
        if crosscheck_mismatch_lines:
            issues.append({
                "line": crosscheck_mismatch_lines[-1],
                "lines": crosscheck_mismatch_lines,
                "count": len(crosscheck_mismatch_lines),
                "reason": "같은 컨센서스 표의 원/억원 환산값이 서로 일치하지 않습니다.",
                "source": "consensus_extraction",
            })
        entries.sort(key=lambda item: (item["date"], item["line"]))
        next_forecast = next(
            (_leading_forecast_match(lines[idx]) for idx in range(end, next_consensus)
             if _leading_forecast_match(lines[idx])),
            None,
        )
        # end usually points at the first forecast heading that terminated this block.
        if next_forecast is None and end < len(lines):
            next_forecast = _leading_forecast_match(lines[end])
        blocks.append({
            "heading_line": start + 1,
            "heading": lines[start].strip(),
            "entries": entries,
            "latest": entries[-1] if entries else None,
            # An explicit year in the consensus heading/value column is
            # authoritative.  Inferring from the next forecast is only a
            # fallback for legacy reports without any year label.
            "forecast_year": None if year_conflict else explicit_year or table_value_year or (
                next_forecast.group("year") if next_forecast else None
            ),
            "year_source": (
                explicit_year_source if explicit_year else "value_column" if table_value_year
                else "next_forecast" if next_forecast else None
            ),
            "year_conflict": year_conflict,
            "issues": issues,
        })
    return blocks


def latest_consensus_won(markdown_text: str) -> float | None:
    """Return the latest consensus from the last populated consensus section."""
    populated = [
        block for block in extract_consensus_blocks(markdown_text)
        if block["latest"] and not block.get("year_conflict")
    ]
    return float(populated[-1]["latest"]["amount_won"]) if populated else None


def _consensus_for_line(
    blocks: list[dict[str, Any]], line_idx: int, forecast_year: str, explicit: float | None,
) -> dict[str, Any] | None:
    if explicit is not None:
        return {"amount_won": explicit, "date": None, "source": "request", "line": None}
    preceding_all = [
        block for block in blocks
        if block["latest"] and not block.get("year_conflict")
        and int(block["heading_line"]) - 1 <= line_idx
    ]
    matching = [block for block in preceding_all if block.get("forecast_year") == forecast_year]
    if matching:
        block = matching[-1]
    elif preceding_all and all(block.get("forecast_year") for block in preceding_all):
        # Every available section is explicitly tied to another forecast year.
        return None
    else:
        block = preceding_all[-1] if preceding_all else next(
            (item for item in reversed(blocks) if item["latest"] and not item.get("forecast_year")), None,
        )
    if block is None:
        return None
    return {
        **dict(block["latest"]),
        "source": "document",
        "heading_line": block["heading_line"],
    }


def _scale_decision(value_won: float, consensus_won: float) -> tuple[float, float, float] | None:
    if value_won == 0 or consensus_won == 0 or (value_won < 0) != (consensus_won < 0):
        return None
    value_abs = abs(value_won)
    consensus_abs = abs(consensus_won)
    ratio = max(value_abs, consensus_abs) / max(1.0, min(value_abs, consensus_abs))
    if ratio < 5:
        return None
    factor = min(_FACTORS, key=lambda item: abs(value_won * item - consensus_won))
    residual = abs(value_won * factor - consensus_won) / consensus_abs
    # Always return the nearest supported power-of-ten once the gap is large
    # enough; residual quality is left to LLM rereview / callers.
    return (factor, residual, ratio)


def _companion_half_year_changes(
    lines: list[str],
    *,
    year: str,
    annual_idx: int,
    annual_won: float,
    factor: float,
) -> list[dict[str, Any]]:
    """Scale same-year H1/H2 forecast lines when they sum to the annual value.

    GPT-style reports often use a compact block::

        - 2026년 상반기 전망: ...
        - 2026년 하반기 전망: ...
        - 2026년 영업이익 전망: ...

    Scaling only the annual line breaks H1+H2=annual and triggers LLM rejection.
    """
    window = range(max(0, annual_idx - 6), min(len(lines), annual_idx + 7))
    found: dict[str, tuple[int, re.Match[str], float]] = {}
    for idx in window:
        if idx == annual_idx:
            continue
        match = _HALF_YEAR_VALUE_RE.match(lines[idx].rstrip("\r\n"))
        if not match or match.group("year") != year:
            continue
        period = match.group("period")
        if period in found:
            continue
        won = _to_won(_number(match.group("value")), match.group("unit"))
        found[period] = (idx, match, won)
    if set(found) != {"상반기", "하반기"}:
        return []
    h1 = found["상반기"][2]
    h2 = found["하반기"][2]
    denom = max(1.0, abs(annual_won), abs(h1 + h2))
    if abs(annual_won - h1 - h2) / denom > 0.05:
        return []
    changes: list[dict[str, Any]] = []
    for period, (idx, match, old_value) in found.items():
        new_value = old_value * factor
        ending = _line_ending(lines[idx])
        new_line = (
            f"{match.group('prefix')}{_render_won(new_value, match.group('unit'), match.group('value'))}"
            f"{match.group('unit')}{match.group('trailing')}{ending}"
        )
        old_line = lines[idx].rstrip("\r\n")
        lines[idx] = new_line
        changes.append({
            "label": period,
            "line": idx + 1,
            "factor": factor,
            "old_value_won": old_value,
            "new_value_won": new_value,
            "old_line": old_line,
            "new_line": new_line.rstrip("\r\n"),
        })
    return changes


def evaluate_half_year_identity(
    markdown: str,
    *,
    year: str | None = None,
    tolerance: float = 0.02,
) -> dict[str, dict[str, Any]]:
    """Check 상반기 + 하반기 = 연간 for each year using signed sums."""
    halves: dict[str, dict[str, float]] = {}
    annuals: dict[str, float] = {}
    for line in markdown.splitlines():
        content = line.rstrip("\r\n")
        half = _HALF_YEAR_VALUE_RE.match(content)
        if half is not None:
            found_year = half.group("year")
            if year is not None and found_year != year:
                continue
            halves.setdefault(found_year, {})[half.group("period")] = _to_won(
                _number(half.group("value")), half.group("unit"),
            )
            continue
        annual = _ANNUAL_VALUE_RE.match(content)
        if annual is None:
            continue
        year_match = re.search(r"(20\d{2})", content)
        if year_match is None:
            continue
        found_year = year_match.group(1)
        if year is not None and found_year != year:
            continue
        annuals[found_year] = _to_won(_number(annual.group("value")), annual.group("unit"))

    reports: dict[str, dict[str, Any]] = {}
    for found_year, periods in halves.items():
        if "상반기" not in periods or "하반기" not in periods or found_year not in annuals:
            continue
        h1 = periods["상반기"]
        h2 = periods["하반기"]
        annual_value = annuals[found_year]
        expected = h1 + h2
        denom = max(abs(annual_value), abs(expected), 1.0)
        residual = abs(expected - annual_value) / denom
        reports[found_year] = {
            "year": found_year,
            "h1": h1,
            "h2": h2,
            "annual": annual_value,
            "sum": expected,
            "residual": residual,
            "identity_ok": residual <= tolerance,
            "has_h1_h2": True,
        }
    return reports


def scale_cluster_is_locked(
    markdown: str,
    applied: list[dict[str, Any]],
    *,
    residual_manual_review: float = 0.25,
) -> dict[str, Any]:
    """Keep a scale block when H1+H2=annual holds; flag large residuals."""
    scale_items = [item for item in applied if item.get("kind") == "scale_correction"]
    if not scale_items:
        return {"lock": False, "manual_review": False, "identities": {}, "max_residual": 0.0}
    years = {str(item.get("year") or "") for item in scale_items if item.get("year")}
    identities = evaluate_half_year_identity(markdown)
    relevant = {
        found_year: report
        for found_year, report in identities.items()
        if not years or found_year in years
    }
    if relevant and not all(bool(report.get("identity_ok")) for report in relevant.values()):
        return {
            "lock": False,
            "manual_review": True,
            "identities": relevant,
            "max_residual": max(float(report.get("residual") or 0.0) for report in relevant.values()),
        }
    max_residual = 0.0
    for item in scale_items:
        try:
            max_residual = max(max_residual, float(item.get("residual") or item.get("residual_ratio") or 0.0))
        except (TypeError, ValueError):
            continue
    return {
        "lock": True,
        "manual_review": max_residual > residual_manual_review,
        "identities": relevant,
        "max_residual": max_residual,
    }


def _scenario_records(lines: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Collect H1/H2/annual three-case blocks without changing Markdown."""
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for idx, line in enumerate(lines):
        header = _leading_forecast_match(line, _PERIOD_FORECAST_HEADER_RE)
        if not header:
            continue
        # A numbered navigation heading is followed by a concrete Markdown
        # block.  Let the concrete (later) header win instead of recording the
        # same scenario rows twice.
        if "시나리오 분석" in line and _ANNUAL_VALUE_RE.match(line.rstrip("\r\n")) is None:
            continue
        period = header.group("period")
        kind = "H1" if period == "상반기" else "H2" if period == "하반기" else "annual"
        parsed: dict[str, dict[str, Any]] = {}
        for line_idx in range(idx + 1, min(idx + 16, len(lines))):
            content = lines[line_idx].rstrip("\r\n")
            match = _SCENARIO_LINE_RE.match(content)
            if match:
                parsed[match.group("label")] = {
                    "line_idx": line_idx,
                    "match": match,
                    "won": _to_won(_number(match.group("value")), match.group("unit")),
                }
                if set(parsed) == set(_LABELS):
                    break
                continue
            if parsed and content.strip() and not re.match(r"^\s*[-+*]", content):
                break
        if set(parsed) == set(_LABELS):
            records.setdefault(header.group("year"), {})[kind] = {
                "header_line": idx + 1,
                "values": parsed,
            }
    return records


def _apply_half_year_invariant(
    lines: list[str],
    result: dict[str, Any],
    *,
    consensus_blocks: list[dict[str, Any]],
    explicit_consensus: float | None,
) -> set[str]:
    """Repair a uniquely identified power-of-ten error via H1 + H2 = annual.

    Consensus is the primary anchor.  This secondary invariant is useful when
    one whole half-year/annual block has a digit shift, and is deliberately
    conservative: a large baseline error, a <=2% repaired residual, scenario
    ordering, and a unique best block-factor tuple are all required.
    """
    corrected_years: set[str] = set()
    for year, blocks in _scenario_records(lines).items():
        if not all(kind in blocks for kind in ("H1", "H2", "annual")):
            continue
        values = {
            kind: {label: float(blocks[kind]["values"][label]["won"]) for label in _LABELS}
            for kind in ("H1", "H2", "annual")
        }

        def errors(factors: tuple[float, float, float]) -> tuple[float, dict[str, dict[str, float]]] | None:
            scaled = {
                kind: {label: values[kind][label] * factor for label in _LABELS}
                for kind, factor in zip(("H1", "H2", "annual"), factors)
            }
            if not all(
                scaled[kind]["상방"] >= scaled[kind]["중간"] >= scaled[kind]["하방"]
                for kind in scaled
            ):
                return None
            relative = [
                abs(scaled["annual"][label] - scaled["H1"][label] - scaled["H2"][label])
                / max(
                    1.0,
                    abs(scaled["annual"][label]),
                    abs(scaled["H1"][label] + scaled["H2"][label]),
                )
                for label in _LABELS
            ]
            return max(relative), scaled

        baseline_relative = [
            abs(values["annual"][label] - values["H1"][label] - values["H2"][label])
            / max(
                1.0,
                abs(values["annual"][label]),
                abs(values["H1"][label] + values["H2"][label]),
            )
            for label in _LABELS
        ]
        baseline_error = max(baseline_relative)
        baseline_ordered = all(
            values[kind]["상방"] >= values[kind]["중간"] >= values[kind]["하방"]
            for kind in values
        )
        if baseline_error < 0.20:
            if not baseline_ordered:
                result["review_items"].append({
                    "year": year,
                    "reason": "상·하반기/연간 시나리오 순서가 깨져 수동 검토가 필요합니다.",
                    "source": "half_year_sum_invariant",
                })
            continue

        annual_header_idx = int(blocks["annual"]["header_line"]) - 1
        consensus_info = _consensus_for_line(
            consensus_blocks, annual_header_idx, year, explicit_consensus,
        )
        annual_middle = values["annual"]["중간"]
        if consensus_info is None:
            result["review_items"].append({
                "year": year,
                "reason": "반기 합산 불변식 위반은 찾았지만 어느 블록이 틀렸는지 정할 컨센서스가 없습니다.",
                "source": "half_year_sum_invariant",
                "baseline_max_residual_ratio": baseline_error,
            })
            continue
        consensus_value = float(consensus_info["amount_won"])
        annual_ratio = abs(annual_middle) / max(1.0, abs(consensus_value))
        if (
            (annual_middle < 0) != (consensus_value < 0)
            or not 0.35 <= annual_ratio <= 2.8
        ):
            result["review_items"].append({
                "year": year,
                "reason": "연간 블록을 컨센서스로 고정할 수 없어 반기 합산 교정을 보류했습니다.",
                "source": "half_year_sum_invariant",
                "baseline_max_residual_ratio": baseline_error,
                "annual_consensus_ratio": annual_ratio,
            })
            continue

        # First try a strictly local repair.  For each broken scenario label,
        # annual-H2 or annual-H1 must identify exactly one half-year cell whose
        # current value differs by a supported power of ten.  This catches a
        # lone digit shift without changing the other eight correct cells.
        proposed_cells = {kind: dict(kind_values) for kind, kind_values in values.items()}
        cell_updates: list[tuple[str, str, float, float]] = []
        cell_solution = True
        for label, label_error in zip(_LABELS, baseline_relative):
            if label_error <= 0.02:
                continue
            candidates: list[tuple[str, float, float]] = []
            for kind, expected in (
                ("H1", values["annual"][label] - values["H2"][label]),
                ("H2", values["annual"][label] - values["H1"][label]),
            ):
                decision = _scale_decision(values[kind][label], expected)
                if decision is not None and decision[1] <= 0.02:
                    factor = decision[0]
                    candidates.append((kind, factor, values[kind][label] * factor))
            if len(candidates) != 1:
                cell_solution = False
                break
            kind, factor, new_value = candidates[0]
            proposed_cells[kind][label] = new_value
            cell_updates.append((kind, label, factor, new_value))

        proposed_cell_errors = [
            abs(
                proposed_cells["annual"][label]
                - proposed_cells["H1"][label]
                - proposed_cells["H2"][label]
            )
            / max(
                1.0,
                abs(proposed_cells["annual"][label]),
                abs(proposed_cells["H1"][label] + proposed_cells["H2"][label]),
            )
            for label in _LABELS
        ]
        cell_postcondition = bool(cell_updates) and cell_solution and all(
            proposed_cells[kind]["상방"] >= proposed_cells[kind]["중간"] >= proposed_cells[kind]["하방"]
            for kind in proposed_cells
        ) and max(proposed_cell_errors) <= 0.02
        if cell_postcondition:
            for kind in ("H1", "H2"):
                kind_updates = [item for item in cell_updates if item[0] == kind]
                if not kind_updates:
                    continue
                changes: list[dict[str, Any]] = []
                factor_by_label: dict[str, float] = {}
                for _, label, factor, new_value in kind_updates:
                    item = blocks[kind]["values"][label]
                    match = item["match"]
                    line_idx = int(item["line_idx"])
                    old_line = lines[line_idx]
                    ending = _line_ending(old_line)
                    new_line = (
                        f"{match.group('prefix')}"
                        f"{_render_won(new_value, match.group('unit'), match.group('value'))}"
                        f"{match.group('unit')}{match.group('trailing')}{ending}"
                    )
                    lines[line_idx] = new_line
                    factor_by_label[label] = factor
                    changes.append({
                        "label": label,
                        "line": line_idx + 1,
                        "factor": factor,
                        "old_value_won": item["won"],
                        "new_value_won": new_value,
                        "old_line": old_line.rstrip("\r\n"),
                        "new_line": new_line.rstrip("\r\n"),
                    })
                unique_factors = set(factor_by_label.values())
                result["corrections"].append({
                    "year": year,
                    "period": kind,
                    "factor": next(iter(unique_factors)) if len(unique_factors) == 1 else None,
                    "factors": factor_by_label,
                    "consensus_won": consensus_info["amount_won"],
                    "consensus_date": consensus_info.get("date"),
                    "consensus_source": "half_year_sum_invariant",
                    "residual_ratio": max(proposed_cell_errors),
                    "reason": "상반기+하반기=연간 합산 불변식이 유일하게 지목한 셀의 자릿수 오류를 교정했습니다.",
                    "changes": changes,
                })
            corrected_years.add(year)
            continue

        ranked: list[tuple[float, float, tuple[float, float, float], dict[str, dict[str, float]]]] = []
        for h1_factor in _INVARIANT_FACTORS:
            for h2_factor in _INVARIANT_FACTORS:
                # The annual block is already grounded by consensus.  Without
                # this pin, scaling annual versus both halves is inherently
                # ambiguous even if one choice changes fewer cells.
                for annual_factor in (1.0,):
                    factors = (h1_factor, h2_factor, annual_factor)
                    if factors == (1.0, 1.0, 1.0):
                        continue
                    evaluated = errors(factors)
                    if evaluated is None:
                        continue
                    max_error, scaled = evaluated
                    changed_blocks = sum(factor != 1.0 for factor in factors)
                    ranked.append((max_error + changed_blocks * 0.001, max_error, factors, scaled))
        ranked.sort(key=lambda item: (item[0], item[2]))
        best = ranked[0] if ranked else None
        tied = bool(
            best and len(ranked) > 1
            and abs(ranked[1][0] - best[0]) <= 1e-9
            and ranked[1][2] != best[2]
        )
        if best is None or best[1] > 0.02 or tied:
            result["review_items"].append({
                "year": year,
                "reason": "상반기+하반기=연간 불변식에서 유일한 10의 거듭제곱 교정을 확정하지 못했습니다.",
                "source": "half_year_sum_invariant",
                "baseline_max_residual_ratio": baseline_error,
            })
            continue

        _, residual, factors, scaled = best
        for kind, factor in zip(("H1", "H2", "annual"), factors):
            if factor == 1.0:
                continue
            changes: list[dict[str, Any]] = []
            for label in _LABELS:
                item = blocks[kind]["values"][label]
                match = item["match"]
                line_idx = int(item["line_idx"])
                old_line = lines[line_idx]
                ending = _line_ending(old_line)
                new_value = scaled[kind][label]
                new_line = (
                    f"{match.group('prefix')}"
                    f"{_render_won(new_value, match.group('unit'), match.group('value'))}"
                    f"{match.group('unit')}{match.group('trailing')}{ending}"
                )
                lines[line_idx] = new_line
                changes.append({
                    "label": label,
                    "line": line_idx + 1,
                    "factor": factor,
                    "old_value_won": item["won"],
                    "new_value_won": new_value,
                    "old_line": old_line.rstrip("\r\n"),
                    "new_line": new_line.rstrip("\r\n"),
                })
            result["corrections"].append({
                "year": year,
                "period": kind,
                "factor": factor,
                "factors": {label: factor for label in _LABELS},
                "consensus_won": consensus_info["amount_won"],
                "consensus_date": consensus_info.get("date"),
                "consensus_source": "half_year_sum_invariant",
                "residual_ratio": residual,
                "reason": "상반기+하반기=연간 합산 불변식으로 블록의 자릿수 오류를 교정했습니다.",
                "changes": changes,
            })
            corrected_years.add(year)
    return corrected_years


def correct_operating_profit_forecast_scale(
    markdown_text: str, *, consensus_won: float | None = None,
) -> dict[str, Any]:
    """Return a format-preserving, consensus-grounded scale correction."""
    blocks = extract_consensus_blocks(markdown_text)
    default_consensus = consensus_won if consensus_won is not None else latest_consensus_won(markdown_text)
    result: dict[str, Any] = {
        "original_text": markdown_text,
        "corrected_text": markdown_text,
        "consensus_won": default_consensus,
        "consensus_source": "request" if consensus_won is not None else "document",
        "consensus_extraction": {"blocks": blocks, "blocks_with_values": sum(bool(b["latest"]) for b in blocks)},
        "corrections": [],
        "review_items": [
            dict(issue, heading_line=block["heading_line"])
            for block in blocks for issue in (block.get("issues") or [])
        ],
        "stats": {"forecast_blocks_checked": 0, "corrections_applied": 0},
        "needs_manual_review": default_consensus is None,
    }
    lines = markdown_text.splitlines(keepends=True)
    header_years: set[str] = set()
    checked_years: set[str] = set()

    for idx, line in enumerate(list(lines)):
        header = _leading_forecast_match(line)
        if not header:
            continue
        year = header.group("year")
        header_years.add(year)

        # A high-level heading can be followed by a more concrete annual
        # header before any scenario row.  Process only that concrete block so
        # missing-consensus/review entries are not duplicated.
        if _ANNUAL_VALUE_RE.match(line.rstrip("\r\n")) is None:
            nested_header = False
            for look_idx in range(idx + 1, min(idx + 10, len(lines))):
                look = lines[look_idx].rstrip("\r\n")
                if _SCENARIO_LINE_RE.match(look):
                    break
                if _leading_forecast_match(look):
                    nested_header = True
                    break
            if nested_header:
                continue
        consensus_info = _consensus_for_line(blocks, idx, year, consensus_won)
        if consensus_info is None:
            result["review_items"].append({"year": year, "line": idx + 1, "reason": "해당 전망 앞에서 유효한 컨센서스를 찾지 못했습니다."})
            continue

        # A section title is navigation, not the annual value block. Reports
        # usually repeat a concrete "YYYY년 영업이익 전망:" header below it.
        if "시나리오 분석" in line and _ANNUAL_VALUE_RE.match(line.rstrip("\r\n")) is None:
            continue

        parsed: list[tuple[int, re.Match[str]]] = []
        for line_idx in range(idx + 1, min(idx + 16, len(lines))):
            content = lines[line_idx].rstrip("\r\n")
            match = _SCENARIO_LINE_RE.match(content)
            if match:
                parsed.append((line_idx, match))
                if len({item.group("label") for _, item in parsed}) == 3:
                    break
                continue
            if parsed and content.strip() and not re.match(r"^\s*[-+*]", content):
                break
        values = {match.group("label"): match for _, match in parsed}

        # A section heading may precede H1/H2 blocks. Only the concrete annual
        # block containing all three labels is eligible for scenario scaling.
        if set(values) == set(_LABELS):
            result["stats"]["forecast_blocks_checked"] += 1
            checked_years.add(year)
            won = {
                label: _to_won(_number(match.group("value")), match.group("unit"))
                for label, match in values.items()
            }
            if not (won["상방"] >= won["중간"] >= won["하방"]):
                result["review_items"].append({"year": year, "line": idx + 1, "reason": "상방 ≥ 중간 ≥ 하방 순서가 깨져 자동 배율 교정을 보류했습니다."})
                continue
            consensus_value = float(consensus_info["amount_won"])
            cell_factors: dict[str, float] = {}
            ambiguous: dict[str, float] = {}
            for label, value in won.items():
                raw_ratio = abs(value) / abs(consensus_value)
                decision = _scale_decision(value, consensus_value)
                if decision is not None:
                    cell_factors[label] = decision[0]
                elif (value < 0) == (consensus_value < 0) and 0.35 <= raw_ratio <= 2.8:
                    cell_factors[label] = 1.0
                else:
                    ambiguous[label] = raw_ratio
            if ambiguous:
                result["review_items"].append({
                    "year": year, "line": idx + 1,
                    "reason": "일부 시나리오 셀의 배율을 컨센서스에서 단일하게 확정할 수 없어 자동 교정을 보류했습니다.",
                    "unresolved_consensus_ratios": ambiguous,
                })
                continue
            changed_labels = [label for label, factor in cell_factors.items() if factor != 1.0]
            if not changed_labels:
                continue
            proposed = {label: won[label] * cell_factors[label] for label in _LABELS}
            if not (proposed["상방"] >= proposed["중간"] >= proposed["하방"]):
                result["review_items"].append({
                    "year": year, "line": idx + 1,
                    "reason": "셀별 배율 교정 후 상방 ≥ 중간 ≥ 하방 순서가 깨져 자동 교정을 보류했습니다.",
                    "factors": cell_factors,
                })
                continue
            changes: list[dict[str, Any]] = []
            for line_idx, match in parsed:
                label = match.group("label")
                factor = cell_factors[label]
                if factor == 1.0:
                    continue
                old_value = won[label]
                new_value = old_value * factor
                ending = _line_ending(lines[line_idx])
                old_line = lines[line_idx][:-len(ending)] if ending else lines[line_idx]
                new_line = (
                    f"{match.group('prefix')}{_render_won(new_value, match.group('unit'), match.group('value'))}"
                    f"{match.group('unit')}{match.group('trailing')}{ending}"
                )
                lines[line_idx] = new_line
                changes.append({
                    "label": label, "line": line_idx + 1,
                    "factor": factor,
                    "old_value_won": old_value, "new_value_won": new_value,
                    "old_line": old_line, "new_line": new_line.rstrip("\r\n"),
                })
            unique_factors = sorted({cell_factors[label] for label in changed_labels})
            middle_after = proposed["중간"]
            residual = abs(middle_after - consensus_value) / abs(consensus_value)
            result["corrections"].append({
                "year": year,
                "factor": unique_factors[0] if len(unique_factors) == 1 else None,
                "factors": {label: cell_factors[label] for label in changed_labels},
                "consensus_won": consensus_info["amount_won"],
                "consensus_date": consensus_info.get("date"),
                "consensus_source": consensus_info["source"],
                "middle_case_won": won["중간"], "residual_ratio": residual,
                "reason": f"컨센서스와 정합하는 10의 거듭제곱 배율을 {', '.join(changed_labels)} 셀에 개별 적용했습니다.",
                "changes": changes,
            })
            continue

        # Some reports provide one annual forecast rather than scenarios.
        annual = _ANNUAL_VALUE_RE.match(line.rstrip("\r\n"))
        if annual:
            result["stats"]["forecast_blocks_checked"] += 1
            checked_years.add(year)
            old_value = _to_won(_number(annual.group("value")), annual.group("unit"))
            consensus_value = float(consensus_info["amount_won"])
            decision = _scale_decision(old_value, consensus_value)
            if decision is None:
                same_sign = (old_value < 0) == (consensus_value < 0)
                ratio = (
                    max(abs(old_value), abs(consensus_value))
                    / max(1.0, min(abs(old_value), abs(consensus_value)))
                )
                if not same_sign or ratio > 2.8:
                    result["review_items"].append({
                        "year": year,
                        "line": idx + 1,
                        "reason": (
                            "연간 전망과 컨센서스의 부호가 다릅니다."
                            if not same_sign else
                            "연간 전망과 컨센서스의 격차가 크지만 자동 배율 교정 임계(5배)에 미달하여 보류했습니다."
                        ),
                        "consensus_ratio": ratio,
                    })
                continue
            factor, residual, ratio = decision
            new_value = old_value * factor
            ending = _line_ending(line)
            new_line = (
                f"{annual.group('prefix')}{_render_won(new_value, annual.group('unit'), annual.group('value'))}"
                f"{annual.group('unit')}{annual.group('trailing')}{ending}"
            )
            lines[idx] = new_line
            changes = [{
                "label": "연간", "line": idx + 1,
                "factor": factor,
                "old_value_won": old_value, "new_value_won": new_value,
                "old_line": line.rstrip("\r\n"), "new_line": new_line.rstrip("\r\n"),
            }]
            half_changes = _companion_half_year_changes(
                lines,
                year=year,
                annual_idx=idx,
                annual_won=old_value,
                factor=factor,
            )
            changes.extend(half_changes)
            reason = (
                f"연간 전망이 최신 컨센서스와 약 {ratio:.0f}배 차이여서 {factor:g}배 배율을 적용했습니다."
            )
            if half_changes:
                reason += " 같은 블록의 상반기·하반기 전망에도 동일 배율을 적용했습니다."
            result["corrections"].append({
                "year": year, "factor": factor,
                "consensus_won": consensus_info["amount_won"],
                "consensus_date": consensus_info.get("date"),
                "consensus_source": consensus_info["source"],
                "middle_case_won": old_value, "residual_ratio": residual,
                "reason": reason,
                "changes": changes,
            })

    # A second, consensus-independent check catches a shifted H1/H2/annual
    # block only when the accounting identity identifies one unique repair.
    invariant_years = _apply_half_year_invariant(
        lines,
        result,
        consensus_blocks=blocks,
        explicit_consensus=consensus_won,
    )
    checked_years.update(invariant_years)
    if invariant_years:
        result["review_items"] = [
            item for item in result["review_items"]
            if not (
                str(item.get("year") or "") in invariant_years
                and (
                    "컨센서스" in str(item.get("reason") or "")
                    or "지원하는 연간 값" in str(item.get("reason") or "")
                )
            )
        ]

    for year in sorted(header_years - checked_years):
        result["review_items"].append({
            "year": year,
            "reason": "영업이익 전망 heading은 찾았지만 지원하는 연간 값 또는 상/중/하 블록을 추출하지 못했습니다.",
        })

    result["corrected_text"] = "".join(lines)
    result["stats"]["corrections_applied"] = len(result["corrections"])
    result["stats"]["numeric_cells_changed"] = sum(
        len(correction.get("changes") or []) for correction in result["corrections"]
    )
    result["needs_manual_review"] = bool(result["review_items"] or (not result["corrections"] and default_consensus is None))
    if not result["corrections"]:
        result["reason"] = "컨센서스와 정합하는 단일 자릿수 배율 오류를 찾지 못했습니다."
    return result
