"""Command line interface for BumpCheck."""

from __future__ import annotations

from collections import Counter
from datetime import date
from enum import Enum
from pathlib import Path

import typer
from packaging.version import InvalidVersion, Version

from bumpcheck import __version__
from bumpcheck.apidiff import ApiDiffResult, diff_versions
from bumpcheck.bench.eval import evaluate as run_eval
from bumpcheck.bench.eval import load_results
from bumpcheck.bench.github import GitHubClient, GitHubError, RateLimitWaitTooLong
from bumpcheck.bench.inspect_case import MAX_PYPI_LOOKUPS, redact, run_inspect_batch
from bumpcheck.bench.inspect_case import inspect_case as run_inspect
from bumpcheck.bench.labels import (
    compute_agreement,
    compute_stats,
    load_candidates,
    load_labels,
    run_label_loop,
)
from bumpcheck.bench.mine import mine as run_mine
from bumpcheck.bench.report import write_report
from bumpcheck.bench.snapshot import directory_size, snapshot_case, snapshot_path
from bumpcheck.check import check as run_check
from bumpcheck.check import format_markdown, format_text
from bumpcheck.envfile import EnvFileError, has_token, load_env
from bumpcheck.lockdiff import LockParseError, parse_lock_pair
from bumpcheck.usage import trace_usage
from bumpcheck.verdict import at_or_above


class LockKind(str, Enum):
    UV_LOCK = "uv.lock"
    POETRY_LOCK = "poetry.lock"
    REQUIREMENTS = "requirements"


class OutputFormat(str, Enum):
    JSON = "json"
    TEXT = "text"
    MARKDOWN = "markdown"


class FailOn(str, Enum):
    LIKELY_BREAKING = "LIKELY_BREAKING"
    REVIEW = "REVIEW"
    UNKNOWN = "UNKNOWN"


class BotChoice(str, Enum):
    DEPENDABOT = "dependabot"
    RENOVATE = "renovate"
    BOTH = "both"


class CIStatusChoice(str, Enum):
    ANY = "any"
    FAILURE = "failure"
    SUCCESS = "success"


class OnlySuggestionChoice(str, Enum):
    BROKE_SUSPECT = "BROKE_SUSPECT"
    OK_SUSPECT = "OK_SUSPECT"
    UNCLEAR = "UNCLEAR"


class Round1LabelChoice(str, Enum):
    BROKE = "BROKE"
    OK = "OK"


class SplitChoice(str, Enum):
    DEV = "dev"
    TEST = "test"
    ALL = "all"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _no_labels_message(labels_path: Path) -> str | None:
    if load_labels(labels_path):
        return None
    return f"0 labeled cases found in {labels_path} (or file missing)"


app = typer.Typer(
    add_completion=False,
    help="Analyze whether a dependency bump will break your code.",
)


MISSING_TOKEN_MESSAGE = "GITHUB_TOKEN is not set: requests are unauthenticated (60 per hour)."


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Say which env file supplied GITHUB_TOKEN.",
    ),
) -> None:
    """Analyze dependency bumps for breaking changes."""
    try:
        load_env(verbose=verbose)
    except EnvFileError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _guard_github_token(*, offline: bool, allow_unauthenticated: bool) -> None:
    if offline or has_token():
        return
    typer.echo(MISSING_TOKEN_MESSAGE, err=True)
    if not allow_unauthenticated:
        raise typer.Exit(code=2)


def _validate_max_rate_limit_wait(seconds: float) -> float:
    if seconds < 0:
        typer.echo("--max-rate-limit-wait must be zero or greater", err=True)
        raise typer.Exit(code=2)
    return seconds


@app.command()
def parse(
    old: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="The lockfile before the bump.",
    ),
    new: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="The lockfile after the bump.",
    ),
    kind: LockKind = typer.Option(
        ...,
        "--kind",
        help="The kind of lockfile being compared.",
    ),
) -> None:
    """Compare two lockfiles and print the changed packages as JSON."""
    try:
        old_text = old.read_text(encoding="utf-8-sig")
        new_text = new.read_text(encoding="utf-8-sig")
        result = parse_lock_pair(old_text, new_text, kind.value)
    except LockParseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(result.model_dump_json(indent=2))


