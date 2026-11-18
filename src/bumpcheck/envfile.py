"""Load GITHUB_TOKEN from .env-style files without trusting repository content."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path

TOKEN_KEY = "GITHUB_TOKEN"
_EXPORT_PREFIX = "export "


class EnvFileError(Exception):
    """A configured environment file could not be used."""


def has_token(environ: MutableMapping[str, str] | None = None) -> bool:
    """Whether GITHUB_TOKEN is set to a non-empty value."""
    source = os.environ if environ is None else environ
    return bool(source.get(TOKEN_KEY, "").strip())


def load_env(
    *,
    verbose: bool = False,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    warn: Callable[[str], None] | None = None,
) -> Path | None:
    """Fill GITHUB_TOKEN from the first file that supplies a non-empty value.

    Sources, in order: BUMPCHECK_ENV_FILE, ~/.bumpcheck/.env, ./.env. Only
    GITHUB_TOKEN is ever read. A non-empty value already in ``environ`` wins.
    """
    env = os.environ if environ is None else environ
    emit = warn if warn is not None else _stderr
    if has_token(env):
        return None

    sources: list[Path] = []
    override = env.get("BUMPCHECK_ENV_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise EnvFileError(f"BUMPCHECK_ENV_FILE file does not exist: {path}")
        sources.append(path)
    base = Path.cwd() if cwd is None else cwd
    user_home = Path.home() if home is None else home
    sources.append(user_home / ".bumpcheck" / ".env")
    sources.append(base / ".env")

    seen: set[Path] = set()
    for path in sources:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.is_file():
            continue
        _warn_permissions(path, emit)
        value = _read_token(path)
        if value is None:
            continue
        env[TOKEN_KEY] = value
        if verbose:
            emit(f"GITHUB_TOKEN loaded from {path}")
        return path
    return None


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _warn_permissions(path: Path, emit: Callable[[str], None]) -> None:
    if sys.platform == "win32":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        emit(f"warning: {path} is readable by group or others; restrict it with chmod 600")


def _read_token(path: Path) -> str | None:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(_EXPORT_PREFIX):
            line = line[len(_EXPORT_PREFIX) :].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != TOKEN_KEY:
            continue
        value = _unquote(value.strip())
        if value:
            return value
    return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
