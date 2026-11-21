"""Hand-labelling store and blind review loop for mined benchmark candidates."""

from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from bumpcheck.bench.titles import is_bot_commit

LabelValue = Literal["BROKE", "OK", "EXCLUDE"]
BreakCategory = Literal[
    "api_break", "python_version_support", "dependency_conflict", "behavioral", "other"
]
EvidenceKind = Literal[
    "log_names_package",
    "install_error",
    "maintainer_fix",
    "reproduced_locally",
    "pypi_metadata",
    "other",
]
ExcludeReason = Literal[
    "flaky_or_infra", "preexisting_failure", "not_attributable", "superseded", "other"
]

BREAK_CATEGORIES: tuple[str, ...] = (
    "api_break",
    "python_version_support",
    "dependency_conflict",
    "behavioral",
    "other",
)
EVIDENCE_KINDS: tuple[str, ...] = (
    "log_names_package",
    "install_error",
    "maintainer_fix",
    "reproduced_locally",
    "pypi_metadata",
    "other",
)
EXCLUDE_REASONS: tuple[str, ...] = (
    "flaky_or_infra",
    "preexisting_failure",
    "not_attributable",
    "superseded",
    "other",
)
LABEL_VALUES: tuple[str, ...] = ("BROKE", "OK", "EXCLUDE")
FAILING_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out", "action_required"})
LOG_EXPIRY_NOTE = "logs likely expired (GitHub keeps Actions logs at most 90 days)"


