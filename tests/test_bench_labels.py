from __future__ import annotations

import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bumpcheck.bench.labels import (
    EVIDENCE_KINDS,
    LOG_EXPIRY_NOTE,
    Label,
    _default_output,
    compute_agreement,
    compute_stats,
    format_case,
    load_labels,
    run_label_loop,
    save_label,
    select_cases,
    undo_label,
    write_labels,
)
from bumpcheck.cli import app

FIXED = date(2026, 3, 15)


def _candidate(case_id: str = "acme/widgets#1", **overrides: object) -> dict:
    record: dict = {
        "case_id": case_id,
        "repo": "acme/widgets",
        "pr_number": 1,
        "bot": "renovate",
        "title": "Update dependency requests to v2.32.0 - autoclosed",
        "package": "requests",
        "old_version": "2.31.0",
        "new_version": "2.32.0",
        "merged": True,
        "merged_at": "2026-01-05T10:00:00Z",
        "autoclosed": False,
        "rerun_flaky": False,
        "label_suggestion": "OK_SUSPECT",
        "ci_summary": "all_passed",
        "ci_checks": [{"name": "tests", "conclusion": "success"}],
        "failure_vs_base": [],
        "failure_excerpts": [],
        "later_fix_commits": [],
        "base_ci_checks": [],
    }
    record.update(overrides)
    return record


def _label(case_id: str, round: int, value: str, **overrides: object) -> Label:
    payload: dict = {"case_id": case_id, "round": round, "label": value, "labeled_on": FIXED}
    if value == "BROKE":
        payload.update(
            break_category="api_break",
            evidence_kind="log_names_package",
            evidence="ImportError: requests.legacy",
        )
    elif value == "EXCLUDE":
        payload.update(exclude_reason="superseded", evidence="closed by later PR")
    payload.update(overrides)
    return Label.model_validate(payload)


def _setup(tmp_path: Path, candidates: list[dict], labels: list[Label] | None = None) -> Path:
    directory = tmp_path / "store"
    directory.mkdir()
    (directory / "candidates.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in candidates),
        encoding="utf-8",
    )
    if labels:
        write_labels(directory / "labels.jsonl", labels)
    return directory


