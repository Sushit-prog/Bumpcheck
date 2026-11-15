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

