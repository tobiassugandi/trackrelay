# Continuous integration

Run the lint and default test checks locally:

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
require PostgreSQL. A separate `PostgreSQL integration tests` job runs those
tests against a fresh database, as described below. Two further jobs check
Terraform and build and smoke-test the container images. CI needs no AWS
credentials and does not deploy anything or run live AWS experiments.

## How GitHub runs it

[The workflow](../.github/workflows/ci.yml) connects GitHub events to `make ci`:

1. Opening or updating a pull request targeting `main` starts the workflow.
2. GitHub gives each job its own fresh Ubuntu runner (a temporary VM). The four
   jobs can run in parallel and report independent results.
3. The `Lint and tests` job checks out the code, installs uv, and runs `make ci`.
   The PostgreSQL job installs dependencies, migrates a fresh database, and
   runs `make test-integration`. The Terraform job runs `make infra-init` and
   `make infra-check`; the container job runs the three image smoke targets.
   For pull requests,
   the default checkout tests GitHub's proposed merge with the base branch.
4. A command returning a nonzero exit code fails the job and produces a red
   check. Expand the failed step in the Actions log to see the error.
5. Merging the pull request causes a push to `main`, which runs the checks again.

Pushing a feature branch without an open pull request does not trigger this
workflow. Once its pull request is open, subsequent pushes rerun the checks.
The workflow cancels an older run for the same ref when a new run starts and
limits each job to ten minutes (twenty for container builds). It has read-only
repository permissions.
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
branch protection for `main` after the checks have run, and select
`Lint and tests`, `PostgreSQL integration tests`, `Terraform checks`, and
`Container builds and smoke tests` as required status checks.
Availability depends on your repository and plan.

## PostgreSQL integration tests

The `integration` job starts a disposable `postgres:17-alpine` service, matching
the PostgreSQL version in `compose.yaml`. GitHub waits for its `pg_isready`
health check before running the job's steps. The runner connects through
`127.0.0.1:5432`; `TRACKRELAY_DATABASE_URL` points both Alembic and the tests at
that service. The credentials in the workflow are only for this temporary test
database, so repository secrets are unnecessary.

The job runs `make sync`, then `make migrate` to apply all Alembic migrations
to the empty database, then `make test-integration`. The test target overrides
pytest's default exclusion and selects only tests marked `integration`. These
exercise persistence, concurrent deduplication, API flows, correctness scenarios,
and reconciliation against real PostgreSQL. API and downstream simulator clients
run inside the tests, so no separate web servers or AWS services are needed.

The reset-contract test requires its own database, `trackrelay_reset_contract`.
CI creates that database and sets `TRACKRELAY_RESET_TEST_DATABASE_URL`; the test
fixture creates its schema. This isolates its destructive reset operations from
the migrated application database. Without that variable, this test skips.

GitHub removes the service when the job ends. Each run starts with a new
database. A failure in `Apply database migrations` points to setup or schema
creation; a failure in `Run integration tests` points to a test or fixture.
The separate check keeps those failures distinct from lint and default tests.

To reproduce locally using the development database (Docker must be running):

```shell
export TRACKRELAY_ENVIRONMENT=test
export TRACKRELAY_DATABASE_URL=postgresql+psycopg://trackrelay:trackrelay@127.0.0.1:5433/trackrelay
make sync
make db-up
make migrate
docker compose exec -T postgres createdb -U trackrelay trackrelay_reset_contract
export TRACKRELAY_RESET_TEST_DATABASE_URL=postgresql+psycopg://trackrelay:trackrelay@127.0.0.1:5433/trackrelay_reset_contract
make test-integration
make db-down
```

These commands use the default Compose credentials and port from `.env.example`;
adjust the URL if you have customized them. Use a development database: the tests
insert and delete fixture records. Local Compose retains its database volume
after `make db-down`, whereas GitHub's service is fresh for every job. Run the
`createdb` command with a fresh database: the reset test requires no existing
tables. For a repeat run, remove only this disposable test database with
`docker compose exec -T postgres dropdb -U trackrelay trackrelay_reset_contract`
before recreating it (while PostgreSQL is running).

## Terraform checks

CI installs the version in `.terraform-version`, then runs:

```shell
TF_CLI_ARGS_init=-lockfile=readonly make infra-init
make infra-check
```

`infra-init` downloads the providers with the backend disabled. CI uses the
committed `.terraform.lock.hcl` without updating it. `infra-check` checks
formatting, validates the configuration, and runs the Terraform test suite.
The current tests use a mocked AWS provider and `command = plan`: they exercise
infrastructure assertions without creating AWS resources or needing credentials.
They do not establish that a real AWS deployment will succeed.

To reproduce locally, install the Terraform version in `.terraform-version`
and run the commands above from the repository root. When changing provider
requirements, update and commit the provider lockfile intentionally; CI should
not silently select new provider versions.

Keep provider checksums for both the ARM Mac development environment and the
Linux amd64 CI runner in the lockfile. After changing provider requirements,
generate and commit those checksums with:

```shell
terraform -chdir=infra/terraform providers lock \
  -platform=darwin_arm64 -platform=linux_amd64
```

This verifies packages against the publisher's signed checksums and records
platform-specific content hashes. With read-only initialization, a lockfile
containing only the Mac content hash can pass local checks but fail validation
of the unpacked Linux provider. Keep read-only initialization in CI and update
the lockfile explicitly rather than allowing CI to rewrite it.

## Container builds and smoke tests

The container job runs these existing targets in separate steps:

```shell
make image-api-smoke
make image-worker-smoke
make image-simulator-smoke
```

Each target builds its Dockerfile stage and runs its smoke script. API and
simulator checks wait for container health, call `/health/live`, and verify
the process runs as UID 10001. The worker check verifies its entrypoint,
imports, non-root user, and rejection of missing SQS configuration. It does
not connect to SQS or process real messages. The HTTP checks cover liveness,
not database readiness or end-to-end delivery.

All three steps share one runner so Docker can reuse common build layers.
Images are built for the runner's Linux amd64 architecture and are not pushed
to a registry. Smoke scripts remove their temporary containers. Local runs
require Docker and build for your machine's default architecture; use
`DOCKER_DEFAULT_PLATFORM=linux/amd64 make images-smoke` to match CI on an ARM Mac.
`make images-smoke` runs all three targets together. `make ci` remains the
lightweight lint and default-test command; Terraform and Docker have their own
jobs and local commands.

References: [uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)
and [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
See also [PostgreSQL service containers](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers).
Terraform installation uses [HashiCorp's setup-terraform action](https://github.com/hashicorp/setup-terraform).