class ScriptedInput:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def _collector() -> tuple[list[str], Callable[[str], None]]:
    lines: list[str] = []
    return lines, lines.append


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def _run_loop(
    directory: Path, answers: list[str], **kwargs: object
) -> tuple[list[str], ScriptedInput]:
    output, collect = _collector()
    script = ScriptedInput(answers)
    run_label_loop(
        directory,
        input_fn=script,
        output=collect,
        today=lambda: FIXED,
        **kwargs,  # type: ignore[arg-type]
    )
    return output, script


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"label": "BROKE"}, "BROKE requires break_category"),
        (
            {"label": "BROKE", "break_category": "api_break"},
            "BROKE requires evidence_kind",
        ),
        (
            {"label": "BROKE", "break_category": "api_break", "evidence_kind": "log_names_package"},
            "BROKE requires non-empty evidence",
        ),
        (
            {
                "label": "BROKE",
                "break_category": "api_break",
                "evidence_kind": "log_names_package",
                "evidence": "e",
                "exclude_reason": "superseded",
            },
            "BROKE does not allow exclude_reason",
        ),
        ({"label": "EXCLUDE"}, "EXCLUDE requires exclude_reason"),
        (
            {"label": "EXCLUDE", "exclude_reason": "superseded"},
            "EXCLUDE requires non-empty evidence",
        ),
        (
            {
                "label": "EXCLUDE",
                "exclude_reason": "superseded",
                "evidence": "e",
                "break_category": "api_break",
            },
            "EXCLUDE does not allow break_category",
        ),
        (
            {
                "label": "EXCLUDE",
                "exclude_reason": "superseded",
                "evidence": "e",
                "evidence_kind": "other",
            },
            "EXCLUDE does not allow evidence_kind",
        ),
        ({"label": "OK", "break_category": "api_break"}, "OK does not allow break_category"),
        ({"label": "OK", "evidence_kind": "other"}, "OK does not allow evidence_kind"),
        ({"label": "OK", "exclude_reason": "superseded"}, "OK does not allow exclude_reason"),
        ({"label": "OK", "round": 0}, "round must be one or greater"),
    ],
)
def test_model_validation_messages(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        Label.model_validate({"case_id": "a/b#1", "labeled_on": FIXED, **payload})


def test_label_serializes_with_stable_key_order() -> None:
    label = _label("a/b#1", 1, "BROKE")
    text = label.model_dump_json()
    assert text.startswith('{"case_id":"a/b#1","round":1,"label":"BROKE"')
    assert '"labeled_on":"2026-03-15"' in text


def test_broke_flow_writes_label_with_fixed_date(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    output, _ = _run_loop(
        directory,
        ["b", "api_break", "log_names_package", "ImportError: requests", "", "checked log", "q"],
    )
    labels = load_labels(directory / "labels.jsonl")
    label = labels[("acme/widgets#1", 1)]
    assert label.label == "BROKE"
    assert label.break_category == "api_break"
    assert label.evidence == "ImportError: requests"
    assert label.labeled_on == FIXED
    assert label.notes == "checked log"
    assert "saved BROKE for acme/widgets#1" in _text(output)
    assert list(directory.glob("*.tmp")) == []


def test_file_is_written_after_every_label(tmp_path: Path) -> None:
    directory = _setup(
        tmp_path,
        [_candidate("acme/widgets#1"), _candidate("acme/widgets#2", pr_number=2)],
    )
    snapshots: list[int] = []

    def input_fn(prompt: str) -> str:
        if prompt.startswith("[b]roke"):
            snapshots.append(len(load_labels(directory / "labels.jsonl")))
        return script(prompt)

    script = ScriptedInput(
        [
            "b",
            "api_break",
            "log_names_package",
            "first",
            "",
            "",
            "b",
            "behavioral",
            "other",
            "second",
            "",
            "",
            "q",
        ]
    )
    output, _collect = _collector()
    run_label_loop(directory, input_fn=input_fn, output=_collect, today=lambda: FIXED)
    assert snapshots == [0, 1]
    assert len(load_labels(directory / "labels.jsonl")) == 2


def test_relabel_replaces_existing_record(tmp_path: Path) -> None:
    previous = _label("acme/widgets#1", 1, "BROKE")
    directory = _setup(tmp_path, [_candidate()], [previous])
    replacement = _label("acme/widgets#1", 1, "OK")
    labels, replaced = save_label(directory / "labels.jsonl", replacement)
    assert replaced == previous
    assert len(labels) == 1
    assert load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)] == replacement


def test_undo_restores_replaced_record(tmp_path: Path) -> None:
    previous = _label("acme/widgets#1", 1, "BROKE")
    directory = _setup(tmp_path, [_candidate()], [previous])
    new_label = _label("acme/widgets#1", 1, "OK")
    labels, replaced = save_label(directory / "labels.jsonl", new_label)
    undo_label(directory / "labels.jsonl", labels, ("acme/widgets#1", 1), replaced)
    stored = load_labels(directory / "labels.jsonl")
    assert stored[("acme/widgets#1", 1)] == previous
    assert list(directory.glob("*.tmp")) == []


