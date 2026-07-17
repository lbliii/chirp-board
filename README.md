# Chirp Board

Focused, keyboard-first team issue tracking powered by Chirp and Workspace Core.

The bounded product, schema, permission, realtime, and repository contract is recorded in
[Chirp issue #763](https://github.com/lbliii/chirp/issues/763). Durable Board-domain
implementation is tracked by [issue #771](https://github.com/lbliii/chirp/issues/771).

Chirp Board is under active development and is not yet a published Railway product.

## Domain boundary

Workspace Core owns local identity, workspaces, memberships, and baseline roles. Board owns only
the `board_*` tables and compiles Core's Viewer, Member, Admin, and Owner roles into exact Board
permissions. Every repository operation requires a request-scoped `WorkspacePrincipal`; every
read, write, foreign key, and unique constraint carries the workspace boundary.

The first domain release includes:

- one configurable workflow per project, with ordered and archivable statuses;
- immutable issue identity and atomic project-local issue numbers;
- priorities, assignments to current members, labels, comments, subscriptions, and typed views;
- optimistic revisions, deterministic issue ranks, cursor pagination, archive/restore, and
  immutable secret-safe activity;
- an idempotent `seed_demo()` helper for local evaluation.

It deliberately does not introduce a JSON application API, SPA state model, Redis, a worker, or a
second ownership model. Server-rendered product routes and realtime delivery arrive in later
issues; this package is the durable domain boundary they consume.

## Migrations

Run Workspace Core migrations before Board migrations against the same connected Chirp database:

```python
from chirp.data import migrate
from chirp_board import migration_directory as board_migrations
from chirp_workspace_core.migrations import migration_directory as core_migrations

await migrate(database, core_migrations())
await migrate(database, board_migrations())
```

Board reserves migration versions starting at `1001` so its packaged migrations do not collide
with Core's shared migration ledger. Applied migrations are immutable.

## Development

The committed dependency is the release range
`chirp-workspace-core>=0.1.0a2,<0.2`; Git, path, and editable dependencies are intentionally absent
from package metadata. Until that supplier release is available from the public package index, a
maintainer can test an exact tagged Core checkout locally without changing the consumer contract:

```console
uv venv --python 3.14
uv pip install -e /path/to/chirp-workspace-core-v0.1.0a2
uv pip install -e . --no-deps
uv pip install pytest pytest-asyncio pytest-cov ruff ty build
```

Then run the acceptance gates:

```console
.venv/bin/ruff check .
.venv/bin/ruff format . --check
.venv/bin/ty check src/chirp_board tests
.venv/bin/pytest --cov=chirp_board --cov-fail-under=80
.venv/bin/python -m build --no-isolation
```
