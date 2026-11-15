"""Command line interface for BumpCheck."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from packaging.version import InvalidVersion, Version

from bumpcheck import __version__
from bumpcheck.apidiff import diff_versions
from bumpcheck.lockdiff import LockParseError, parse_lock_pair


class LockKind(str, Enum):
    UV_LOCK = "uv.lock"
    POETRY_LOCK = "poetry.lock"
    REQUIREMENTS = "requirements"


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