def test_undo_deletes_new_label_and_handles_empty_stack(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    output, _ = _run_loop(
        directory,
        [
            "u",
            "b",
            "api_break",
            "other",
            "evidence",
            "",
            "",
            "u",
            "q",
        ],
    )
    assert "nothing to undo in this session" in _text(output)
    assert load_labels(directory / "labels.jsonl") == {}


def test_skip_and_quit_write_nothing(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    output, _ = _run_loop(directory, ["s", "q"])
    text = _text(output)
    assert "session: 0 label(s) saved, 1 skipped" in text
    assert "skipped case_ids: acme/widgets#1" in text
    assert not (directory / "labels.jsonl").exists()


def test_requested_case_filter_shows_only_requested_cases(tmp_path: Path) -> None:
    candidates = [
        _candidate("acme/widgets#1", pr_number=1),
        _candidate("acme/widgets#2", pr_number=2),
        _candidate("acme/widgets#3", pr_number=3),
    ]
    directory = _setup(tmp_path, candidates)
    output, _ = _run_loop(
        directory,
        ["s", "s"],
        requested_ids=["acme/widgets#2", "acme/widgets#3"],
    )
    text = _text(output)
    assert "/pull/2" in text and "/pull/3" in text
    assert "/pull/1" not in text
    assert "requested cases dropped: none" in text


def test_requested_case_unknown_raises_clear_error(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    with pytest.raises(ValueError, match="candidate not found: acme/widgets#99"):
        _run_loop(directory, ["q"], requested_ids=["acme/widgets#99"])


def test_requested_cases_dropped_are_reported(tmp_path: Path) -> None:
    candidates = [
        _candidate("acme/widgets#1", pr_number=1, label_suggestion="OK_SUSPECT"),
        _candidate("acme/widgets#2", pr_number=2, label_suggestion="BROKE_SUSPECT"),
        _candidate("acme/widgets#3", pr_number=3, label_suggestion="OK_SUSPECT"),
    ]
    directory = _setup(tmp_path, candidates, [_label("acme/widgets#3", 1, "OK")])
    output, _ = _run_loop(
        directory,
        ["q"],
        only="OK_SUSPECT",
        requested_ids=["acme/widgets#1", "acme/widgets#2", "acme/widgets#3"],
    )
    text = _text(output)
    assert "acme/widgets#2 (dropped by --only OK_SUSPECT)" in text
    assert (
        "acme/widgets#3 (already labeled this round; use --relabel with --case to replace it)"
        in text
    )


def test_shorthand_resolves_and_only_dropped_id_is_reported(tmp_path: Path) -> None:
    candidates = [
        _candidate("acme/widgets#1", pr_number=1),
        _candidate("acme/widgets#2", pr_number=2),
        _candidate("acme/widgets#3", pr_number=3),
    ]
    directory = _setup(tmp_path, candidates, [_label("acme/widgets#3", 1, "OK")])
    output, _ = _run_loop(
        directory,
        ["b", "api_break", "log_names_package", "evidence", "", ""],
        requested_ids=["widgets#1", "acme/widgets#3"],
    )
    text = _text(output)
    assert (
        "requested cases dropped: acme/widgets#3 "
        "(already labeled this round; use --relabel with --case to replace it)"
    ) in text
    assert "widgets#1 (" not in text
    stored = load_labels(directory / "labels.jsonl")
    assert stored[("acme/widgets#1", 1)].label == "BROKE"


def test_undo_skipped_when_another_session_changed_label(tmp_path: Path) -> None:
    directory = _setup(
        tmp_path, [_candidate("acme/widgets#1"), _candidate("acme/widgets#2", pr_number=2)]
    )
    labels_path = directory / "labels.jsonl"
    script = ScriptedInput(["b", "api_break", "log_names_package", "evidence", "", "", "u", "q"])
    choice_prompts = 0

    def input_fn(prompt: str) -> str:
        nonlocal choice_prompts
        if prompt.startswith("[b]roke"):
            choice_prompts += 1
            if choice_prompts == 2:
                save_label(labels_path, _label("acme/widgets#1", 1, "OK"))
        return script(prompt)

    output, collect = _collector()
    run_label_loop(directory, input_fn=input_fn, output=collect, today=lambda: FIXED)
    text = _text(output)
    assert "undo skipped: acme/widgets#1 was changed by another session; store left as is" in text
    assert "undid label for" not in text
    assert load_labels(labels_path)[("acme/widgets#1", 1)].label == "OK"


def test_exclude_flow(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    _run_loop(
        directory,
        ["x", "preexisting_failure", "also failed on base", "", "q"],
    )
    label = load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)]
    assert label.label == "EXCLUDE"
    assert label.exclude_reason == "preexisting_failure"
    assert label.evidence == "also failed on base"


def test_round_two_pool_and_round1_label_flag(tmp_path: Path) -> None:
    candidates = [
        _candidate("acme/widgets#1", pr_number=1),
        _candidate("acme/widgets#2", pr_number=2),
        _candidate("acme/widgets#3", pr_number=3),
    ]
    labels = [
        _label("acme/widgets#1", 1, "BROKE"),
        _label("acme/widgets#2", 1, "OK"),
        _label("acme/widgets#3", 1, "EXCLUDE"),
    ]
    directory = _setup(tmp_path, candidates, labels)
    output, _ = _run_loop(directory, ["s", "s", "q"], round=2)
    assert "/pull/1" in _text(output) and "/pull/2" in _text(output)
    assert "/pull/3" not in _text(output)
    assert "BROKE" not in _text(output) and "OK label" not in _text(output)

    narrowed, _ = _run_loop(directory, ["s", "q"], round=2, round1_label="BROKE")
    assert "/pull/1" in _text(narrowed)
    assert "/pull/2" not in _text(narrowed)


def test_suggestion_hidden_by_default_and_shown_with_flag(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    hidden, _ = _run_loop(directory, ["q"])
    assert "OK_SUSPECT" not in _text(hidden)

    shown, _ = _run_loop(directory, ["q"], show_suggestion=True)
    assert "label_suggestion: OK_SUSPECT" in _text(shown)


def test_display_includes_non_cp1252_text(tmp_path: Path) -> None:
    directory = _setup(
        tmp_path,
        [
            _candidate(
                title="Update dependency requests 🐍 to v2.32.0",
                failure_excerpts=[
                    {
                        "check_name": "tests",
                        "log_status": "matched",
                        "lines": ["Ошибка: ImportError: requests.legacy"],
                    }
                ],
            )
        ],
    )
    output, _ = _run_loop(directory, ["q"])
    assert "🐍" in "\n".join(output)
    assert "Ошибка" in "\n".join(output)


def test_default_output_reconfigures_and_replaces_unencodable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconfigured: list[dict] = []

    class Stream:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        def reconfigure(self, **kwargs: object) -> None:
            reconfigured.append(kwargs)

        def write(self, text: str) -> None:
            text.encode(self.encoding)

        def flush(self) -> None:
            return None

    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)
    _default_output("emoji 🐍 Ошибка")
    assert reconfigured == [{"errors": "replace"}]
    assert b"?" in stream.buffer.getvalue()


def test_default_output_survives_unsupported_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stream:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        def reconfigure(self, **kwargs: object) -> None:
            raise OSError("not supported")

        def write(self, text: str) -> None:
            text.encode(self.encoding)

        def flush(self) -> None:
            return None

    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)
    _default_output("emoji 🐍")
    assert b"?" in stream.buffer.getvalue()


@pytest.mark.parametrize(
    "overrides",
    [{"merged": False}, {"ci_summary": "some_failed"}],
)
def test_ok_is_refused_without_merge_and_all_passed(tmp_path: Path, overrides: dict) -> None:
    directory = _setup(tmp_path, [_candidate(**overrides)])
    output, _ = _run_loop(directory, ["o", "y", "q"])
    assert "use [x] exclude instead" in "\n".join(output)
    assert not (directory / "labels.jsonl").exists()


def test_also_dir_skips_previously_labeled_cases(tmp_path: Path) -> None:
    directory = _setup(
        tmp_path,
        [
            _candidate("acme/widgets#1", pr_number=1),
            _candidate("acme/widgets#2", pr_number=2),
        ],
    )
    other = tmp_path / "other"
    other.mkdir()
    write_labels(other / "labels.jsonl", [_label("acme/widgets#1", 1, "BROKE")])
    output, _ = _run_loop(directory, ["s", "q"], also_dirs=[other])
    assert "/pull/2" in _text(output)
    assert "/pull/1" not in _text(output)


def test_also_dir_missing_directory_or_file_is_an_error(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="--also-dir directory not found"):
        run_label_loop(directory, today=lambda: FIXED, also_dirs=[missing])
    missing.mkdir()
    with pytest.raises(ValueError, match="--also-dir labels file not found"):
        run_label_loop(directory, today=lambda: FIXED, also_dirs=[missing])
    result = CliRunner().invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--also-dir", str(missing)],
    )
    assert result.exit_code == 2
    assert "--also-dir labels file not found" in result.output


