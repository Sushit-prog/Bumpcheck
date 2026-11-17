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
    Label,
    _default_output,
    compute_agreement,
    compute_stats,
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
    assert "session: 0 label(s) saved, 1 skipped" in _text(output)
    assert not (directory / "labels.jsonl").exists()


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