@app.command()
def diff(
    package: str = typer.Argument(..., help="The distribution name on PyPI."),
    old: str = typer.Argument(..., help="The version before the bump."),
    new: str = typer.Argument(..., help="The version after the bump."),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Directory used to cache downloaded wheels.",
    ),
) -> None:
    """Compare the public API of two versions of a package as JSON."""
    if not package.strip():
        typer.echo("Package name must not be empty", err=True)
        raise typer.Exit(code=2)
    for label, value in (("old", old), ("new", new)):
        try:
            Version(value)
        except InvalidVersion as exc:
            typer.echo(f"Invalid {label} version: {value!r}", err=True)
            raise typer.Exit(code=2) from exc

    result = diff_versions(package, old, new, cache_dir=cache_dir)
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def usage(
    repo_path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="The repository to scan for usages.",
    ),
    package: str = typer.Argument(..., help="The distribution name on PyPI."),
    symbols: list[str] | None = typer.Option(
        None,
        "--symbols",
        help="A dotted symbol to trace. Repeat for multiple symbols.",
    ),
    api_diff: Path | None = typer.Option(
        None,
        "--api-diff",
        exists=True,
        dir_okay=False,
        readable=True,
        help="An ApiDiffResult JSON file whose changed symbols should be traced.",
    ),
    import_names: list[str] | None = typer.Option(
        None,
        "--import-name",
        help="The package import name. Repeat to override multiple names.",
    ),
) -> None:
    """Trace static usages of package symbols in a repository as JSON."""
    if not package.strip():
        typer.echo("Package name must not be empty", err=True)
        raise typer.Exit(code=2)

    collected = [symbol for symbol in (symbols or []) if symbol.strip()]
    parameters: dict[str, list[tuple[str, str]]] = {}
    api_result: ApiDiffResult | None = None
    if api_diff is not None:
        try:
            api_result = ApiDiffResult.model_validate_json(api_diff.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            typer.echo(f"Could not read API diff: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        for change in api_result.changes:
            collected.append(change.symbol)
            if change.parameter:
                parameters.setdefault(change.symbol, []).append((change.parameter, change.kind))

    if not collected:
        typer.echo("Provide at least one --symbols or an --api-diff file", err=True)
        raise typer.Exit(code=2)

    roots = list(import_names) if import_names else None
    if roots is None and api_result is not None and api_result.top_level:
        roots = list(api_result.top_level)

    result = trace_usage(
        repo_path,
        package,
        collected,
        import_names=roots,
        parameters=parameters,
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def check(
    repo_path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="The repository to scan for usages.",
    ),
    old: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="The lockfile before the bump.",
    ),
    new: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="The lockfile after the bump.",
    ),
    kind: LockKind = typer.Option(
        ...,
        "--kind",
        help="The kind of lockfile being compared.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Directory used to cache downloaded wheels.",
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.JSON,
        "--format",
        help="Output format.",
    ),
    fail_on: FailOn | None = typer.Option(
        None,
        "--fail-on",
        help="Exit with status 1 when the overall verdict is at or above this level.",
    ),
) -> None:
    """Check lockfile bumps against a repository and print verdicts."""
    try:
        old_text = old.read_text(encoding="utf-8-sig")
        new_text = new.read_text(encoding="utf-8-sig")
        result = run_check(repo_path, old_text, new_text, kind.value, cache_dir=cache_dir)
    except (OSError, LockParseError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if output_format == OutputFormat.TEXT:
        typer.echo(format_text(result))
    elif output_format == OutputFormat.MARKDOWN:
        typer.echo(format_markdown(result))
    else:
        typer.echo(result.model_dump_json(indent=2))

    if fail_on is not None and at_or_above(result.overall, fail_on.value):
        raise typer.Exit(code=1)


bench_app = typer.Typer(no_args_is_help=True, help="Mine and prepare benchmark data.")
app.add_typer(bench_app, name="bench")


@bench_app.command("mine")
def bench_mine(
    out: Path = typer.Option(
        ...,
        "--out",
        help="Directory for candidates.jsonl, excluded.jsonl and summary.json.",
    ),
    bot: BotChoice = typer.Option(
        BotChoice.BOTH,
        "--bot",
        help="Which dependency bot to mine.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Earliest closed date (YYYY-MM-DD); defaults to 180 days before --until.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Latest closed date, exclusive (YYYY-MM-DD); defaults to today.",
    ),
    limit: int = typer.Option(
        60,
        "--limit",
        help="Stop after this many kept candidates, including earlier runs.",
    ),
    max_per_repo: int = typer.Option(
        3,
        "--max-per-repo",
        help="Skip PRs from repos that already have this many candidates.",
    ),
    ci_status: CIStatusChoice = typer.Option(
        CIStatusChoice.ANY,
        "--ci-status",
        help="Only search PRs whose checks are failing or passing.",
    ),
    skip_repo_pattern: list[str] | None = typer.Option(
        None,
        "--skip-repo-pattern",
        help=(
            "Case-insensitive regex matched against owner/name; repeatable. "
            "Patterns starting with a literal '^' and containing no '/' match "
            "the repo name only. Any pattern replaces the built-in noise list."
        ),
    ),
    window_days: int = typer.Option(
        7,
        "--window-days",
        help="Length in days of each fixed search window.",
    ),
    pages_per_window: int = typer.Option(
        1,
        "--pages-per-window",
        help="Search pages to fetch per window and bot (1-10).",
    ),
    dependabot_label: str = typer.Option(
        "python",
        "--dependabot-label",
        help='Label appended to Dependabot search queries; pass "" to disable.',
    ),
    retry_excluded: list[str] | None = typer.Option(
        None,
        "--retry-excluded",
        help=(
            "Re-process stored exclusions with this reason before discovery; "
            "repeatable. Retried recoveries can push the kept count above --limit."
        ),
    ),
    logs: bool = typer.Option(
        False,
        "--logs",
        help="Fetch job logs for failing checks (requires GITHUB_TOKEN).",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Read the response cache only; never touch the network.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="GitHub response cache directory (default .bumpcheck-cache/github).",
    ),
    allow_unauthenticated: bool = typer.Option(
        False,
        "--allow-unauthenticated",
        help="Continue without GITHUB_TOKEN instead of exiting.",
    ),
    max_rate_limit_wait: float = typer.Option(
        900.0,
        "--max-rate-limit-wait",
        help="Longest rate-limit wait in seconds; 0 fails immediately.",
    ),
) -> None:
    """Mine public Dependabot/Renovate PRs as benchmark candidates."""
    max_wait = _validate_max_rate_limit_wait(max_rate_limit_wait)
    _guard_github_token(offline=offline, allow_unauthenticated=allow_unauthenticated)
    try:
        since_date = date.fromisoformat(since) if since else None
        until_date = date.fromisoformat(until) if until else None
    except ValueError as exc:
        typer.echo(f"Invalid date: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        code = run_mine(
            out=out,
            bot=bot.value,
            since=since_date,
            until=until_date,
            limit=limit,
            logs=logs,
            offline=offline,
            cache_dir=cache_dir,
            max_per_repo=max_per_repo,
            ci_status=ci_status.value,
            skip_repo_patterns=skip_repo_pattern,
            window_days=window_days,
            pages_per_window=pages_per_window,
            dependabot_label=dependabot_label,
            retry_excluded=retry_excluded,
            max_rate_limit_wait_s=max_wait,
        )
    except RateLimitWaitTooLong as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except (GitHubError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if code:
        raise typer.Exit(code=code)


@bench_app.command("label")
def bench_label(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl and labels.jsonl.",
    ),
    round: int = typer.Option(1, "--round", help="Review round (1 or greater)."),
    only: OnlySuggestionChoice | None = typer.Option(
        None,
        "--only",
        help="Only show cases with this miner suggestion.",
    ),
    case: list[str] | None = typer.Option(
        None,
        "--case",
        help="Only show this case id; repeatable.",
    ),
    relabel: bool = typer.Option(
        False,
        "--relabel",
        help="Allow replacing labels for cases already labeled in this round; requires --case.",
    ),
    sample: float | None = typer.Option(
        None,
        "--sample",
        help="Show this fraction of selected cases (with --seed).",
    ),
    seed: int = typer.Option(0, "--seed", help="Seed for --sample."),
    show_suggestion: bool = typer.Option(
        False,
        "--show-suggestion",
        help="Reveal the miner's label_suggestion (hidden by default).",
    ),
    also_dir: list[Path] | None = typer.Option(
        None,
        "--also-dir",
        help="Skip case_ids already labeled for this round in another directory; repeatable.",
    ),
    round1_label: Round1LabelChoice | None = typer.Option(
        None,
        "--round1-label",
        help="For round 2+, restrict the pool to this round-1 label.",
    ),
) -> None:
    """Hand-label mined candidates as BROKE, OK or EXCLUDE."""
    if relabel and not case:
        typer.echo("--relabel requires at least one --case", err=True)
        raise typer.Exit(code=2)
    if relabel and (sample is not None or only is not None or also_dir):
        typer.echo(
            "--relabel cannot be combined with --sample, --only, or --also-dir; "
            "those filters would hide or change the meaning of a targeted relabel",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        code = run_label_loop(
            dir,
            round=round,
            only=only.value if only is not None else None,
            requested_ids=case,
            relabel=relabel,
            sample=sample,
            seed=seed,
            show_suggestion=show_suggestion,
            also_dirs=also_dir,
            round1_label=round1_label.value if round1_label is not None else None,
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if code:
        raise typer.Exit(code=code)


@bench_app.command("label-stats")
def bench_label_stats(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl and labels.jsonl.",
    ),
) -> None:
    """Print labeling counts by round and evidence category."""
    try:
        candidates = load_candidates(dir / "candidates.jsonl")
        labels = load_labels(dir / "labels.jsonl")
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    stats = compute_stats(candidates, labels)
    total = sum(sum(counts.values()) for counts in stats["rounds"].values())
    detail = ", ".join(
        f"round {number}: {sum(counts.values())}" for number, counts in stats["rounds"].items()
    )
    typer.echo(f"labeled cases: {total} ({detail})")
    for number, counts in stats["rounds"].items():
        typer.echo(
            f"round {number}: " + " ".join(f"{name}={count}" for name, count in counts.items())
        )
    for heading, key in (
        ("break_category", "break_categories"),
        ("evidence_kind", "evidence_kinds"),
        ("exclude_reason", "exclude_reasons"),
    ):
        counts = stats[key]
        typer.echo(f"{heading}: " + " ".join(f"{name}={count}" for name, count in counts.items()))
    typer.echo(f"unlabeled in round 1: {stats['unlabeled_round_1']}")


@bench_app.command("agreement")
def bench_agreement(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing labels.jsonl.",
    ),
) -> None:
    """Report round 1 vs round 2 agreement on BROKE/OK labels."""
    try:
        result = compute_agreement(load_labels(dir / "labels.jsonl"))
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"N: {result['n']}")
    typer.echo("round 1 \\ round 2   BROKE   OK")
    for first in ("BROKE", "OK"):
        matrix = result["matrix"]
        typer.echo(f"{first:8}  {matrix[(first, 'BROKE')]:7} {matrix[(first, 'OK')]:4}")
    raw = result["raw_agreement"]
    kappa = result["kappa"]
    raw_text = "n/a" if raw is None else f"{raw:.3f}"
    kappa_text = "n/a" if kappa is None else f"{kappa:.3f}"
    typer.echo(f"raw agreement: {raw_text}")
    typer.echo(f"cohen's kappa: {kappa_text}")
    typer.echo("disagreeing: " + (", ".join(result["disagreeing_case_ids"]) or "none"))
    typer.echo("EXCLUDE disagreements: " + (", ".join(result["exclude_disagreements"]) or "none"))


