"""Pydantic models for BumpCheck."""

from __future__ import annotations

from typing import Literal

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LockSource = Literal["uv.lock", "poetry.lock", "requirements"]


class Bump(BaseModel):
    """A single dependency change between two lockfiles."""

    model_config = ConfigDict(frozen=True)

    package: str
    old_version: str | None
    new_version: str | None
    source: LockSource

    @field_validator("package")
    @classmethod
    def _canonicalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("old_version", "new_version")
    @classmethod
    def _validate_version(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                Version(value)
            except InvalidVersion as exc:
                raise ValueError(f"Invalid version: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _validate_change(self) -> Bump:
        if self.old_version is None and self.new_version is None:
            raise ValueError("A bump must have an old version, a new version, or both")
        if self.old_version is not None and self.old_version == self.new_version:
            raise ValueError("A bump must change the version")
        return self


class Unresolved(BaseModel):
    """A package that resolves to multiple versions and cannot be compared."""

    model_config = ConfigDict(frozen=True)

    package: str
    removed: list[str] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)

    @field_validator("package")
    @classmethod
    def _canonicalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("removed", "added")
    @classmethod
    def _sort_versions(cls, value: list[str]) -> list[str]:
        return sorted(value, key=Version)


class ParseResult(BaseModel):
    """The outcome of comparing two lockfiles."""

    model_config = ConfigDict(frozen=True)

    bumps: list[Bump]
    warnings: list[str]
    unresolved: list[Unresolved] = Field(default_factory=list)

    @field_validator("unresolved")
    @classmethod
    def _sort_unresolved(cls, value: list[Unresolved]) -> list[Unresolved]:
        return sorted(value, key=lambda item: item.package)