class Label(BaseModel):
    """One human judgement of one mined candidate in one review round."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    round: int = 1
    label: LabelValue
    break_category: BreakCategory | None = None
    evidence_kind: EvidenceKind | None = None
    evidence: str = ""
    exclude_reason: ExcludeReason | None = None
    labeled_on: date
    notes: str = ""

    @field_validator("round")
    @classmethod
    def _positive_round(cls, value: int) -> int:
        if value < 1:
            raise ValueError("round must be one or greater")
        return value

    @model_validator(mode="after")
    def _enforce_rules(self) -> Label:
        if self.label == "BROKE":
            if self.break_category is None:
                raise ValueError("BROKE requires break_category")
            if self.evidence_kind is None:
                raise ValueError("BROKE requires evidence_kind")
            if not self.evidence.strip():
                raise ValueError("BROKE requires non-empty evidence")
            if self.exclude_reason is not None:
                raise ValueError("BROKE does not allow exclude_reason")
        elif self.label == "EXCLUDE":
            if self.exclude_reason is None:
                raise ValueError("EXCLUDE requires exclude_reason")
            if not self.evidence.strip():
                raise ValueError("EXCLUDE requires non-empty evidence")
            if self.break_category is not None:
                raise ValueError("EXCLUDE does not allow break_category")
            if self.evidence_kind is not None:
                raise ValueError("EXCLUDE does not allow evidence_kind")
        else:
            if self.break_category is not None:
                raise ValueError("OK does not allow break_category")
            if self.evidence_kind is not None:
                raise ValueError("OK does not allow evidence_kind")
            if self.exclude_reason is not None:
                raise ValueError("OK does not allow exclude_reason")
        return self


def _format_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            message = str(errors[0].get("msg", exc))
            prefix = "Value error, "
            return message[len(prefix) :] if message.startswith(prefix) else message
    return str(exc)


def load_labels(path: Path) -> dict[tuple[str, int], Label]:
    """Load labels keyed by ``(case_id, round)``; name the bad line on errors."""
    if not path.exists():
        return {}
    records: dict[tuple[str, int], Label] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            label = Label.model_validate(json.loads(line))
        except (ValueError, ValidationError) as exc:
            raise ValueError(
                f"{path.name} line {line_number}: {_format_validation_error(exc)}"
            ) from exc
        records[(label.case_id, label.round)] = label
    return records


def write_labels(path: Path, labels: Iterable[Label]) -> None:
    """Atomically rewrite ``labels.jsonl`` sorted by case and round."""
    ordered = sorted(labels, key=lambda label: (label.case_id, label.round))
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            for label in ordered:
                payload = label.model_dump(mode="json")
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def save_label(
    path: Path, label: Label, records: dict[tuple[str, int], Label] | None = None
) -> tuple[dict[tuple[str, int], Label], Label | None]:
    """Upsert one label and return the store plus the record it replaced."""
    current = dict(records) if records is not None else load_labels(path)
    key = (label.case_id, label.round)
    previous = current.get(key)
    current[key] = label
    write_labels(path, current.values())
    return current, previous


def undo_label(
    path: Path,
    records: dict[tuple[str, int], Label],
    key: tuple[str, int],
    previous: Label | None,
) -> dict[tuple[str, int], Label]:
    """Undo a save, restoring the replaced record when there was one."""
    if previous is None:
        records.pop(key, None)
    else:
        records[key] = previous
    write_labels(path, records.values())
    return records


def load_candidates(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"candidates file not found: {path}")
    candidates: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("case_id"), str):
            candidates[record["case_id"]] = record
    return candidates


def collect_also_labeled(dirs: list[Path] | None, round: int) -> set[str]:
    found: set[str] = set()
    for directory in dirs or []:
        if not directory.is_dir():
            raise ValueError(f"--also-dir directory not found: {directory}")
        path = directory / "labels.jsonl"
        if not path.is_file():
            raise ValueError(f"--also-dir labels file not found: {path}")
        for case_id, label_round in load_labels(path):
            if label_round == round:
                found.add(case_id)
    return found


def select_cases(
    candidates: dict[str, Any],
    labels: dict[tuple[str, int], Label],
    *,
    round: int = 1,
    only: str | None = None,
    sample: float | None = None,
    seed: int = 0,
    also_labeled: set[str] | None = None,
    round1_label: str | None = None,
    requested_ids: list[str] | None = None,
) -> list[str]:
    """Choose the unlabeled case_ids for a round, deterministically sorted."""
    if requested_ids is not None:
        unknown = [case_id for case_id in requested_ids if case_id not in candidates]
        if unknown:
            raise ValueError("candidate not found: " + ", ".join(unknown))
    if round < 1:
        raise ValueError("round must be one or greater")
    if round1_label is not None and round < 2:
        raise ValueError("--round1-label requires --round 2 or greater")
    if round1_label is not None and round1_label not in {"BROKE", "OK"}:
        raise ValueError("--round1-label must be BROKE or OK")
    if sample is not None and not 0 < sample <= 1:
        raise ValueError("--sample must be greater than 0 and at most 1")

    labeled_this_round = {case_id for case_id, label_round in labels if label_round == round}
    prior = {
        case_id: label.label for (case_id, label_round), label in labels.items() if label_round == 1
    }
    case_ids = set(candidates) - labeled_this_round
    if round >= 2:
        case_ids &= {case_id for case_id, value in prior.items() if value in {"BROKE", "OK"}}
        if round1_label is not None:
            case_ids &= {case_id for case_id, value in prior.items() if value == round1_label}
    if also_labeled:
        case_ids -= also_labeled
    if only is not None:
        case_ids = {
            case_id for case_id in case_ids if candidates[case_id].get("label_suggestion") == only
        }
    if requested_ids is not None:
        case_ids &= set(requested_ids)
    ordered = sorted(case_ids)
    if sample is not None and requested_ids is None and ordered:
        count = max(1, int(sample * len(ordered) + 0.5))
        chosen = random.Random(seed).sample(ordered, min(count, len(ordered)))
        ordered = sorted(chosen)
    return ordered


def _default_output(message: str) -> None:
    """Print without crashing on characters the terminal encoding cannot encode."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass
    try:
        stream.write(message + "\n")
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        buffer = getattr(stream, "buffer", None)
        encoded = (message + "\n").encode(encoding, errors="replace")
        if buffer is not None:
            buffer.write(encoded)
            buffer.flush()
        else:
            stream.write(encoded.decode(encoding, errors="replace"))