@bench_app.command("inspect")
def bench_inspect(
    case_id: str = typer.Argument(
        ...,
        help="Candidate case id, e.g. owner/repo#123.",
    ),
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl.",
    ),
    also_dir: list[Path] | None = typer.Option(
        None,
        "--also-dir",
        help="Fallback directories to search for the case; repeatable.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Read the response cache only; never touch the network.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Response cache directory (default .bumpcheck-cache/github).",
    ),
    allow_unauthenticated: bool = typer.Option(
        False,
        "--allow-unauthenticated",
        help="Continue without GITHUB_TOKEN instead of exiting.",
    ),
    max_rate_limit_wait: float = typer.Option(
        900.0,
        "--max-rate-limit-wait",
        help="Longest rate-limit wait in seconds; 0 fails immediately.",
    ),
    max_pypi_lookups: int = typer.Option(
        MAX_PYPI_LOOKUPS,
        "--max-pypi-lookups",
        help="Maximum base-pin PyPI lookups per case.",
    ),
) -> None:
    """Print a read-only inspection report for one candidate."""
    max_wait = _validate_max_rate_limit_wait(max_rate_limit_wait)
    if max_pypi_lookups < 1:
        typer.echo("--max-pypi-lookups must be one or greater", err=True)
        raise typer.Exit(code=2)
    _guard_github_token(offline=offline, allow_unauthenticated=allow_unauthenticated)
    try:
        report = run_inspect(
            case_id,
            dir=dir,
            also_dirs=also_dir,
            offline=offline,
            cache_dir=cache_dir,
            max_rate_limit_wait_s=max_wait,
            max_pypi_lookups=max_pypi_lookups,
        )
    except RateLimitWaitTooLong as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=3) from exc
    except (GitHubError, ValueError, OSError) as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(report, nl=False)


