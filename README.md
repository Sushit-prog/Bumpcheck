# BumpCheck

**BumpCheck analyzes whether a Python dependency update in a Dependabot or Renovate pull request is likely to break your code.**

It combines **lockfile analysis, public API diffing, static usage tracing, Python-version compatibility checks, and rule-based risk classification** to turn dependency bumps into actionable compatibility verdicts.

> **Status:** Early development — `v0.1.2`

---

## Quick Start

### 1. Install BumpCheck

BumpCheck is published as the `bumpcheck-py` distribution.

```bash
uv tool install bumpcheck-py
```

Verify the installation:

```bash
bumpcheck --help
```

---

### 2. Run it against a dependency bump

BumpCheck compares two versions of a lockfile and analyzes the dependency changes against your repository.

For a `uv.lock` update:

```bash
bumpcheck check REPO_PATH OLD_LOCKFILE NEW_LOCKFILE --kind uv.lock
```

For example:

```bash
bumpcheck check . /tmp/base/uv.lock /tmp/head/uv.lock --kind uv.lock
```

You can choose the output format:

```bash
bumpcheck check . /tmp/base/uv.lock /tmp/head/uv.lock \
  --kind uv.lock \
  --format markdown
```

Possible overall results are:

```text
SAFE
REVIEW
LIKELY_BREAKING
UNKNOWN
```

---

### 3. Understand the result

A typical workflow looks like:

```text
Dependency bump
      │
      ▼
Parse lockfiles
      │
      ▼
Compare package APIs
      │
      ▼
Trace repository usage
      │
      ▼
Check Python compatibility
      │
      ▼
Apply safety rules
      │
      ▼
SAFE / REVIEW / LIKELY_BREAKING / UNKNOWN
```

For example:

```text
requests 2.31.0 → 2.32.0

API changes:       3
Repository hits:   0
Blocking risks:    0
Python support:    compatible

Verdict: SAFE
```

A `SAFE` result means BumpCheck found no relevant API changes used by the repository and no blocking analysis risks.

It does **not** prove runtime or behavioral compatibility.

---

## Quick API Diff

You can inspect the public API changes between two PyPI releases independently:

```bash
bumpcheck diff requests 2.31.0 2.32.0
```

BumpCheck:

- downloads wheels only
- verifies their hashes
- does not install the package
- does not import the package
- unpacks the wheels into its cache

If an API comparison cannot be performed, BumpCheck returns `no_api_diff` with an explanation.

---

## Quick Usage Trace

To see whether your repository uses specific symbols from a package:

```bash
bumpcheck usage . requests \
  --symbols Session \
  --symbols get \
  --symbols request
```

The tracer uses Python's AST and does not execute your repository.

You can also trace every changed symbol from an API-diff result:

```bash
bumpcheck usage . requests \
  --api-diff API_DIFF.json
```

---

## Quick GitHub Action

The easiest way to run BumpCheck automatically on dependency-update pull requests is the included GitHub Action.

Create:

```text
.github/workflows/bumpcheck.yml
```

with:

```yaml
name: BumpCheck

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  dependency-safety:
    runs-on: ubuntu-latest

    steps:
      - uses: Sushit-prog/Bumpcheck@main
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          lockfile: uv.lock
          fail-on: REVIEW
```

The action compares the base and PR versions of the lockfile, generates a Markdown report, uploads it as an artifact, and posts a sticky PR comment.

The PR's Python code is **never executed**.

### What happens on a pull request?

```text
Dependabot / Renovate PR
          │
          ▼
      BumpCheck
          │
     ┌────┴────┐
     ▼         ▼
 Lockfile    API diff
     │         │
     └────┬────┘
          ▼
    Usage tracing
          │
          ▼
 Python compatibility
          │
          ▼
       Verdict
          │
     ┌────┼──────────────┐
     ▼    ▼              ▼
   SAFE REVIEW   LIKELY_BREAKING
                    │
                    ▼
                  CI fails
```

With:

```yaml
fail-on: REVIEW
```

the workflow fails for:

```text
REVIEW
LIKELY_BREAKING
UNKNOWN
```

while allowing:

```text
SAFE
```

to pass.

---

## Quick Real-World Validation

To test the lockfile parser against real dependency-update PRs, create:

```text
realworld/prs.txt
```

Add one GitHub PR URL per line:

```text
https://github.com/example/project/pull/123
https://github.com/example/project/pull/456
```

Then run:

```bash
uv run python scripts/realworld.py fetch
uv run python scripts/realworld.py report
```

Results are written to:

```text
realworld/report.md
realworld/report.json
```

---

## Quick Benchmark

To mine real Dependabot and Renovate dependency-update PRs:

