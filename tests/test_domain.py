"""Issue #771 acceptance for the durable Board domain."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from chirp.data import Database, DatabaseConnectionError, migrate
from chirp_workspace_core import (
    AuthorizationError,
    ConflictError,
    DatabaseUnavailableError,
    NotFoundError,
    Role,
    SchemaMismatchError,
    TenantMismatchError,
    ValidationError,
    WorkspacePrincipal,
    WorkspaceRepository,
)
from chirp_workspace_core.migrations import migration_directory as core_migration_directory
from conftest import PASSWORD, BoardHarness

from chirp_board import (
    BoardRepository,
    ColorToken,
    Priority,
    StatusCategory,
    migration_directory,
    seed_demo,
)

pytestmark = pytest.mark.issue(771)


async def _project(harness: BoardHarness, *, key: str = "ENG"):
    return await harness.board.create_project(
        harness.owner,
        key=key,
        name="Engineering",
        description="Durable issue tracking",
    )


async def test_packaged_migration_replays_and_domain_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    database = Database(f"sqlite:///{path}")
    await database.connect()
    await migrate(database, core_migration_directory())
    first = await migrate(database, migration_directory())
    replay = await migrate(database, migration_directory())
    assert len(first.applied) == 1
    assert replay.applied == []
    identities = WorkspaceRepository(database)
    result = await identities.bootstrap_first_owner(
        expected_setup_token="token",
        supplied_setup_token="token",
        email="owner@example.test",
        display_name="Owner",
        password=PASSWORD,
        workspace_slug="primary",
        workspace_name="Primary",
    )
    principal = await identities.principal(
        user_id=result.user.id,
        workspace_id=result.workspace.id,
    )
    board = BoardRepository(database)
    project, _workflow, _statuses = await board.create_project(
        principal, key="OPS", name="Operations"
    )
    await database.disconnect()

    restarted = Database(f"sqlite:///{path}")
    await restarted.connect()
    try:
        assert (await migrate(restarted, migration_directory())).applied == []
        loaded = await BoardRepository(restarted).project(principal, str(project.id))
        assert loaded == project
    finally:
        await restarted.disconnect()


async def test_project_workflow_configuration_and_archive_rules(harness: BoardHarness) -> None:
    project, workflow, statuses = await _project(harness)
    assert [item.category for item in statuses] == [
        StatusCategory.BACKLOG,
        StatusCategory.UNSTARTED,
        StatusCategory.STARTED,
        StatusCategory.COMPLETED,
    ]
    project = await harness.board.update_project(
        harness.owner,
        str(project.id),
        expected_revision=project.revision,
        name="Platform Engineering",
        description="Updated",
    )
    assert project.revision == 2

    added = await harness.board.add_status(
        harness.owner,
        project_id=str(project.id),
        expected_workflow_revision=workflow.revision,
        name="Canceled",
        category=StatusCategory.CANCELED,
        color_token=ColorToken.RED,
    )
    updated = await harness.board.update_status(
        harness.owner,
        project_id=str(project.id),
        status_id=str(added.id),
        expected_workflow_revision=2,
        name="Won't do",
        category=StatusCategory.CANCELED,
        color_token=ColorToken.GRAY,
    )
    assert updated.name == "Won't do"
    archived = await harness.board.set_status_archived(
        harness.owner,
        project_id=str(project.id),
        status_id=str(updated.id),
        expected_workflow_revision=3,
        archived=True,
    )
    assert archived.archived_at is not None
    restored = await harness.board.set_status_archived(
        harness.owner,
        project_id=str(project.id),
        status_id=str(updated.id),
        expected_workflow_revision=4,
        archived=False,
    )
    assert restored.archived_at is None

    _loaded_workflow, current = await harness.board.workflow(harness.owner, str(project.id))
    reordered = tuple(str(item.id) for item in reversed(current) if item.archived_at is None)
    await harness.board.reorder_statuses(
        harness.owner,
        project_id=str(project.id),
        expected_workflow_revision=5,
        ordered_status_ids=reordered,
    )
    _loaded_workflow, current = await harness.board.workflow(harness.owner, str(project.id))
    assert [str(item.id) for item in current if item.archived_at is None] == list(reordered)

    issue = await harness.board.create_issue(
        harness.owner,
        project_id=str(project.id),
        title="Blocks status archival",
        status_id=str(current[0].id),
    )
    with pytest.raises(ConflictError, match="containing issues"):
        await harness.board.set_status_archived(
            harness.owner,
            project_id=str(project.id),
            status_id=str(issue.status_id),
            expected_workflow_revision=6,
            archived=True,
        )


async def test_issue_lifecycle_filters_comments_views_and_activity(harness: BoardHarness) -> None:
    project, _workflow, statuses = await _project(harness)
    member = await harness.add_principal(Role.MEMBER, prefix="member")
    label = await harness.board.create_label(
        harness.owner,
        project_id=str(project.id),
        name="Security",
        color_token=ColorToken.RED,
    )
    issue = await harness.board.create_issue(
        member,
        project_id=str(project.id),
        title="Tenant-safe filters",
        description_source="Exercise search and durable mutations.",
        priority=Priority.HIGH,
        status_id=str(statuses[0].id),
    )
    issue = await harness.board.replace_assignees(
        member,
        str(issue.id),
        expected_revision=issue.revision,
        user_ids=(str(member.user.id), str(member.user.id)),
    )
    issue = await harness.board.replace_labels(
        member,
        str(issue.id),
        expected_revision=issue.revision,
        label_ids=(str(label.id), str(label.id)),
    )
    issue = await harness.board.update_issue(
        member,
        str(issue.id),
        expected_revision=issue.revision,
        title="Tenant-safe issue filters",
        description_source=issue.description_source,
        priority=Priority.URGENT,
    )
    issue = await harness.board.move_issue(
        member,
        str(issue.id),
        expected_revision=issue.revision,
        status_id=str(statuses[1].id),
    )
    assert issue.assignee_user_ids == (str(member.user.id),)
    assert [item.id for item in issue.labels] == [label.id]

    page = await harness.board.list_issues(
        harness.owner,
        project_id=str(project.id),
        query="DURABLE MUTATIONS",
        status_ids=(str(statuses[1].id),),
        priorities=(Priority.URGENT,),
        assignee_ids=(str(member.user.id), str(member.user.id)),
        label_ids=(str(label.id), str(label.id)),
    )
    assert [item.id for item in page.items] == [issue.id]

    comment = await harness.board.add_comment(
        member,
        str(issue.id),
        body_source="A durable comment",
    )
    assert await harness.board.set_subscription(member, str(issue.id), subscribed=True)
    assert not await harness.board.set_subscription(member, str(issue.id), subscribed=False)
    redacted = await harness.board.redact_comment(
        member,
        str(comment.id),
        expected_revision=comment.revision,
    )
    assert redacted.body_source == ""
    assert (await harness.board.comments(harness.owner, str(issue.id)))[0] == redacted

    personal = await harness.board.save_view(
        member,
        project_id=str(project.id),
        name="My urgent work",
        filters={"priorities": ["urgent"], "assignee_ids": [str(member.user.id)]},
    )
    shared = await harness.board.save_view(
        harness.owner,
        project_id=str(project.id),
        name="Team urgent work",
        filters={"priorities": ["urgent"], "group_by": "status"},
        shared=True,
    )
    assert {
        item.id for item in await harness.board.saved_views(member, project_id=str(project.id))
    } == {
        personal.id,
        shared.id,
    }
    with pytest.raises(ConflictError):
        await harness.board.save_view(
            harness.owner,
            project_id=str(project.id),
            name="Team urgent work",
            filters={},
            shared=True,
        )
    actions = {
        item.action
        for item in await harness.board.activity(
            harness.owner, project_id=str(project.id), issue_id=str(issue.id)
        )
    }
    assert {"issue.created", "issue.moved", "comment.created", "comment.redacted"} <= actions


async def test_roles_tenant_boundaries_and_principal_integrity(harness: BoardHarness) -> None:
    project, _workflow, statuses = await _project(harness)
    viewer = await harness.add_principal(Role.VIEWER, prefix="viewer")
    member = await harness.add_principal(Role.MEMBER, prefix="member-two")
    assert await harness.board.project(viewer, str(project.id)) == project
    with pytest.raises(AuthorizationError):
        await harness.board.create_issue(
            viewer,
            project_id=str(project.id),
            title="Viewer write",
            status_id=str(statuses[0].id),
        )
    with pytest.raises(AuthorizationError):
        await harness.board.create_project(member, key="NO", name="Not allowed")

    second_owner = await harness.identities.create_user(
        email="second@example.test",
        display_name="Second",
        password=PASSWORD,
    )
    second_workspace, _membership = await harness.identities.create_workspace(
        owner_user_id=second_owner.id,
        slug="second",
        name="Second Workspace",
    )
    second_principal = await harness.identities.principal(
        user_id=second_owner.id,
        workspace_id=second_workspace.id,
    )
    with pytest.raises(NotFoundError):
        await harness.board.project(second_principal, str(project.id))
    with pytest.raises(NotFoundError):
        await harness.board.create_issue(
            second_principal,
            project_id=str(project.id),
            title="Cross-tenant write",
            status_id=str(statuses[0].id),
        )

    tampered = WorkspacePrincipal(
        harness.owner.user,
        second_principal.workspace,
        harness.owner.membership,
        harness.owner.permissions,
    )
    with pytest.raises(TenantMismatchError):
        await harness.board.projects(tampered)


async def test_atomic_numbers_pagination_conflicts_and_archive(harness: BoardHarness) -> None:
    project, _workflow, statuses = await _project(harness)

    async def create(index: int):
        return await harness.board.create_issue(
            harness.owner,
            project_id=str(project.id),
            title=f"Concurrent issue {index:02d}",
            status_id=str(statuses[0].id),
        )

    issues = await asyncio.gather(*(create(index) for index in range(12)))
    assert sorted(item.number for item in issues) == list(range(1, 13))
    page_one = await harness.board.list_issues(harness.owner, project_id=str(project.id), limit=5)
    assert len(page_one.items) == 5
    assert page_one.next_cursor
    page_two = await harness.board.list_issues(
        harness.owner,
        project_id=str(project.id),
        limit=5,
        cursor=page_one.next_cursor,
    )
    assert len({item.id for item in page_one.items + page_two.items}) == 10
    with pytest.raises(ValidationError, match="cursor"):
        await harness.board.list_issues(
            harness.owner, project_id=str(project.id), cursor="malformed"
        )

    target = issues[0]
    changed = await harness.board.update_issue(
        harness.owner,
        str(target.id),
        expected_revision=target.revision,
        title="Changed",
        description_source=target.description_source,
        priority=target.priority,
    )
    with pytest.raises(ConflictError):
        await harness.board.update_issue(
            harness.owner,
            str(target.id),
            expected_revision=target.revision,
            title="Stale",
            description_source="",
            priority=Priority.NONE,
        )
    archived = await harness.board.set_issue_archived(
        harness.owner,
        str(changed.id),
        expected_revision=changed.revision,
        archived=True,
    )
    assert archived.archived_at
    with pytest.raises(ConflictError):
        await harness.board.add_comment(harness.owner, str(archived.id), body_source="No")
    restored = await harness.board.set_issue_archived(
        harness.owner,
        str(archived.id),
        expected_revision=archived.revision,
        archived=False,
    )
    assert restored.archived_at is None


async def test_concurrent_moves_keep_distinct_deterministic_ranks(harness: BoardHarness) -> None:
    project, _workflow, statuses = await _project(harness)
    first, second, anchor = await asyncio.gather(
        *(
            harness.board.create_issue(
                harness.owner,
                project_id=str(project.id),
                title=title,
                status_id=str(statuses[0].id),
            )
            for title in ("First mover", "Second mover", "Destination anchor")
        )
    )
    anchor = await harness.board.move_issue(
        harness.owner,
        str(anchor.id),
        expected_revision=anchor.revision,
        status_id=str(statuses[1].id),
    )
    moved = await asyncio.gather(
        harness.board.move_issue(
            harness.owner,
            str(first.id),
            expected_revision=first.revision,
            status_id=str(statuses[1].id),
            before_issue_id=str(anchor.id),
        ),
        harness.board.move_issue(
            harness.owner,
            str(second.id),
            expected_revision=second.revision,
            status_id=str(statuses[1].id),
            before_issue_id=str(anchor.id),
        ),
    )
    assert len({item.rank for item in (*moved, anchor)}) == 3
    with pytest.raises(ValidationError, match="before itself"):
        await harness.board.move_issue(
            harness.owner,
            str(moved[0].id),
            expected_revision=moved[0].revision,
            status_id=str(statuses[1].id),
            before_issue_id=str(moved[0].id),
        )


async def test_demo_seed_is_idempotent_and_validates_input(harness: BoardHarness) -> None:
    first = await seed_demo(harness.board, harness.owner)
    second = await seed_demo(harness.board, harness.owner)
    assert second == first
    assert len(first.issues) == 2
    with pytest.raises(ValidationError):
        await harness.board.save_view(
            harness.owner,
            project_id=str(first.project.id),
            name="Invalid",
            filters={"unknown": True},
        )
    with pytest.raises(ValidationError):
        await harness.board.create_project(harness.owner, key="bad-key", name="Bad")
    with pytest.raises(ConflictError):
        await harness.board.create_project(harness.owner, key="DEMO", name="Duplicate")
    with pytest.raises(ValidationError):
        await harness.board.add_comment(harness.owner, str(first.issues[0].id), body_source="")


async def test_additive_schema_update_remains_rollback_compatible(harness: BoardHarness) -> None:
    project, _workflow, _statuses = await _project(harness)
    await harness.database.execute("ALTER TABLE board_projects ADD COLUMN future_note TEXT")
    loaded = await harness.board.project(harness.owner, str(project.id))
    assert loaded == project


async def test_unmigrated_database_fails_with_actionable_schema_error(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'unmigrated.db'}")
    await database.connect()
    await migrate(database, core_migration_directory())
    identities = WorkspaceRepository(database)
    result = await identities.bootstrap_first_owner(
        expected_setup_token="token",
        supplied_setup_token="token",
        email="owner@example.test",
        display_name="Owner",
        password=PASSWORD,
        workspace_slug="primary",
        workspace_name="Primary",
    )
    owner = await identities.principal(
        user_id=result.user.id,
        workspace_id=result.workspace.id,
    )
    try:
        with pytest.raises(SchemaMismatchError, match="packaged Board migrations"):
            await BoardRepository(database).projects(owner)
    finally:
        await database.disconnect()


async def test_disconnected_database_fails_with_actionable_error(
    harness: BoardHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(*args: object, **kwargs: object) -> None:
        raise DatabaseConnectionError("database is offline")

    monkeypatch.setattr(Database, "fetch", unavailable)
    with pytest.raises(DatabaseUnavailableError, match="Board database is unavailable"):
        await harness.board.projects(harness.owner)
