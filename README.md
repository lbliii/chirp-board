# Chirp Board

Focused, keyboard-first team issue tracking powered by Chirp and Workspace Core.

The bounded product, schema, permission, realtime, and repository contract is recorded in
[Chirp issue #763](https://github.com/lbliii/chirp/issues/763). Durable Board-domain
implementation is tracked by [issue #771](https://github.com/lbliii/chirp/issues/771).
Board and list interaction surfaces are tracked by
[issue #768](https://github.com/lbliii/chirp/issues/768).

Chirp Board is under active development and is not yet a published Railway product.

## What #768 ships

Server-rendered project and issue workflow on top of the released Board domain:

- workspace home, project landing, board view, list view, issue detail
- create / edit / move / assign / label / comment / subscribe / archive flows
- bookmarkable search, typed filters, pagination, and saved views
- normal-form controls for every mutation, with HTMX progressive enhancement
- keyboard hooks (`/`, `c`, `j`/`k`, Enter, `m`) without a parallel SPA or JSON API

## Domain boundary

Workspace Core owns local identity, workspaces, memberships, and baseline roles. Board owns only
the `board_*` tables and compiles Core's Viewer, Member, Admin, and Owner roles into exact Board
permissions. Every repository operation requires a request-scoped `WorkspacePrincipal`; every
read, write, foreign key, and unique constraint carries the workspace boundary.

## Local development

```console
uv sync --locked
WORKSPACE_SETUP_TOKEN=local-board-setup-token-24chars \
CHIRP_SECRET_KEY=local-board-secret-key-with-32-bytes!! \
uv run python app.py
```

Development defaults to SQLite. Visit `/setup` once to claim the first workspace, then use board
and list views under `/workspaces/{id}/projects/{project_id}/board`.

Acceptance gates:

```console
uv run ruff check .
uv run ruff format . --check
uv run ty check src/chirp_board board_app app.py tests
uv run pytest --cov=chirp_board --cov=board_app --cov-fail-under=80 -q
uv build
```

Behavioral coverage for the interaction surface uses `@pytest.mark.issue(768)`. Domain coverage
remains `@pytest.mark.issue(771)`.

## Migrations

Run Workspace Core migrations before Board migrations against the same connected Chirp database.
The app entry point applies both on startup.

## Realtime and Railway

Live activity fan-out and Railway marketplace publication remain #766. V1 stays one Chirp process,
one PostgreSQL database, and no Redis or worker.