def test_sample_with_seed_is_deterministic(tmp_path: Path) -> None:
    candidates = [_candidate(f"acme/widgets#{n}", pr_number=n) for n in range(1, 11)]
    directory = _setup(tmp_path, candidates)
    answers = ["s"] * 5 + ["q"]
    first, _ = _run_loop(directory, answers, sample=0.5, seed=7)
    second, _ = _run_loop(directory, answers, sample=0.5, seed=7)
    assert [line for line in first if line.startswith("case:")] == [
        line for line in second if line.startswith("case:")
    ]
    assert len([line for line in first if line.startswith("case:")]) == 5


def test_selection_validation() -> None:
    with pytest.raises(ValueError, match="--sample"):
        select_cases({}, {}, sample=1.5)
    with pytest.raises(ValueError, match="--round1-label requires"):
        select_cases({}, {}, round1_label="BROKE")
    with pytest.raises(ValueError, match="--round1-label must be"):
        select_cases({}, {}, round=2, round1_label="EXCLUDE")


def test_label_stats_counts() -> None:
    candidates = {"a/b#1": {}, "a/b#2": {}}
    labels = {
        ("a/b#1", 1): _label("a/b#1", 1, "BROKE"),
        ("a/b#2", 1): _label("a/b#2", 1, "OK"),
        ("a/b#1", 2): _label("a/b#1", 2, "EXCLUDE"),
    }
    stats = compute_stats(candidates, labels)
    assert stats["rounds"] == {
        1: {"BROKE": 1, "OK": 1, "EXCLUDE": 0},
        2: {"BROKE": 0, "OK": 0, "EXCLUDE": 1},
    }
    assert stats["break_categories"]["api_break"] == 1
    assert stats["evidence_kinds"]["log_names_package"] == 1
    assert stats["exclude_reasons"]["superseded"] == 1
    assert stats["unlabeled_round_1"] == 0


