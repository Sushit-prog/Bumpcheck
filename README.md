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
  `group_bump`, `not_single_package`, `non_python_package`,
  `no_supported_lockfile`, `parse_mismatch`, `no_ci`,
  `ci_not_attributable`, `closed_without_ci_signal`, `noise_repo`,
  `repo_cap`, or `other`. Each record keeps the PR title so rejections can be
  audited, and a short `detail` (at most 200 characters) explains
  `parse_mismatch` (title vs. lockfile bumps), pending/failing check names for
  `other`/`ci_not_attributable`, and exception type/message for `other`
  (URL query strings stripped).
- `summary.json`: counts per label suggestion, bot, split and exclusion
  reason, plus distinct repos, API calls (`api_calls`), search requests
  (`search_calls`), and the `retried`/`recovered` counters from
  `--retry-excluded`.

Single-package bumps carry `autoclosed: true` when the pull request title was
decorated with `- autoclosed`. Renovate titles are stripped of trailing
`- autoclosed`, `[SECURITY]`-style tags and `(major)`-style parenthesised
decorations (repeatedly, never down to an empty title) before parsing.
Parenthesised text inside the title, such as the `(fork)` in
`Update dependency foo (fork) to v1`, is left alone. Dependabot's
`Bump X from A to B in the <group> group across N directories` form is treated
as a single-package bump, not a group bump.

Discovery walks fixed `--window-days` windows (default 7) from `--since` to
`--until`, ordered deterministically by the SHA-256 of each window's start
date so neighbouring requests are not adjacent dates. Windows are visited
lazily and sequentially: each window is searched for each selected bot
(Dependabot first), only the first `--pages-per-window` pages are read
(default 1, max 10), and items are yielded sorted by `(created_at, number)`
before moving on. A window whose `total_count` reaches GitHub's 1000-result
cap is not split. The next window is only fetched when another item is
needed, and reaching `--limit` stops all further search requests.

Titles classified as single-package bumps whose package name contains `/` or
starts with `@` (GitHub Actions/Docker references and npm scopes) are excluded
as `non_python_package` before any per-PR request. `--dependabot-label TEXT`
(default `python`; pass `""` to disable) appends `label:TEXT` to Dependabot
search queries only; Renovate queries are unchanged.

Repos matching a built-in noise list (for example repo names starting with
`skills-`, plus `secure-code-game`, `codeql`, `copilot-codespaces`, `opensafely`)
are recorded as `noise_repo` from search results alone, and a repo that already
contributed `--max-per-repo` candidates (default 3) is recorded as `repo_cap`.
Both checks run before any PR request, so they cost no API budget. Passing one
or more `--skip-repo-pattern` regexes (case-insensitive) replaces the built-in
list. A pattern that starts with a literal `^` and contains no `/` is matched
against the bare repo name, so `^skills-` skips `someuser/skills-introduction`
but keeps `skills-org/real-project`; `^acme` likewise excludes `x/acme-tools`
but not `acme/widget`. Every other pattern is matched against the full
`owner/name`, so `opensafely` still hits the owner and `^acme/` targets an
owner explicitly. Only a leading literal `^` counts: an inline-flag form such
as `(?i)^skills-` is treated as unanchored and matched against `owner/name`.
`--ci-status {any,failure,success}` adds GitHub's `status:` qualifier to the
search and is copied into each candidate's `sampled_by` field.

`--retry-excluded REASON` (repeatable) re-processes stored `excluded.jsonl`
entries with that reason in a separate pass before discovery, even when
`--limit` is already met. Retried recoveries can therefore push the kept count
above `--limit`; discovery only runs when the kept count is still below it.
Each selected entry is handled in sorted `case_id` order directly from its
stored `repo` and `pr_number`: a stored title is re-classified without a
request and the PR is fetched only if it now looks like a single-package bump,
while a legacy entry without a title first fetches `/pulls/{n}` once to read
the title and author login (the fetched title is stored even if the entry stays
excluded). Recovered entries go through the normal per-PR pipeline and move to
`candidates.jsonl`; anything that fails again is re-excluded with its new
reason and `detail`. The same noise-repo patterns and `--max-per-repo` cap
apply, and progress lines are prefixed `[retry]`. Entries not selected by
`--retry-excluded` are untouched.

