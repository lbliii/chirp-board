# Chirp Board Agent Guide

Chirp Board is the focused, independently deployable issue-tracking product defined by
Chirp decision issue #763.

## Invariants

- Workspace Core owns tenancy, local identity, memberships, and baseline roles.
- Every Board repository operation is scoped by an explicit `WorkspacePrincipal` and every
  Board-owned row includes `workspace_id`.
- Server-rendered Chirp pages, typed returns, named blocks, normal forms, HTMX, and bounded SSE
  remain the interaction architecture; never add a parallel SPA or JSON application API.
- Issues and activity retain stable opaque identities. User-facing issue keys do not depend on
  mutable titles or workflow names.
- Board owns only `board_*` tables and packaged migrations. It never imports another product's
  internal tables or templates.

## Stop and ask

- A change alters the #763 product/schema/permission/realtime contract.
- A change requires a Chirp or Workspace Core public API change, a new service, or a new required
  runtime dependency.
- A migration deletes data, rewrites stable identity, or breaks the previous application release.

## Done

- Run Ruff, Ruff format, Ty, pytest with at least 80% coverage, and package build.
- Mark behavioral acceptance with the originating Chirp issue number.
- Update README, changelog, migrations, and compatibility guidance together.