@bench_app.command("inspect-batch")
def bench_inspect_batch(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        help="File to write the condensed inspection report.",
    ),
    only: OnlySuggestionChoice | None = typer.Option(
        None,
        "--only",
        help="Only inspect cases with this miner suggestion.",
    ),
    case: list[str] | None = typer.Option(
        None,
        "--case",
        help="Only inspect this case id; repeatable.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Read the response cache only; never touch the network.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Response cache directory (default .bumpcheck-cache/github).",
    ),
    allow_unauthenticated: bool = typer.Option(
        False,
        "--allow-unauthenticated",
        help="Continue without GITHUB_TOKEN instead of exiting.",
    ),
    max_rate_limit_wait: float = typer.Option(
        900.0,
        "--max-rate-limit-wait",
        help="Longest rate-limit wait in seconds; 0 fails immediately.",
    ),
    max_pypi_lookups: int = typer.Option(
        MAX_PYPI_LOOKUPS,
        "--max-pypi-lookups",
        help="Maximum base-pin PyPI lookups per case.",
    ),
) -> None:
    """Write a condensed read-only inspection report for selected cases."""
    max_wait = _validate_max_rate_limit_wait(max_rate_limit_wait)
    if max_pypi_lookups < 1:
        typer.echo("--max-pypi-lookups must be one or greater", err=True)
        raise typer.Exit(code=2)
    _guard_github_token(offline=offline, allow_unauthenticated=allow_unauthenticated)
    try:
        destination = run_inspect_batch(
            Path(dir),
            out=Path(out),
            only=only.value if only is not None else None,
            case_ids=case,
            offline=offline,
            cache_dir=cache_dir,
            max_rate_limit_wait_s=max_wait,
            max_pypi_lookups=max_pypi_lookups,
        )
    except RateLimitWaitTooLong as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=3) from exc
    except (ValueError, OSError) as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"wrote {destination}")


