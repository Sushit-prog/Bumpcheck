BumpCheck analyzes whether a dependency bump in a Dependabot or Renovate pull request will break your code.

Status: early development

## Real-world validation

Validate the lockfile parser against real GitHub pull requests. List the PR
URLs (one per line, `#` comments allowed) in `realworld/prs.txt`, then run:

```
uv run python scripts/realworld.py fetch
uv run python scripts/realworld.py report
```

`fetch` downloads the before/after lockfiles into `realworld/data/` and
`report` writes `realworld/report.md` and `realworld/report.json`, including
any places where the parser fails or disagrees with the PR title.

## API diff

Compare the public API of two versions of a PyPI package:

```
uv run bumpcheck diff PACKAGE OLD_VERSION NEW_VERSION [--cache-dir PATH]
```

BumpCheck downloads wheels only (never source distributions, and it never
installs or imports the package), verifies their hashes, and unpacks them into
a cache directory (`--cache-dir`, else `$BUMPCHECK_CACHE`, else
`~/.cache/bumpcheck`). Packages without a usable wheel produce a
`no_api_diff` result whose `reason` explains why.

## Usage tracer

Find where a repository's own Python code touches symbols from a package:

```
uv run bumpcheck usage REPO_PATH PACKAGE --symbols SYM [--symbols SYM ...]
uv run bumpcheck usage REPO_PATH PACKAGE --api-diff API_DIFF.json
```

`--api-diff` accepts an `ApiDiffResult` JSON file and traces every changed
symbol, including parameter names and change kinds in the `parameters` field
of the result. `--import-name` overrides the import name(s) used for matching;
it may be repeated. Without `--api-diff`, the default import name is the
package name with `-` replaced by `_`. With `--api-diff`, its `top_level`
list supplies the default import names.

The tracer is static: it parses Python files with `ast` and never imports or
executes repository code. It reports `call`, `attribute`, `import`, and
`reference` hits with a source snippet, keyword argument names, positional
argument counts, star-argument flags, and an `exact` or `possible` confidence.
It also reports risks that make a clean result unsafe to treat as proof:

- `star_import`: `from package import *` makes names unresolvable.
- `dynamic_getattr`: non-literal `getattr` on a package module.
- `dynamic_import`: `importlib.import_module` / `__import__` with a package
  name.
- `dynamic_import_unknown_target`: a non-literal dynamic import.
- `unparsable_file`: at least one file could not be parsed.
- `scan_truncated`: the 5000-file cap was reached.
- `method_target_untraced`: a method-like target had no related hits.

Same-scope assignments of the form `x = PackageClass(...)` are tracked so
`x.method()` and `x.attribute` resolve to the target symbol with `possible`
confidence. Files are parsed as bytes with PEP 263 encoding detection; files
larger than 2 MB and symlinks are skipped, and excluded directories include
`.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `build`, `dist`, and
`site-packages`.

### Usage tracer limitations

- Method calls on instances are only tracked for direct same-scope assignments
  (`x = Target(...)`); cross-function flow and return values are not followed.
- Re-exports through the user's own modules are not followed. A package's own
  `__init__` re-exports are approximated by a final-segment fallback under the
  import root, with `possible` confidence.
- Transitive dependencies are not traced; only imports that resolve to the
  requested package are considered.
- Star imports, `getattr`, and dynamic imports are flagged as risks but their
  concrete targets are not resolved.
- The import table is file-level, not scope-aware, so shadowed or locally
  imported names can produce conservative matches.
- Package source is skipped only when the repository's `pyproject.toml`
  `[project].name` canonicalizes to the requested package.

## Verdicts and rules

Classify every bump between two lockfiles against a repository:

```
uv run bumpcheck check REPO_PATH OLD NEW --kind uv.lock [--format json|text]
uv run bumpcheck check REPO_PATH OLD NEW --kind uv.lock --fail-on REVIEW
```

The pipeline parses the lockfiles, runs the API diff for each bumped package,
traces usages in the repository, and applies the first matching rule:

| Rule | Condition | Verdict |
| ---- | --------- | ------- |
| R0 | A package has multiple versions or no comparable version | `UNKNOWN` |
| R1 | The API diff returned `no_api_diff` | `UNKNOWN` |
| R2 | The API diff is `ok` and has no changes | `SAFE` |
| R3 | Changes exist but usage was not traced | `UNKNOWN` |
| R4 | An exact-confidence hit matches breaking or high-severity changes | `LIKELY_BREAKING` / `REVIEW` |
| R5 | Any other usage hit matches a change | `REVIEW` |
| R6 | No matching hits, but a blocking risk exists | `REVIEW` |
| R7 | No matching hits and no blocking risks | `SAFE` |
| E1 | One bump raised an exception; other bumps still run | `UNKNOWN` |

R4 marks `OBJECT_REMOVED` and `OBJECT_CHANGED_KIND` as `LIKELY_BREAKING`.
`PARAMETER_REMOVED` is breaking when the call passes that keyword, and
`PARAMETER_ADDED_REQUIRED` is breaking when the call omits it without star
arguments or keyword splats. Other high-severity parameter changes are
`REVIEW`. R6 blocking risks are `star_import`, `dynamic_getattr`,
`dynamic_import`, `scan_truncated`, `method_target_untraced`, and
`unparsable_file`; `dynamic_import_unknown_target` alone does not block `SAFE`.
Overall severity is `LIKELY_BREAKING` > `REVIEW` > `UNKNOWN` > `SAFE`, so any
`UNKNOWN` bump prevents an overall `SAFE`.

### What SAFE does and does not mean

`SAFE` means the API diff found no relevant changes and the static tracer found
no direct usages that match them. It covers API signatures and direct static
usage only. It does not prove behavioral compatibility, transitive breakage,
runtime-only imports, or usage reached through untraced instance flow.

## Benchmark

Mine public Dependabot and Renovate pull requests into hand-labellable
candidates:

```
uv run bumpcheck bench mine --out bench/v0-candidates --bot both \
  --since 2026-01-01 --limit 60 --logs
