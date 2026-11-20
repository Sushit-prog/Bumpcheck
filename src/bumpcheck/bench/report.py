"""Markdown reports for benchmark evaluation results."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from bumpcheck.bench.labels import load_candidates, load_labels
from bumpcheck.verdict import Verdict

SYSTEMS = ("bumpcheck", "always_safe", "semver_rule")
LABELS = ("BROKE", "OK")


def wilson(k: int, n: int, *, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def format_rate(k: int, n: int) -> str:
    interval = wilson(k, n)
    if interval is None:
        return "0/0 (n/a)"
    low, high = interval
    return f"{k}/{n} = {100 * k / n:.1f}% [{100 * low:.1f}, {100 * high:.1f}]"


def _verdict(record: dict[str, Any], system: str) -> Verdict:
    verdicts = record.get("verdicts")
    if not isinstance(verdicts, dict):
        return "UNKNOWN"
    value = verdicts.get(system)
    return value if value in {"SAFE", "REVIEW", "LIKELY_BREAKING", "UNKNOWN"} else "UNKNOWN"


def _is_break_detected(record: dict[str, Any], system: str) -> bool:
    return _verdict(record, system) in {"REVIEW", "LIKELY_BREAKING"}


def _rate_row(name: str, records: list[dict[str, Any]], predicate) -> str:
    k = sum(1 for record in records if predicate(record))
    return f"| {name} | {format_rate(k, len(records))} |"


def _system_metrics(records: list[dict[str, Any]], system: str) -> list[tuple[str, str]]:
    scored = [record for record in records if record.get("label") in {"BROKE", "OK"}]
    broke = [record for record in scored if record.get("label") == "BROKE"]
    safe_all = [record for record in scored if _verdict(record, system) == "SAFE"]
    safe_ok = [record for record in safe_all if record.get("label") == "OK"]
    return [
        (
            "false-safe rate",
            format_rate(
                sum(1 for record in broke if _verdict(record, system) == "SAFE"), len(broke)
            ),
        ),
        ("SAFE precision", format_rate(len(safe_ok), len(safe_all))),
        (
            "breakage recall",
            format_rate(
                sum(1 for record in broke if _is_break_detected(record, system)), len(broke)
            ),
        ),
        (
            "LIKELY_BREAKING-only recall",
            format_rate(
                sum(1 for record in broke if _verdict(record, system) == "LIKELY_BREAKING"),
                len(broke),
            ),
        ),
        (
            "UNKNOWN rate",
            format_rate(
                sum(1 for record in scored if _verdict(record, system) == "UNKNOWN"),
                len(scored),
            ),
        ),
    ]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _fingerprints(records: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for record in records:
        fingerprint = record.get("fingerprint")
        if isinstance(fingerprint, dict) and isinstance(fingerprint.get("id"), str):
            if fingerprint["id"] not in ids:
                ids.append(fingerprint["id"])
    return ids


def _fired_rules(record: dict[str, Any], system: str) -> set[str]:
    fired = record.get("fired_rule_ids")
    if not isinstance(fired, dict):
        return set()
    values = fired.get(system)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def _rule_counts(records: list[dict[str, Any]]) -> list[list[str]]:
    scored = [record for record in records if record.get("label") in {"BROKE", "OK"}]
    rules: set[str] = set()
    for record in scored:
        rules.update(_fired_rules(record, "bumpcheck"))
    rows: list[list[str]] = []
    for rule in sorted(rules):
        fires = [record for record in scored if rule in _fired_rules(record, "bumpcheck")]
        tp = sum(1 for record in fires if record.get("label") == "BROKE")
        fp = sum(1 for record in fires if record.get("label") == "OK")
        rows.append([rule, str(len(fires)), str(tp), str(fp)])
    return rows


def _grouped_rates(records: list[dict[str, Any]], key: str) -> list[list[str]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("label") not in {"BROKE", "OK"}:
            continue
        value = str(record.get(key) or "none")
        groups.setdefault(value, []).append(record)
    rows: list[list[str]] = []
    for value in sorted(groups):
        group = groups[value]
        broke = [record for record in group if record.get("label") == "BROKE"]
        cells = [value, str(len(broke)), str(len(group) - len(broke))]
        for system in SYSTEMS:
            cells.append(
                format_rate(
                    sum(1 for record in broke if _verdict(record, system) == "SAFE"), len(broke)
                )
            )
        rows.append(cells)
    return rows


def _load_excluded(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def render_report(
    directory: Path,
    records: list[dict[str, Any]],
    *,
    split: str,
    candidates_path: Path | None = None,
    labels_path: Path | None = None,
    excluded_path: Path | None = None,
) -> str:
    base = Path(directory)
    candidates = load_candidates(candidates_path or base / "candidates.jsonl")
    labels = load_labels(labels_path or base / "labels.jsonl")
    excluded = _load_excluded(excluded_path or base / "excluded.jsonl")
    selected = [record for record in records if split == "all" or record.get("split") == split]
    broke = [record for record in selected if record.get("label") == "BROKE"]
    disputed = [record for record in selected if record.get("label") == "DISPUTED"]
    fingerprints = _fingerprints(selected)

    lines = [f"# Benchmark report (split: {split})", ""]
    if len(broke) < 30:
        lines.extend(["> n is small; do not quote these numbers publicly", ""])
    if len(fingerprints) > 1:
        lines.extend([f"> Warning: mixed fingerprints: {', '.join(fingerprints)}", ""])
    lines.extend(
        [
            "Definitions: false-safe rate = BROKE cases with verdict SAFE / all BROKE cases; "
            "SAFE precision = OK cases among SAFE verdicts; breakage recall = BROKE cases with "
            "REVIEW or LIKELY_BREAKING / all BROKE cases; LIKELY_BREAKING-only recall uses only "
            "LIKELY_BREAKING; UNKNOWN rate = UNKNOWN verdicts / all BROKE and OK cases. "
            "Every rate is shown as raw k/n with a 95% Wilson interval.",
            "",
            "## Known gap: Python-version-support breaks",
            "",
        ]
    )
    version_records = [
        record for record in broke if record.get("break_category") == "python_version_support"
    ]
    if version_records:
        for record in version_records:
            verdict = _verdict(record, "bumpcheck")
            rules = ", ".join(sorted(_fired_rules(record, "bumpcheck"))) or "none"
            lines.append(f"- {record.get('case_id')}: BumpCheck said {verdict} (rules: {rules}).")
    else:
        lines.append("- No labeled BROKE python_version_support cases in this split.")
    lines.extend(
        [
            "- Worked example: `Apocrathia/NSL-Downloader#49`, expected BumpCheck result SAFE "
            "or UNKNOWN because Python-version-support breaks are not a rule.",
            "",
        ]
    )

    metric_names = [name for name, _ in _system_metrics(selected, SYSTEMS[0])]
    rows: list[list[str]] = []
    for index, name in enumerate(metric_names):
        rows.append([name, *[_system_metrics(selected, system)[index][1] for system in SYSTEMS]])
    lines.extend(["## Headline metrics", "", _table(["metric", *SYSTEMS], rows), ""])

    lines.extend(
        [
            "## UNKNOWN rate by label",
            "",
            _table(
                ["label", *SYSTEMS],
                [
                    [
                        label,
                        *[
                            _system_metrics(
                                [record for record in selected if record.get("label") == label],
                                system,
                            )[4][1]
                            for system in SYSTEMS
                        ],
                    ]
                    for label in LABELS
                ],
            ),
            "",
        ]
    )

    error_counts = Counter(str(record.get("error_kind") or "none") for record in selected)
    lines.extend(
        [
            "## Errors and disputed labels",
            "",
            "Reported separately (never counted as SAFE): "
            + ", ".join(f"{kind}={count}" for kind, count in sorted(error_counts.items())),
            "",
        ]
    )
    if disputed:
        lines.extend(
            [
                "Disputed cases excluded from metrics: "
                + ", ".join(str(record.get("case_id")) for record in disputed),
                "",
            ]
        )
    else:
        lines.extend(["Disputed cases excluded from metrics: none", ""])

    rule_rows = _rule_counts(selected)
    lines.extend(
        [
            "## Rules",
            "",
            _table(["rule", "fires", "TP (BROKE)", "FP (OK)"], rule_rows)
            if rule_rows
            else "_No rules fired._",
            "",
        ]
    )

    category_rows = _grouped_rates(selected, "break_category")
    lines.extend(
        [
            "## Break categories",
            "",
            _table(
                [
                    "category",
                    "BROKE",
                    "OK",
                    "bumpcheck false-safe",
                    "always_safe false-safe",
                    "semver_rule false-safe",
                ],
                category_rows,
            )
            if category_rows
            else "_No labeled cases._",
            "",
        ]
    )

    for heading, key in (("Sampling source", "sampled_by"), ("Split", "split")):
        rows = _grouped_rates(selected, key)
        lines.extend(
            [
                f"## Slices by {heading}",
                "",
                _table(
                    [
                        key,
                        "BROKE",
                        "OK",
                        "bumpcheck false-safe",
                        "always_safe false-safe",
                        "semver_rule false-safe",
                    ],
                    rows,
                )
                if rows
                else "_No labeled cases._",
                "",
            ]
        )

    exclusion_counts = Counter(str(item.get("reason") or "other") for item in excluded)
    sampled_counts = Counter(
        str(candidate.get("sampled_by") or "any")
        for candidate in candidates.values()
        if split == "all" or candidate.get("split") == split
    )
    round2 = any(round_number >= 2 for _, round_number in labels)
    lines.extend(
        [
            "## Selection bias",
            "",
            "Exclusion reason counts: "
            + (
                ", ".join(f"{key}={value}" for key, value in sorted(exclusion_counts.items()))
                or "none"
            ),
            "",
            "Sampled-by mix: "
            + (
                ", ".join(f"{key}={value}" for key, value in sorted(sampled_counts.items()))
                or "none"
            ),
            "",
            "Unbiased sampling had ~2% BROKE (roughly 1 in 50 candidates), so this enriched "
            "benchmark should not be read as a population rate. `parse_mismatch` and "
            "`not_single_package` exclusions remain undiagnosed. Labels are single-annotator "
            "round 1 unless round 2 exists" + (" (round 2 labels are present)." if round2 else "."),
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    directory: Path,
    records: list[dict[str, Any]],
    *,
    split: str,
) -> str:
    text = render_report(directory, records, split=split)
    path = Path(directory) / "eval" / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return text
