"""Command line interface for BumpCheck."""

from __future__ import annotations

from datetime import date
from enum import Enum
from pathlib import Path

import typer
from packaging.version import InvalidVersion, Version

from bumpcheck import __version__
from bumpcheck.apidiff import ApiDiffResult, diff_versions
from bumpcheck.bench.github import GitHubError
from bumpcheck.bench.mine import mine as run_mine
from bumpcheck.check import check as run_check
from bumpcheck.check import format_text
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


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(
    add_completion=False,
    help="Analyze whether a dependency bump will break your code.",
)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Analyze dependency bumps for breaking changes."""


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
) -> None:
    """Mine public Dependabot/Renovate PRs as benchmark candidates."""
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
        )
    except (GitHubError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if code:
        raise typer.Exit(code=code)
