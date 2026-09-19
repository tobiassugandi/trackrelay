# Continuous integration

Run the same checks locally that GitHub runs on a pull request:

```shell
make ci
```

You need Make and [uv](https://docs.astral.sh/uv/getting-started/installation/).
CI pins uv to 0.12.13; use that version locally for the closest match.
The target runs these existing Make targets in order, stopping on a failure:

| Target | Purpose |
| --- | --- |
| `sync` | Install Python 3.12 and dependencies from `uv.lock` using `uv sync --locked --python 3.12`. A stale lockfile fails instead of being silently updated. |
| `lint` | Run Ruff to catch code-quality issues. |
| `test` | Run the default pytest suite, including unit and contract tests. |

The default pytest configuration excludes tests marked `integration`, which
require PostgreSQL. This first CI does not run database integration tests,
container smoke tests, Terraform validation, or live AWS experiments. It needs
no AWS credentials and does not deploy anything. Passing this check establishes
that lint and the default tests pass; it does not prove a deployment works.

## How GitHub runs it

[The workflow](../.github/workflows/ci.yml) connects GitHub events to `make ci`:

1. Opening or updating a pull request targeting `main` starts the workflow.
2. GitHub gives the `Lint and tests` job a fresh Ubuntu runner (a temporary VM).
3. Steps check out the code, install uv, and run `make ci`. For pull requests,
   the default checkout tests GitHub's proposed merge with the base branch.
4. A command returning a nonzero exit code fails the job and produces a red
   check. Expand the failed step in the Actions log to see the error.
5. Merging the pull request causes a push to `main`, which runs the checks again.

Pushing a feature branch without an open pull request does not trigger this
workflow. Once its pull request is open, subsequent pushes rerun the checks.
The workflow cancels an older run for the same ref when a new run starts and
limits each job to ten minutes. It has read-only repository permissions.
Actions are pinned to commit SHAs, with version comments for readability; uv
is also pinned. The dependency download cache improves speed, while `uv.lock`
determines the dependency versions.

The Makefile owns the checks, and the workflow owns when and where they run.
This gives developers one command to reproduce a failed check locally.

## Your first pull request

From the `codex/ci` branch, review, test, commit, and push:

```shell
git diff
git status --short
make ci
git add Makefile docs/ci.md .github/workflows/ci.yml
git diff --cached
git commit -m "Add basic GitHub Actions CI"
git push -u origin codex/ci
```

On GitHub, open a pull request with **base: main** and **compare: codex/ci**.
Read the Files changed tab, then watch the `Lint and tests` check. Open its
details to see the runner executing the same command you ran locally. Fix any
failure locally, commit, and push again to update the same pull request. Merge
when you are happy with the changes and the check passes.

For a learning exercise before merging, temporarily add `assert False` inside
an existing test, commit and push, and inspect the failed check. Remove that
line in another commit and push to see the check turn green again.

CI reporting and merge enforcement are separate: this workflow reports a
result. To require a passing result before merging, configure a ruleset or
branch protection for `main` after the check has run, and select `Lint and tests`
as a required status check. Availability depends on your repository and plan.

## A useful next increment

Add a separate job with a disposable PostgreSQL service and run
`make test-integration`. Keeping it separate gives database failures their own
logs and status. Container builds and Terraform checks can follow as additional
jobs when those checks are useful for your development workflow.

References: [uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)
and [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