```bash
uv run bumpcheck bench mine \
  --out bench/v0-candidates \
  --bot both \
  --since 2026-01-01 \
  --limit 60 \
  --logs
```

Then inspect candidates:

```bash
uv run bumpcheck bench inspect \
  acme/widgets#123 \
  --dir bench/v0-candidates
```

Label them:

```bash
uv run bumpcheck bench label \
  --dir bench/v0-candidates
```

Evaluate the labeled dataset:

```bash
uv run bumpcheck bench eval \
  --dir bench/v0-candidates
```

Generate the report:

```bash
uv run bumpcheck bench report \
  --dir bench/v0-candidates
```

---

# Why BumpCheck?

Automated dependency updates create a simple but important question:

> **Can this dependency update safely merge?**

A version change alone cannot answer that.

BumpCheck looks at the actual relationship between:

- the package version that changed
- the package's public API
- the symbols your repository uses
- the way those symbols are called
- the Python versions your project supports
- static-analysis limitations and risks

The result is a more evidence-based signal than simply classifying updates by semantic versioning.

---

# Key Features

- 🔍 Public API diffing between PyPI versions
- 🧭 Static Python usage tracing
- 🔐 No package installation or importing during API analysis
- 📦 Wheel-only analysis with hash verification
- 🐍 `requires_python` compatibility analysis
- 📋 `SAFE`, `REVIEW`, `LIKELY_BREAKING`, and `UNKNOWN` verdicts
- 🤖 Dependabot and Renovate PR analysis
- ⚙️ Reusable GitHub composite action
- 📊 Benchmark mining and evaluation
- 🧪 Acceptance tests against known breaking releases
- 💾 Offline cache support
- 🛡️ Repository code is never executed
- 🚦 Configurable CI failure thresholds

---

# How It Works

```text
Dependency update
       │
       ▼
   Lockfile
    parsing
       │
       ▼
   API diff
       │
       ▼
 Usage tracing
       │
       ▼
Python support
   analysis
       │
       ▼
  Safety rules
       │
       ▼
    Verdict
```

BumpCheck intentionally prefers uncertainty over false safety.

If the available static evidence is insufficient to establish that an update is safe, it returns `UNKNOWN` or `REVIEW` instead of claiming `SAFE`.

---

# Verdicts

| Verdict | Meaning |
|---|---|
| `SAFE` | No relevant API changes or blocking risks were found |
| `REVIEW` | A change or analysis risk requires human review |
| `LIKELY_BREAKING` | Strong evidence indicates the update can break the repository |
| `UNKNOWN` | BumpCheck could not establish a reliable result |

Severity is ordered as:

```text
LIKELY_BREAKING > REVIEW > UNKNOWN > SAFE
```

Therefore, an `UNKNOWN` dependency bump prevents an overall `SAFE` result.

---

# What `SAFE` Does and Does Not Mean

`SAFE` means:

> **The API diff found no relevant changes, and static analysis found no matching direct usage or blocking risks.**

It does not prove:

- behavioral compatibility
- runtime-only imports
- transitive dependency compatibility
- hidden dynamic usage
- untraced instance flow
- application-level semantic correctness

BumpCheck is a compatibility signal, not a replacement for tests or CI.

---

# API Diff

Compare two versions of a PyPI package:

```bash
bumpcheck diff PACKAGE OLD_VERSION NEW_VERSION
```

Example:

```bash
bumpcheck diff requests 2.31.0 2.32.0
```

Cache location:

```bash
--cache-dir PATH
```

or:

```bash
BUMPCHECK_CACHE
```

Packages without a usable wheel produce:

```text
no_api_diff
```

with a reason explaining why.

---

# Usage Tracer

Find where repository Python code touches package symbols:

```bash
bumpcheck usage REPO_PATH PACKAGE --symbols SYMBOL
```

Or use an API diff:

```bash
bumpcheck usage REPO_PATH PACKAGE \
  --api-diff API_DIFF.json
```

The tracer reports:

- calls
- attributes
- imports
- references
- keyword arguments
- positional argument counts
- star-argument usage
- `exact` or `possible` confidence

The analysis is performed using Python's `ast` module.

Repository code is never imported or executed.

---

# Usage Tracer Risks

BumpCheck reports analysis conditions that can make a clean result unsafe to treat as proof:

| Risk | Meaning |
|---|---|
| `star_import` | `from package import *` prevents reliable name resolution |
| `dynamic_getattr` | Non-literal `getattr()` is used on a package module |
| `dynamic_import` | Dynamic import targets the package |
| `dynamic_import_unknown_target` | Dynamic import target cannot be determined |
| `unparsable_file` | A Python file could not be parsed |
| `scan_truncated` | The 5,000-file scan cap was reached |
| `method_target_untraced` | A method-like target could not be associated with a usage |