@bench_app.command("snapshot")
def bench_snapshot(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl; snapshots are written under it.",
    ),
    case: list[str] | None = typer.Option(
        None,
        "--case",
        help="Only snapshot this case id; repeatable.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Rewrite terminal snapshots instead of skipping them.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Read cached HTTP responses only; never touch the network.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="HTTP response cache directory.",
    ),
    allow_unauthenticated: bool = typer.Option(
        False,
        "--allow-unauthenticated",
        help="Continue without GITHUB_TOKEN instead of exiting.",
    ),
    max_rate_limit_wait: float = typer.Option(
        900.0,
        "--max-rate-limit-wait",
        help="Longest GitHub rate-limit wait in seconds; 0 fails immediately.",
    ),
) -> None:
    """Fetch offline wheel and repo snapshots for benchmark cases."""
    max_wait = _validate_max_rate_limit_wait(max_rate_limit_wait)
    _guard_github_token(offline=offline, allow_unauthenticated=allow_unauthenticated)
    try:
        candidates = load_candidates(dir / "candidates.jsonl")
        wanted = set(case or ())
        client = GitHubClient(
            offline=offline,
            cache_dir=cache_dir,
            max_rate_limit_wait_s=max_wait,
        )
        failed_cases: list[str] = []
        for case_id in sorted(candidates):
            if wanted and case_id not in wanted:
                continue
            candidate = candidates[case_id]
            record = snapshot_case(
                candidate,
                dir / "snapshots",
                github_client=client,
                force=force,
            )
            status = str(record.get("status") or "failed")
            size = directory_size(snapshot_path(dir / "snapshots", candidate))
            line = f"{case_id}: {status} ({size} bytes)"
            error = record.get("error")
            if error:
                line += f" - {redact(str(error))}"
            typer.echo(line)
            if status == "failed":
                failed_cases.append(case_id)
    except (GitHubError, ValueError, OSError) as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=2) from exc
    root = dir / "snapshots"
    typer.echo(f"snapshot disk usage: {directory_size(root)} bytes")
    if failed_cases:
        raise typer.Exit(code=1)