def test_agreement_known_confusion_matrix() -> None:
    pairs = [
        ("BROKE", "BROKE"),
        ("BROKE", "OK"),
        ("OK", "BROKE"),
        ("OK", "OK"),
    ]
    labels: dict[tuple[str, int], Label] = {}
    for number, (first, second) in enumerate(pairs, start=1):
        case_id = f"a/b#{number}"
        labels[(case_id, 1)] = _label(case_id, 1, first)
        labels[(case_id, 2)] = _label(case_id, 2, second)
    result = compute_agreement(labels)
    assert result["n"] == 4
    assert result["matrix"] == {
        ("BROKE", "BROKE"): 1,
        ("BROKE", "OK"): 1,
        ("OK", "BROKE"): 1,
        ("OK", "OK"): 1,
    }
    assert result["raw_agreement"] == 0.5
    assert result["kappa"] == pytest.approx(0.0)
    assert result["disagreeing_case_ids"] == ["a/b#2", "a/b#3"]


def test_agreement_perfect_and_zero_variance() -> None:
    labels = {
        ("a/b#1", 1): _label("a/b#1", 1, "BROKE"),
        ("a/b#1", 2): _label("a/b#1", 2, "BROKE"),
        ("a/b#2", 1): _label("a/b#2", 1, "OK"),
        ("a/b#2", 2): _label("a/b#2", 2, "OK"),
    }
    assert compute_agreement(labels)["kappa"] == 1.0

    constant = {
        ("a/b#1", 1): _label("a/b#1", 1, "BROKE"),
        ("a/b#1", 2): _label("a/b#1", 2, "BROKE"),
        ("a/b#2", 1): _label("a/b#2", 1, "BROKE"),
        ("a/b#2", 2): _label("a/b#2", 2, "BROKE"),
    }
    assert compute_agreement(constant)["kappa"] == 1.0