CI outcomes are noisy suggestions, not labels. `OK_SUSPECT` means every
signal-bearing check passed on a merged PR, `BROKE_SUSPECT` means at least one
non-lint check failed, and `UNCLEAR` covers everything else (for example a
passing PR that was closed without merging). For failing checks the same check
is compared against the base commit (`base_ci_checks`, `failure_vs_base`):
a `regression` (passed at base) or a check with `no_base_run` keeps
`BROKE_SUSPECT`, while failures that were already `preexisting` at base are
downgraded to `UNCLEAR`. Cancelled, skipped, neutral and
stale conclusions are ignored; PRs with pending checks are excluded. Every
candidate still needs hand verification.

With `--logs`, each failing check yields a `failure_excerpts` entry whose
`log_status` is `matched` (package-specific exception lines were found),
`fetched_no_match`, or `unavailable` (no job id, missing token, or the log
could not be fetched). For `BROKE_SUSPECT` merged PRs, `later_fix_commits`
lists commits after the merge that touch the lockfile or mention the package;
the PR's own merge commit, messages that merge this PR (`#<number>`), and
commits dated at or before the merge/close time are filtered out.

Responses are cached under `.bumpcheck-cache/github/` (override with
`--cache-dir`), and `--offline` replays a warm cache without touching the
network. `GITHUB_TOKEN` is optional but required for `--logs` and for usable
rate limits; it is never written to the cache or to any output file. Requests are
spaced at least 1 second apart (search at least 3), transient network errors
are retried up to four times with jittered exponential backoff, and
rate-limit 403/429 responses are retried with the API's own retry hint (or a
doubling backoff, up to four retries) before failing with the API's message.
Rerunning
into the same `--out` skips cases that are already recorded; `--limit` counts
those earlier candidates too. Each processed PR prints one progress line to
stderr, and both JSONL files are rewritten atomically (temp file + replace)
after every case, so pressing Ctrl-C saves what has been mined, writes
`summary.json`, and exits with code 130; a rerun resumes from that state.

The `dev`/`test` split is assigned deterministically per repository
(`sha256(repo)` first byte below 77 means `test`, about 30%), so reruns and
resumed runs keep the same split and no PR leaks between splits. Discovery
builds one-month windows per bot (oldest first) and takes one PR from each
window in round-robin order, so results spread across the whole date range
instead of exhausting the oldest month first; each search page is still
fetched lazily, so `--limit` stops early. A window is split in half whenever
GitHub's 1000-result search cap is hit, and a single day with more than 1000
matches is still truncated by GitHub and cannot be paged further.

### Labeling rules

Hand-label mined candidates in a store separate from `candidates.jsonl`:

```
uv run bumpcheck bench label --dir bench/v0-candidates
uv run bumpcheck bench label-stats --dir bench/v0-candidates
uv run bumpcheck bench agreement --dir bench/v0-candidates
```

`labels.jsonl` holds one JSON object per `(case_id, round)`, rewritten
atomically after every label. `BROKE` requires a `break_category`, an
`evidence_kind`, and non-empty evidence tying the failure to the **bumped
package**: an error naming its symbols (`log_names_package`), an install error
for it (`install_error`), a maintainer fix in a later commit
(`maintainer_fix`), or a local reproduction (`reproduced_locally`). A failing
check alone is never enough. A failure already present on the base commit is
`EXCLUDE` with `preexisting_failure`, not `BROKE`. `OK` is only allowed for a
merged PR whose `ci_summary` is `all_passed`; closed-unmerged and `autoclosed`
PRs can never be `OK` and are normally `EXCLUDE` (`superseded`) or `BROKE` with
evidence. `EXCLUDE` always needs a reason and evidence.

The miner's `label_suggestion` is hidden by default so the reviewer is not
anchored on its judgement (`--show-suggestion` reveals it). Hiding it mitigates
anchoring but does not remove it: check conclusions are still displayed and can
influence the decision. Use `bench label --round 2` at least a week later to
re-label a random 20% of the round-1 `BROKE`/`OK` cases (`--sample 0.2 --seed
42`), optionally restricted with `--round1-label BROKE`, then report raw
agreement and Cohen's kappa from `bench agreement`. `--only` filters by miner
suggestion, and repeatable `--also-dir DIR` skips case_ids already labeled for
that round in another store. Labels are never saved for a case already labeled
in the selected round; re-labeling the same `(case_id, round)` replaces it.

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

