# Benchmark report (split: all)

> n is small; do not quote these numbers publicly

Definitions: false-safe rate = BROKE cases with verdict SAFE / all BROKE cases; SAFE precision = OK cases among SAFE verdicts; breakage recall = BROKE cases with REVIEW or LIKELY_BREAKING / all BROKE cases; LIKELY_BREAKING-only recall uses only LIKELY_BREAKING; UNKNOWN rate = UNKNOWN verdicts / all BROKE and OK cases. Every rate is shown as raw k/n with a 95% Wilson interval.

## Known gap: Python-version-support breaks

- No labeled BROKE python_version_support cases in this split.
- Worked example: `Apocrathia/NSL-Downloader#49`, expected BumpCheck result SAFE or UNKNOWN because Python-version-support breaks are not a rule.

## Headline metrics

| metric | bumpcheck | always_safe | semver_rule |
| --- | --- | --- | --- |
| false-safe rate | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| SAFE precision | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| breakage recall | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| LIKELY_BREAKING-only recall | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| UNKNOWN rate | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |

## UNKNOWN rate by label

| label | bumpcheck | always_safe | semver_rule |
| --- | --- | --- | --- |
| BROKE | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| OK | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |

## Errors and disputed labels

Reported separately (never counted as SAFE): 

Disputed cases excluded from metrics: none

## Rules

_No rules fired._

## Break categories

_No labeled cases._

## Slices by Sampling source

_No labeled cases._

## Slices by Split

_No labeled cases._

## Selection bias

Exclusion reason counts: closed_without_ci_signal=163, group_bump=90, no_ci=27, no_supported_lockfile=468, not_single_package=590, parse_mismatch=67, repo_cap=35

Sampled-by mix: any=100

Unbiased sampling had ~2% BROKE (roughly 1 in 50 candidates), so this enriched benchmark should not be read as a population rate. `parse_mismatch` and `not_single_package` exclusions remain undiagnosed. Labels are single-annotator round 1 unless round 2 exists.