def test_agreement_reports_exclude_disagreements_and_matrix_in_cli(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    directory.mkdir()
    labels = [
        _label("a/b#1", 1, "BROKE"),
        _label("a/b#1", 2, "BROKE"),
        _label("a/b#2", 1, "EXCLUDE"),
        _label("a/b#2", 2, "OK"),
    ]
    write_labels(directory / "labels.jsonl", labels)
    result = compute_agreement(dict(((label.case_id, label.round), label) for label in labels))
    assert result["exclude_disagreements"] == ["a/b#2"]
    assert result["n"] == 1
    cli = CliRunner().invoke(app, ["bench", "agreement", "--dir", str(directory)])
    assert cli.exit_code == 0
    assert "round 1 \\ round 2   BROKE   OK" in cli.output
    assert "N: 1" in cli.output
    assert "a/b#2" in cli.output


def test_corrupt_labels_line_names_line_number_without_traceback(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    valid = _label("a/b#1", 1, "BROKE").model_dump_json()
    (directory / "labels.jsonl").write_text(
        valid + "\n" + valid + "\n" + "{not json}\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["bench", "label-stats", "--dir", str(directory)])
    assert result.exit_code == 2
    assert "labels.jsonl line 3" in result.output
    assert "Traceback" not in result.output


def test_display_shows_job_links_for_failing_checks() -> None:
    text = format_case(
        _candidate(
            ci_checks=[
                {"name": "tests", "conclusion": "failure", "html_url": "https://ci/tests"},
                {"name": "build", "conclusion": "timed_out", "html_url": "https://ci/build"},
                {"name": "lint", "conclusion": "action_required", "html_url": "https://ci/lint"},
                {"name": "docs", "conclusion": "success", "html_url": "https://ci/docs"},
            ]
        )
    )
    assert "    html_url: https://ci/tests" in text
    assert "    html_url: https://ci/build" in text
    assert "    html_url: https://ci/lint" in text
    assert "https://ci/docs" not in text


@pytest.mark.parametrize(
    ("merged_at", "expected"),
    [
        ("2025-12-14T10:00:00Z", True),
        ("2025-12-15T10:00:00Z", False),
        ("2026-01-05T10:00:00Z", False),
        (None, False),
    ],
)
def test_log_expiry_note_depends_on_merged_at(merged_at: str | None, expected: bool) -> None:
    text = format_case(
        _candidate(
            merged=merged_at is not None,
            merged_at=merged_at,
            ci_checks=[{"name": "tests", "conclusion": "failure", "html_url": "https://ci/tests"}],
            failure_excerpts=[{"check_name": "tests", "log_status": "unavailable", "lines": []}],
        ),
        today=lambda: FIXED,
    )
    assert (LOG_EXPIRY_NOTE in text) is expected


def test_log_expiry_note_only_for_unavailable_logs() -> None:
    text = format_case(
        _candidate(
            merged_at="2025-01-05T10:00:00Z",
            failure_excerpts=[
                {"check_name": "tests", "log_status": "fetched_no_match", "lines": []}
            ],
        ),
        today=lambda: FIXED,
    )
    assert LOG_EXPIRY_NOTE not in text


def test_display_hides_bot_later_fix_commits(tmp_path: Path) -> None:
    directory = _setup(
        tmp_path,
        [
            _candidate(
                later_fix_commits=[
                    {
                        "sha": "bot1",
                        "date": "2026-01-06",
                        "message": "Update dependency requests to v2.32.0",
                    },
                    {
                        "sha": "fix1",
                        "date": "2026-01-07",
                        "message": "Fix requests import",
                    },
                    {
                        "sha": "bot2",
                        "date": "2026-01-08",
                        "message": "Merge pull request #2 from acme/renovate/requests",
                    },
                ]
            )
        ],
    )
    output, _ = _run_loop(directory, ["q"])
    text = _text(output)
    assert "fix1 2026-01-07 Fix requests import" in text
    assert "bot1" not in text
    assert "bot2" not in text
    assert "2 later commit(s) look like bot updates (hidden)" in text


def test_pypi_metadata_evidence_kind_is_supported_and_old_labels_load(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    legacy = {
        "case_id": "acme/widgets#9",
        "round": 1,
        "label": "BROKE",
        "break_category": "api_break",
        "evidence_kind": "log_names_package",
        "evidence": "ImportError: requests.legacy",
        "labeled_on": "2025-01-01",
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    assert load_labels(path)[("acme/widgets#9", 1)].evidence_kind == "log_names_package"

    current = _label(
        "acme/widgets#1",
        1,
        "BROKE",
        evidence_kind="pypi_metadata",
        evidence="Requires-Python >=3.11",
    )
    records, _ = save_label(path, current)
    assert load_labels(path)[("acme/widgets#1", 1)].evidence_kind == "pypi_metadata"
    assert "pypi_metadata" in EVIDENCE_KINDS
    assert compute_stats({}, records)["evidence_kinds"]["pypi_metadata"] == 1


def test_pypi_metadata_is_offered_in_the_broke_prompt(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])
    _, script = _run_loop(
        directory,
        ["b", "api_break", "pypi_metadata", "Requires-Python >=3.11", "", "", "q"],
    )
    assert any("pypi_metadata" in prompt for prompt in script.prompts)
    stored = load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)]
    assert stored.evidence_kind == "pypi_metadata"


def test_relabel_replaces_existing_label_and_preserves_other_records(tmp_path: Path) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="wrong case",
        notes="old note",
    )
    other = _label("acme/widgets#2", 1, "OK", notes="keep me")
    directory = _setup(
        tmp_path,
        [_candidate("acme/widgets#1"), _candidate("acme/widgets#2", pr_number=2)],
        [old, other],
    )
    labels_path = directory / "labels.jsonl"
    before = labels_path.read_text(encoding="utf-8").splitlines()

    output, _ = _run_loop(
        directory,
        ["x", "preexisting_failure", "correct evidence", ""],
        requested_ids=["acme/widgets#1"],
        relabel=True,
    )

    after = labels_path.read_text(encoding="utf-8").splitlines()
    stored = load_labels(labels_path)
    assert len(stored) == 2
    replacement = stored[("acme/widgets#1", 1)]
    assert replacement.label == "EXCLUDE"
    assert replacement.exclude_reason == "preexisting_failure"
    assert replacement.evidence == "correct evidence"
    assert replacement.labeled_on == FIXED
    assert stored[("acme/widgets#2", 1)] == other
    before_other = next(line for line in before if "acme/widgets#2" in line)
    after_other = next(line for line in after if "acme/widgets#2" in line)
    assert before_other == after_other
    assert "replaced previous label: EXCLUDE exclude_reason=superseded" in _text(output)


def test_relabel_history_line_has_exact_old_record(tmp_path: Path) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="wrong case",
        notes="old note",
    )
    directory = _setup(tmp_path, [_candidate()], [old])
    labels_path = directory / "labels.jsonl"
    old_line = next(
        line for line in labels_path.read_text(encoding="utf-8").splitlines() if "widgets#1" in line
    )

    _run_loop(
        directory,
        ["x", "preexisting_failure", "correct evidence", ""],
        requested_ids=["acme/widgets#1"],
        relabel=True,
    )

    history = (directory / "labels.history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 1
    entry = json.loads(history[0])
    assert entry["previous"] == json.loads(old_line)
    assert entry["replaced_by_round"] == 1
    assert "preexisting_failure" in entry["replacement_summary"]
    assert entry["replaced_on"]


def test_relabel_prints_existing_label_before_prompt(tmp_path: Path) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "BROKE",
        break_category="api_break",
        evidence_kind="log_names_package",
        evidence="old evidence",
        notes="old note",
    )
    directory = _setup(tmp_path, [_candidate()], [old])

    output, _ = _run_loop(directory, ["q"], requested_ids=["acme/widgets#1"], relabel=True)

    text = _text(output)
    heading = "existing label (will be replaced if you save a new one):"
    assert heading in text
    assert "break_category: api_break" in text
    assert "evidence_kind: log_names_package" in text
    assert "evidence: old evidence" in text
    assert "notes: old note" in text
    assert text.index(heading) < text.index("case: acme/widgets#1")
    assert load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)] == old