def _logs_likely_expired(merged_at: object, today: date) -> bool:
    if not isinstance(merged_at, str) or not merged_at.strip():
        return False
    try:
        merged_date = date.fromisoformat(merged_at.strip()[:10])
    except ValueError:
        return False
    return today - merged_date > timedelta(days=90)


def format_case(
    candidate: dict[str, Any],
    *,
    show_suggestion: bool = False,
    today: Callable[[], date] = date.today,
) -> str:
    """Render one candidate; never include stored labels from any round."""
    number = candidate.get("pr_number", "?")
    lines = [
        f"case: {candidate.get('case_id', '')}",
        f"url: https://github.com/{candidate.get('repo', '')}/pull/{number}",
        f"bot: {candidate.get('bot', '')}",
        f"title: {candidate.get('title', '')}",
        (
            f"package: {candidate.get('package', '')} "
            f"{candidate.get('old_version') or '?'}->{candidate.get('new_version') or '?'}"
        ),
        f"merged: {candidate.get('merged')} at {candidate.get('merged_at')}",
        f"autoclosed: {bool(candidate.get('autoclosed', False))}",
        f"rerun_flaky: {bool(candidate.get('rerun_flaky', False))}",
    ]
    if show_suggestion:
        lines.append(f"label_suggestion: {candidate.get('label_suggestion', '')}")
    lines.append("ci_checks:")
    for check in candidate.get("ci_checks") or []:
        conclusion = str(check.get("conclusion") or "")
        lines.append(f"  {check.get('name')}: {conclusion}")
        if conclusion.lower() in FAILING_CONCLUSIONS:
            url = check.get("html_url")
            if isinstance(url, str) and url:
                lines.append(f"    html_url: {url}")
    lines.append("failure_vs_base:")
    for entry in candidate.get("failure_vs_base") or []:
        lines.append(f"  {entry.get('name')}: {entry.get('status')}")
    lines.append("failure_excerpts:")
    expired = _logs_likely_expired(candidate.get("merged_at"), today())
    for excerpt in candidate.get("failure_excerpts") or []:
        lines.append(f"  {excerpt.get('check_name')} ({excerpt.get('log_status')}):")
        if excerpt.get("log_status") == "unavailable" and expired:
            lines.append(f"    {LOG_EXPIRY_NOTE}")
        for line in excerpt.get("lines") or []:
            lines.append(f"    {line}")
    lines.append("later_fix_commits:")
    commits = candidate.get("later_fix_commits") or []
    visible = [fix for fix in commits if not is_bot_commit(str(fix.get("message") or ""))]
    for fix in visible:
        lines.append(f"  {fix.get('sha')} {fix.get('date')} {fix.get('message')}")
    hidden = len(commits) - len(visible)
    if hidden:
        lines.append(f"{hidden} later commit(s) look like bot updates (hidden)")
    base_checks = candidate.get("base_ci_checks")
    if isinstance(base_checks, list):
        lines.append(f"base_ci: available ({len(base_checks)} check(s))")
    else:
        lines.append("base_ci: unavailable")
    return "\n".join(lines)