@bench_app.command("eval")
def bench_eval(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing candidates.jsonl, labels.jsonl and snapshots.",
    ),
    split: SplitChoice = typer.Option(
        SplitChoice.DEV,
        "--split",
        help="Which candidate split to evaluate.",
    ),
    timeout: float = typer.Option(
        120.0,
        "--timeout",
        help="Hard per-case worker timeout in seconds.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-evaluate cases even with a matching fingerprint.",
    ),
    case: list[str] | None = typer.Option(
        None,
        "--case",
        help="Only evaluate this case id; repeatable.",
    ),
    jobs: int = typer.Option(
        1,
        "--jobs",
        help="Number of concurrent worker processes.",
    ),
) -> None:
    """Evaluate labeled benchmark cases against snapshots."""
    if timeout <= 0:
        typer.echo("--timeout must be greater than zero", err=True)
        raise typer.Exit(code=2)
    if jobs < 1:
        typer.echo("--jobs must be one or greater", err=True)
        raise typer.Exit(code=2)
    try:
        message = _no_labels_message(dir / "labels.jsonl")
        if message is not None:
            typer.echo(message)
            return
        records = run_eval(
            dir,
            split=split.value,
            timeout=timeout,
            force=force,
            case_ids=set(case or ()),
            jobs=jobs,
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    counts = Counter(str(record.get("label")) for record in records)
    summary = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    typer.echo(f"results: {len(records)} {summary}")


@bench_app.command("report")
def bench_report(
    dir: Path = typer.Option(
        ...,
        "--dir",
        help="Directory containing eval/results.jsonl.",
    ),
    split: SplitChoice = typer.Option(
        SplitChoice.DEV,
        "--split",
        help="Which split to report.",
    ),
) -> None:
    """Write and print a Markdown benchmark report."""
    try:
        message = _no_labels_message(dir / "labels.jsonl")
        if message is not None:
            typer.echo(message)
            return
        records = load_results(dir / "eval" / "results.jsonl")
        text = write_report(dir, records, split=split.value)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(text, nl=False)