```

The miner searches GitHub for closed single-package bump PRs in Python
projects, keeps only PRs whose before/after lockfile parser output matches a
single bumped package named in the title, and records the CI outcome for the
head commit. It writes three files to `--out`:

- `candidates.jsonl`: one candidate per line, sorted by `case_id`. Every
  candidate has a null `label` field, ready for hand labelling.
- `excluded.jsonl`: every inspected PR that was rejected, with a reason:
  `group_bump`, `not_single_package`, `no_supported_lockfile`,
  `parse_mismatch`, `no_ci`, `ci_not_attributable`,
  `closed_without_ci_signal`, or `other`.
- `summary.json`: counts per label suggestion, bot, split and exclusion
  reason, plus distinct repos and API calls made.

CI outcomes are noisy suggestions, not labels. `OK_SUSPECT` means every
signal-bearing check passed on a merged PR, `BROKE_SUSPECT` means at least one
non-lint check failed, and `UNCLEAR` covers everything else (for example a
passing PR that was closed without merging). Cancelled, skipped, neutral and
stale conclusions are ignored; PRs with pending checks are excluded. Every
candidate still needs hand verification.

Responses are cached under `.bumpcheck-cache/github/` (override with
`--cache-dir`), and `--offline` replays a warm cache without touching the
network. `GITHUB_TOKEN` is optional but required for `--logs` and for usable
rate limits; it is never written to the cache or to any output file. Requests are
spaced at least 1 second apart (search at least 2), and rate-limit 403/429
responses are retried with the API's own retry hint (or a doubling backoff, up
to four retries) before failing with the API's message. Rerunning
into the same `--out` skips cases that are already recorded; `--limit` counts
those earlier candidates too.

The `dev`/`test` split is assigned deterministically per repository
(`sha256(repo)` first byte below 77 means `test`, about 30%), so reruns and
resumed runs keep the same split and no PR leaks between splits. Discovery walks
one-month windows in ascending order and processes each search page as it
arrives, so `--limit` stops pagination early; a window is split in half whenever
GitHub's 1000-result search cap is hit, and a single day with more than 1000
matches is still truncated by GitHub and cannot be paged further.

## Acceptance run

Check the API diff against known breaking releases listed in
`acceptance/pairs.json` (each entry names the package, both versions, the
expected breakages, and a source link). This fetches wheels from PyPI:

```
uv run python scripts/apidiff_acceptance.py run [--pairs PATH] [--cache-dir PATH] [--only PACKAGE]
```

Each expectation is reported as `HIT` or `MISS`; a pair whose comparison is
impossible is reported as `NO_API_DIFF` with its reason, and any other failure
as `ERROR` without aborting the run. Results are written to
`acceptance/report.md` and `acceptance/report.json`.