@pytest.mark.parametrize("choice", ["s", "q"])
def test_relabel_skip_or_quit_leaves_existing_label_unchanged(tmp_path: Path, choice: str) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="old evidence",
    )
    directory = _setup(tmp_path, [_candidate()], [old])

    _run_loop(directory, [choice], requested_ids=["acme/widgets#1"], relabel=True)

    assert load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)] == old
    assert not (directory / "labels.history.jsonl").exists()


def test_relabel_undo_restores_original_label_and_appends_history(tmp_path: Path) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="old evidence",
        notes="old note",
    )
    directory = _setup(
        tmp_path,
        [_candidate("acme/widgets#1"), _candidate("acme/widgets#2", pr_number=2)],
        [old],
    )

    output, _ = _run_loop(
        directory,
        ["x", "preexisting_failure", "correct evidence", "", "u", "q"],
        requested_ids=["acme/widgets#1", "acme/widgets#2"],
        relabel=True,
    )

    assert load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)] == old
    history = [
        json.loads(line)
        for line in (directory / "labels.history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(history) == 2
    assert "previous" in history[0]
    assert history[1]["undone_relabel"] is True
    assert history[1]["removed"]["exclude_reason"] == "preexisting_failure"
    assert history[1]["restored"] == old.model_dump(mode="json")
    assert "undid label for acme/widgets#1" in _text(output)


@pytest.mark.parametrize(
    ("kind", "answers"),
    [
        ("invalid", ["b", "api_break", "log_names_package", "", "", "q"]),
        ("cancelled", ["o", "n", "q"]),
    ],
)
def test_relabel_invalid_or_cancelled_answer_leaves_existing_label_unchanged(
    tmp_path: Path, kind: str, answers: list[str]
) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="old evidence",
    )
    directory = _setup(tmp_path, [_candidate()], [old])

    output, _ = _run_loop(directory, answers, requested_ids=["acme/widgets#1"], relabel=True)

    assert load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)] == old
    assert not (directory / "labels.history.jsonl").exists()
    assert (
        ("invalid label" in _text(output))
        if kind == "invalid"
        else ("OK label cancelled" in _text(output))
    )