Blocking risks cause BumpCheck to prefer `REVIEW` over `SAFE`.

---

# Python Compatibility

BumpCheck evaluates `requires_python` across CPython 3.8–3.14.

Project Python versions are resolved using:

1. CI matrices
2. declared project configuration
3. `.python-version`

A dependency that drops support for a concrete Python version used by the project can therefore produce:

```text
LIKELY_BREAKING
```

Unevaluable compatibility produces:

```text
UNKNOWN
```

---

# GitHub Action

The root `action.yml` provides a reusable composite action.

Inputs:

| Input | Default | Description |
|---|---|---|
| `github-token` | — | GitHub authentication |
| `lockfile` | `uv.lock` | Lockfile to analyze |
| `lock-kind` | auto | Lockfile type |
| `fail-on` | `REVIEW` | Failure threshold |
| `comment` | `true` | Post PR comment |
| `artifact-name` | — | Artifact name |

Outputs:

| Output | Description |
|---|---|
| `overall` | Overall verdict |
| `report-path` | Generated report |
| `exit-code` | Process exit code |

Required GitHub permissions:

```yaml
permissions:
  contents: read
  pull-requests: write
```

---

# Benchmark

BumpCheck includes tooling for mining real-world Dependabot and Renovate pull requests.

```bash
uv run bumpcheck bench mine \
  --out bench/v0-candidates \
  --bot both \
  --since 2026-01-01 \
  --limit 60 \
  --logs
```

The miner produces:

```text
candidates.jsonl
excluded.jsonl
summary.json
```

CI outcomes are treated as signals rather than ground-truth labels.

Every candidate still requires human verification.

---

# Labeling

```bash
uv run bumpcheck bench label --dir bench/v0-candidates
```

Statistics:

```bash
uv run bumpcheck bench label-stats --dir bench/v0-candidates
```

Agreement:

```bash
uv run bumpcheck bench agreement --dir bench/v0-candidates
```

`BROKE` labels require evidence connecting the failure to the bumped package.

A failing CI check alone is not sufficient evidence.

---

# Evaluation

Create snapshots:

```bash
uv run bumpcheck bench snapshot \
  --dir bench/v0-candidates
```

Evaluate:

```bash
uv run bumpcheck bench eval \
  --dir bench/v0-candidates
```

Generate the report:

```bash
uv run bumpcheck bench report \
  --dir bench/v0-candidates
```

The benchmark supports `dev`, `test`, and `all` splits.

---

# Acceptance Tests

Known breaking releases are listed in:

```text
acceptance/pairs.json
```

Run:

```bash
uv run python scripts/apidiff_acceptance.py run
```

Results are written to:

```text
acceptance/report.md
acceptance/report.json
```

---

# Real-World Validation

Create:

```text
realworld/prs.txt
```

with one dependency-update PR URL per line.

Then:

```bash
uv run python scripts/realworld.py fetch
uv run python scripts/realworld.py report
```

Output:

```text
realworld/report.md
realworld/report.json
```

---

# Credentials

GitHub operations use `GITHUB_TOKEN`.

BumpCheck checks:

1. `BUMPCHECK_ENV_FILE`
2. `~/.bumpcheck/.env`
3. `./.env`

A real environment variable takes precedence.

Example:

```dotenv
GITHUB_TOKEN=ghp_your_token_here
```

Never commit the token.

For POSIX systems:

```bash
chmod 600 ~/.bumpcheck/.env
```

BumpCheck never writes the token to cache or output files.

---

# Security Model

BumpCheck is deliberately designed not to execute target repository code.

API analysis:

```text
PyPI wheel
    │
    ▼
Hash verification
    │
    ▼
Wheel extraction
    │
    ▼
Static API analysis
```

Repository analysis:

```text
Python source
     │
     ▼
AST parsing
     │
     ▼
Static symbol matching
```

There is no requirement to install or import the dependency being analyzed.

---

# Limitations

BumpCheck currently focuses on direct static Python usage.

It does not fully resolve:

- cross-function data flow
- return-value flow
- user-module re-exports
- transitive dependencies
- dynamic imports
- dynamic attribute access
- arbitrary runtime behavior

Files larger than 2 MB and symlinks are skipped, and the scan is capped at 5,000 files.

These limitations are surfaced as analysis risks rather than silently ignored.

---

# Project Status

**Early development — `v0.1.2`**

The current release focuses on establishing a conservative dependency-safety pipeline combining:

```text
Lockfile analysis
        +
API diffing
        +
Static usage tracing
        +
Python compatibility
        +
Explicit risk rules
        =
Dependency-update verdict
```

The benchmark is currently small and should not be interpreted as production-grade accuracy.

---

# License

MIT License.