def _ask_choice(
    prompt: str,
    options: tuple[str, ...],
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> str:
    while True:
        answer = input_fn(f"{prompt} [{'|'.join(options)}]: ").strip()
        if answer in options:
            return answer
        output(f"invalid {prompt}; choose one of: {', '.join(options)}")


def _ask_multiline(
    prompt: str, input_fn: Callable[[str], str], output: Callable[[str], None]
) -> str:
    output(f"{prompt} (empty line to finish):")
    lines: list[str] = []
    while True:
        line = input_fn("> ")
        if not line:
            return "\n".join(lines)
        lines.append(line)


def _build_label(
    case_id: str,
    choice: str,
    round: int,
    candidate: dict[str, Any],
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    today: Callable[[], date],
) -> Label | None:
    if choice == "b":
        category = _ask_choice("break_category", BREAK_CATEGORIES, input_fn, output)
        kind = _ask_choice("evidence_kind", EVIDENCE_KINDS, input_fn, output)
        evidence = _ask_multiline("evidence", input_fn, output)
        notes = input_fn("notes (optional): ").strip()
        return Label(
            case_id=case_id,
            round=round,
            label="BROKE",
            break_category=category,  # type: ignore[arg-type]
            evidence_kind=kind,  # type: ignore[arg-type]
            evidence=evidence,
            labeled_on=today(),
            notes=notes,
        )
    if choice == "o":
        if input_fn("confirm OK? [y/N]: ").strip().lower() != "y":
            output("OK label cancelled")
            return None
        if candidate.get("merged") is not True or candidate.get("ci_summary") != "all_passed":
            output("OK requires a merged PR with ci_summary all_passed; use [x] exclude instead.")
            return None
        return Label(case_id=case_id, round=round, label="OK", labeled_on=today())
    reason = _ask_choice("exclude_reason", EXCLUDE_REASONS, input_fn, output)
    evidence = _ask_multiline("evidence", input_fn, output)
    return Label(
        case_id=case_id,
        round=round,
        label="EXCLUDE",
        exclude_reason=reason,  # type: ignore[arg-type]
        evidence=evidence,
        labeled_on=today(),
    )


def run_label_loop(
    directory: Path,
    *,
    round: int = 1,
    only: str | None = None,
    sample: float | None = None,
    seed: int = 0,
    show_suggestion: bool = False,
    also_dirs: list[Path] | None = None,
    round1_label: str | None = None,
    requested_ids: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = _default_output,
    today: Callable[[], date] = date.today,
) -> int:
    """Run the interactive labelling loop; all I/O is injectable."""
    candidates = load_candidates(directory / "candidates.jsonl")
    labels_path = directory / "labels.jsonl"
    labels = load_labels(labels_path)
    also_labeled = collect_also_labeled(also_dirs, round)
    case_ids = select_cases(
        candidates,
        labels,
        round=round,
        only=only,
        sample=sample,
        seed=seed,
        also_labeled=also_labeled,
        round1_label=round1_label,
        requested_ids=requested_ids,
    )
    output(f"labeling round {round}: {len(case_ids)} case(s)")
    if requested_ids is not None:
        dropped: list[str] = []
        for case_id in dict.fromkeys(requested_ids):
            if case_id in case_ids:
                continue
            reason = "not selected for this round"
            if (case_id, round) in labels:
                reason = "already labeled this round"
            elif only is not None and candidates[case_id].get("label_suggestion") != only:
                reason = f"dropped by --only {only}"
            elif also_labeled and case_id in also_labeled:
                reason = "dropped by --also-dir"
            dropped.append(f"{case_id} ({reason})")
        output(f"requested cases dropped: {', '.join(dropped) if dropped else 'none'}")
    saved: list[tuple[tuple[str, int], Label | None]] = []
    skipped_ids: list[str] = []
    labeled = skipped = undone = 0

    try:
        for case_id in case_ids:
            candidate = candidates[case_id]
            output("")
            output(format_case(candidate, show_suggestion=show_suggestion, today=today))
            try:
                while True:
                    try:
                        choice = input_fn("[b]roke [o]k [x] exclude [s]kip [u]ndo-last [q]uit: ")
                    except EOFError:
                        choice = "q"
                    choice = choice.strip().lower()
                    if choice in {"b", "o", "x", "s", "u", "q"}:
                        break
                    output("invalid choice; use b, o, x, s, u or q")

                if choice == "q":
                    break
                if choice == "s":
                    skipped += 1
                    skipped_ids.append(case_id)
                    continue
                if choice == "u":
                    if not saved:
                        output("nothing to undo in this session")
                        continue
                    key, previous = saved.pop()
                    labels = undo_label(labels_path, labels, key, previous)
                    undone += 1
                    output(f"undid label for {key[0]}")
                    continue

                label = _build_label(
                    case_id,
                    choice,
                    round,
                    candidate,
                    input_fn=input_fn,
                    output=output,
                    today=today,
                )
            except EOFError:
                output("end of input")
                break
            except ValidationError as exc:
                output(f"invalid label: {_format_validation_error(exc)}")
                continue
            if label is None:
                continue
            key = (label.case_id, label.round)
            labels, previous = save_label(labels_path, label, labels)
            saved.append((key, previous))
            labeled += 1
            output(f"saved {label.label} for {case_id}")
    except KeyboardInterrupt:
        output("interrupted")
    skipped_text = ", ".join(skipped_ids) if skipped_ids else "none"
    output(
        f"session: {labeled} label(s) saved, {skipped} skipped, {undone} undone; "
        f"skipped case_ids: {skipped_text}"
    )
    return 0


def compute_stats(
    candidates: dict[str, Any], labels: dict[tuple[str, int], Label]
) -> dict[str, Any]:
    rounds: dict[int, Counter[str]] = {1: Counter()}
    for _case_id, label_round in labels:
        rounds.setdefault(label_round, Counter())
    for label in labels.values():
        rounds[label.round][label.label] += 1

    breaks: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for label in labels.values():
        if label.break_category is not None:
            breaks[label.break_category] += 1
        if label.evidence_kind is not None:
            kinds[label.evidence_kind] += 1
        if label.exclude_reason is not None:
            reasons[label.exclude_reason] += 1

    labeled_round_1 = {case_id for case_id, label_round in labels if label_round == 1}
    return {
        "rounds": {
            number: {value: counts[value] for value in LABEL_VALUES}
            for number, counts in sorted(rounds.items())
        },
        "break_categories": {value: breaks[value] for value in BREAK_CATEGORIES},
        "evidence_kinds": {value: kinds[value] for value in EVIDENCE_KINDS},
        "exclude_reasons": {value: reasons[value] for value in EXCLUDE_REASONS},
        "unlabeled_round_1": len(set(candidates) - labeled_round_1),
    }


def compute_agreement(labels: dict[tuple[str, int], Label]) -> dict[str, Any]:
    """Cohen's kappa over BROKE/OK pairs labeled in both rounds."""
    case_ids = sorted(
        case_id for case_id, label_round in labels if label_round == 1 and (case_id, 2) in labels
    )
    matrix = {(first, second): 0 for first in ("BROKE", "OK") for second in ("BROKE", "OK")}
    disagreeing: list[str] = []
    exclude_disagreements: list[str] = []
    for case_id in case_ids:
        first = labels[(case_id, 1)].label
        second = labels[(case_id, 2)].label
        if first == "EXCLUDE" or second == "EXCLUDE":
            if first != second:
                exclude_disagreements.append(case_id)
            continue
        if first in ("BROKE", "OK") and second in ("BROKE", "OK"):
            matrix[(first, second)] += 1  # type: ignore[index]
            if first != second:
                disagreeing.append(case_id)

    n = sum(matrix.values())
    raw = (
        sum(count for (first, second), count in matrix.items() if first == second) / n
        if n
        else None
    )
    kappa: float | None = None
    if n:
        first_counts = Counter(
            {
                first: sum(v for (r1, _), v in matrix.items() if r1 == first)
                for first in ("BROKE", "OK")
            }
        )
        second_counts = Counter(
            {
                second: sum(v for (_, r2), v in matrix.items() if r2 == second)
                for second in ("BROKE", "OK")
            }
        )
        expected = sum(
            (first_counts[value] / n) * (second_counts[value] / n) for value in ("BROKE", "OK")
        )
        if 1 - expected == 0:
            kappa = 1.0 if raw == 1 else 0.0
        else:
            kappa = (raw - expected) / (1 - expected)
    return {
        "n": n,
        "matrix": matrix,
        "raw_agreement": raw,
        "kappa": kappa,
        "disagreeing_case_ids": disagreeing,
        "exclude_disagreements": exclude_disagreements,
    }