def test_relabel_on_unlabeled_case_behaves_normally_and_writes_no_history(tmp_path: Path) -> None:
    directory = _setup(tmp_path, [_candidate()])

    _run_loop(
        directory,
        ["b", "api_break", "log_names_package", "evidence", "", ""],
        requested_ids=["acme/widgets#1"],
        relabel=True,
    )

    stored = load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)]
    assert stored.label == "BROKE"
    assert stored.evidence == "evidence"
    assert stored.labeled_on == FIXED
    assert not (directory / "labels.history.jsonl").exists()


def test_already_labeled_drop_message_suggests_relabel(tmp_path: Path) -> None:
    old = _label("acme/widgets#1", 1, "OK")
    directory = _setup(tmp_path, [_candidate()], [old])

    output, _ = _run_loop(directory, ["q"], requested_ids=["acme/widgets#1"])

    assert (
        "acme/widgets#1 (already labeled this round; use --relabel with --case to replace it)"
        in _text(output)
    )
    assert not (directory / "labels.history.jsonl").exists()


def test_relabel_history_append_failure_warns_and_keeps_saved_label(tmp_path: Path) -> None:
    old = _label(
        "acme/widgets#1",
        1,
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="old evidence",
    )
    directory = _setup(tmp_path, [_candidate()], [old])
    (directory / "labels.history.jsonl").mkdir()

    output, _ = _run_loop(
        directory,
        ["x", "preexisting_failure", "correct evidence", ""],
        requested_ids=["acme/widgets#1"],
        relabel=True,
    )

    assert "warning: could not append label history" in _text(output)
    stored = load_labels(directory / "labels.jsonl")[("acme/widgets#1", 1)]
    assert stored.exclude_reason == "preexisting_failure"
