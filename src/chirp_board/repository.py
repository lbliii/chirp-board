"""Tenant-scoped durable domain repository for Chirp Board."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, TypeVar
from uuid import uuid7

from chirp.data import Database, DatabaseConnectionError, QueryError
from chirp_workspace_core import (
    ConflictError,
    DatabaseUnavailableError,
    NotFoundError,
    SchemaMismatchError,
    TenantMismatchError,
    ValidationError,
    WorkspacePrincipal,
)

from .models import (
    Activity,
    ActivityId,
    ColorToken,
    Comment,
    CommentId,
    Issue,
    IssueId,
    IssuePage,
    Label,
    LabelId,
    Priority,
    Project,
    ProjectId,
    SavedView,
    SavedViewId,
    StatusCategory,
    StatusId,
    Workflow,
    WorkflowId,
    WorkflowStatus,
)
from .permissions import BoardPermission, require_board_permission

_T = TypeVar("_T")
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
_FILTER_KEYS = frozenset(
    {
        "query",
        "status_ids",
        "priorities",
        "assignee_ids",
        "label_ids",
        "include_archived",
        "sort",
        "group_by",
    }
)
_FILTER_SORTS = frozenset({"updated_desc", "created_desc", "priority_desc", "rank"})
_FILTER_GROUPS = frozenset({"status", "priority", "assignee", "none"})
_SENSITIVE_METADATA = frozenset(
    {"password", "secret", "token", "cookie", "authorization", "credential", "private_key"}
)
_RANK_STEP = 1024


@dataclass(frozen=True, slots=True)
class _ProjectRow:
    id: str
    workspace_id: str
    key: str
    name: str
    description: str
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class _WorkflowRow:
    id: str
    workspace_id: str
    project_id: str
    name: str
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class _StatusRow:
    id: str
    workspace_id: str
    workflow_id: str
    name: str
    category: str
    color_token: str
    position: int
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class _LabelRow:
    id: str
    workspace_id: str
    project_id: str
    name: str
    color_token: str
    created_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class _IssueRow:
    id: str
    workspace_id: str
    project_id: str
    number: int
    title: str
    description_source: str
    priority: str
    status_id: str
    reporter_user_id: str
    rank: int
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class _CommentRow:
    id: str
    workspace_id: str
    issue_id: str
    author_user_id: str
    body_source: str
    revision: int
    created_at: str
    updated_at: str
    redacted_at: str | None


@dataclass(frozen=True, slots=True)
class _SavedViewRow:
    id: str
    workspace_id: str
    project_id: str
    owner_user_id: str | None
    name: str
    schema_version: int
    typed_filter_json: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class _ActivityRow:
    id: str
    workspace_id: str
    project_id: str
    issue_id: str | None
    actor_user_id: str
    action: str
    resource_kind: str
    resource_id: str
    safe_metadata_json: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class _IdRow:
    id: str


@dataclass(frozen=True, slots=True)
class _UserRow:
    user_id: str


@dataclass(frozen=True, slots=True)
class _CounterRow:
    next_number: int


@dataclass(frozen=True, slots=True)
class _RankRow:
    rank: int


class BoardRepository:
    """One transaction boundary for Board state, history, and authorization."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._db = database
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid7()))

    def _stamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("Board clock must return a timezone-aware datetime.")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _workspace(principal: WorkspacePrincipal) -> str:
        workspace_id = str(principal.workspace.id)
        if str(principal.membership.workspace_id) != workspace_id:
            raise TenantMismatchError("Workspace principal contains mismatched tenant state.")
        if str(principal.membership.user_id) != str(principal.user.id):
            raise TenantMismatchError("Workspace principal contains mismatched identity state.")
        return workspace_id

    @staticmethod
    def _schema_error(exc: QueryError) -> bool:
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "no such table",
                "no column named",
                "does not exist",
                "undefined table",
                "undefined column",
            )
        )

    async def _fetch_one(self, cls: type[_T], sql: str, *params: Any, surface: str) -> _T | None:
        try:
            return await self._db.fetch_one(cls, sql, *params)
        except DatabaseConnectionError as exc:
            raise DatabaseUnavailableError(
                f"Board database is unavailable while reading {surface}."
            ) from exc
        except QueryError as exc:
            if self._schema_error(exc):
                raise SchemaMismatchError(
                    f"Board schema cannot read {surface}; run the packaged Board migrations."
                ) from exc
            raise

    async def _fetch(self, cls: type[_T], sql: str, *params: Any, surface: str) -> list[_T]:
        try:
            return await self._db.fetch(cls, sql, *params)
        except DatabaseConnectionError as exc:
            raise DatabaseUnavailableError(
                f"Board database is unavailable while reading {surface}."
            ) from exc
        except QueryError as exc:
            if self._schema_error(exc):
                raise SchemaMismatchError(
                    f"Board schema cannot read {surface}; run the packaged Board migrations."
                ) from exc
            raise

    async def _fetch_val(self, sql: str, *params: Any, surface: str) -> Any:
        try:
            return await self._db.fetch_val(sql, *params)
        except DatabaseConnectionError as exc:
            raise DatabaseUnavailableError(
                f"Board database is unavailable while reading {surface}."
            ) from exc
        except QueryError as exc:
            if self._schema_error(exc):
                raise SchemaMismatchError(
                    f"Board schema cannot read {surface}; run the packaged Board migrations."
                ) from exc
            raise

    async def _execute(self, sql: str, *params: Any, surface: str) -> int:
        try:
            return await self._db.execute(sql, *params)
        except DatabaseConnectionError as exc:
            raise DatabaseUnavailableError(
                f"Board database is unavailable while updating {surface}."
            ) from exc
        except QueryError as exc:
            if self._schema_error(exc):
                raise SchemaMismatchError(
                    f"Board schema cannot update {surface}; run the packaged Board migrations."
                ) from exc
            raise

    @staticmethod
    def _project(row: _ProjectRow) -> Project:
        return Project(
            ProjectId(row.id),
            row.workspace_id,
            row.key,
            row.name,
            row.description,
            row.revision,
            row.created_at,
            row.updated_at,
            row.archived_at,
        )

    @staticmethod
    def _workflow(row: _WorkflowRow) -> Workflow:
        return Workflow(
            WorkflowId(row.id),
            row.workspace_id,
            ProjectId(row.project_id),
            row.name,
            row.revision,
            row.created_at,
            row.updated_at,
            row.archived_at,
        )

    @staticmethod
    def _status(row: _StatusRow) -> WorkflowStatus:
        return WorkflowStatus(
            StatusId(row.id),
            row.workspace_id,
            WorkflowId(row.workflow_id),
            row.name,
            StatusCategory(row.category),
            ColorToken(row.color_token),
            row.position,
            row.created_at,
            row.updated_at,
            row.archived_at,
        )

    @staticmethod
    def _label(row: _LabelRow) -> Label:
        return Label(
            LabelId(row.id),
            row.workspace_id,
            ProjectId(row.project_id),
            row.name,
            ColorToken(row.color_token),
            row.created_at,
            row.archived_at,
        )

    @staticmethod
    def _comment(row: _CommentRow) -> Comment:
        return Comment(
            CommentId(row.id),
            row.workspace_id,
            IssueId(row.issue_id),
            row.author_user_id,
            row.body_source,
            row.revision,
            row.created_at,
            row.updated_at,
            row.redacted_at,
        )

    @staticmethod
    def _saved_view(row: _SavedViewRow) -> SavedView:
        try:
            filters = json.loads(row.typed_filter_json)
        except json.JSONDecodeError as exc:
            raise SchemaMismatchError("Board saved view contains invalid JSON.") from exc
        if not isinstance(filters, dict):
            raise SchemaMismatchError("Board saved view filter is not an object.")
        return SavedView(
            SavedViewId(row.id),
            row.workspace_id,
            ProjectId(row.project_id),
            row.owner_user_id,
            row.name,
            row.schema_version,
            MappingProxyType(filters),
            row.created_at,
            row.updated_at,
        )

    @staticmethod
    def _activity(row: _ActivityRow) -> Activity:
        try:
            metadata = json.loads(row.safe_metadata_json)
        except json.JSONDecodeError as exc:
            raise SchemaMismatchError("Board activity metadata contains invalid JSON.") from exc
        if not isinstance(metadata, dict):
            raise SchemaMismatchError("Board activity metadata is not an object.")
        return Activity(
            ActivityId(row.id),
            row.workspace_id,
            ProjectId(row.project_id),
            IssueId(row.issue_id) if row.issue_id else None,
            row.actor_user_id,
            row.action,
            row.resource_kind,
            row.resource_id,
            MappingProxyType(metadata),
            row.occurred_at,
        )

    async def _project_row(self, workspace_id: str, project_id: str) -> _ProjectRow:
        row = await self._fetch_one(
            _ProjectRow,
            "SELECT id, workspace_id, key, name, description, revision, created_at, "
            "updated_at, archived_at FROM board_projects WHERE workspace_id = ? AND id = ?",
            workspace_id,
            project_id,
            surface="project",
        )
        if row is None:
            raise NotFoundError("Board project does not exist in this workspace.")
        return row

    async def _workflow_row(self, workspace_id: str, project_id: str) -> _WorkflowRow:
        row = await self._fetch_one(
            _WorkflowRow,
            "SELECT id, workspace_id, project_id, name, revision, created_at, updated_at, "
            "archived_at FROM board_workflows WHERE workspace_id = ? AND project_id = ? "
            "AND archived_at IS NULL",
            workspace_id,
            project_id,
            surface="project workflow",
        )
        if row is None:
            raise NotFoundError("Board project has no active workflow.")
        return row

    async def _issue_row(self, workspace_id: str, issue_id: str) -> _IssueRow:
        row = await self._fetch_one(
            _IssueRow,
            "SELECT id, workspace_id, project_id, number, title, description_source, priority, "
            "status_id, reporter_user_id, rank, revision, created_at, updated_at, archived_at "
            "FROM board_issues WHERE workspace_id = ? AND id = ?",
            workspace_id,
            issue_id,
            surface="issue",
        )
        if row is None:
            raise NotFoundError("Board issue does not exist in this workspace.")
        return row

    async def _hydrate_issue(self, row: _IssueRow) -> Issue:
        assignees = await self._fetch(
            _UserRow,
            "SELECT user_id FROM board_issue_assignees WHERE workspace_id = ? AND issue_id = ? "
            "ORDER BY user_id",
            row.workspace_id,
            row.id,
            surface="issue assignees",
        )
        labels = await self._fetch(
            _LabelRow,
            "SELECT l.id, l.workspace_id, l.project_id, l.name, l.color_token, l.created_at, "
            "l.archived_at FROM board_labels l JOIN board_issue_labels il "
            "ON il.workspace_id = l.workspace_id AND il.label_id = l.id "
            "WHERE il.workspace_id = ? AND il.issue_id = ? ORDER BY l.name, l.id",
            row.workspace_id,
            row.id,
            surface="issue labels",
        )
        return Issue(
            IssueId(row.id),
            row.workspace_id,
            ProjectId(row.project_id),
            row.number,
            row.title,
            row.description_source,
            Priority(row.priority),
            StatusId(row.status_id),
            row.reporter_user_id,
            row.rank,
            row.revision,
            row.created_at,
            row.updated_at,
            row.archived_at,
            tuple(item.user_id for item in assignees),
            tuple(self._label(item) for item in labels),
        )

    async def _record(
        self,
        *,
        workspace_id: str,
        project_id: str,
        issue_id: str | None,
        actor_user_id: str,
        action: str,
        resource_kind: str,
        resource_id: str,
        metadata: Mapping[str, str | int | bool | None] | None = None,
        occurred_at: str | None = None,
    ) -> None:
        clean: dict[str, str | int | bool | None] = {}
        for key, value in (metadata or {}).items():
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_METADATA):
                raise ValidationError(
                    f"Board activity metadata key {key!r} could contain a secret."
                )
            if not isinstance(value, str | int | bool | type(None)):
                raise ValidationError("Board activity metadata values must be scalar.")
            clean[key[:40]] = value[:200] if isinstance(value, str) else value
        await self._execute(
            "INSERT INTO board_activity (id, workspace_id, project_id, issue_id, actor_user_id, "
            "action, resource_kind, resource_id, safe_metadata_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._id_factory(),
            workspace_id,
            project_id,
            issue_id,
            actor_user_id,
            action,
            resource_kind,
            resource_id,
            json.dumps(clean, sort_keys=True, separators=(",", ":")),
            occurred_at or self._stamp(),
            surface="immutable Board activity",
        )

    @staticmethod
    def _text(value: str, surface: str, maximum: int, *, required: bool = True) -> str:
        clean = " ".join(value.split()) if "\n" not in value else value.strip()
        if required and not clean:
            raise ValidationError(f"{surface} is required.")
        if len(clean) > maximum:
            raise ValidationError(f"{surface} must contain at most {maximum} characters.")
        return clean

    async def create_project(
        self,
        principal: WorkspacePrincipal,
        *,
        key: str,
        name: str,
        description: str = "",
    ) -> tuple[Project, Workflow, tuple[WorkflowStatus, ...]]:
        require_board_permission(principal, BoardPermission.PROJECT_MANAGE)
        workspace_id = self._workspace(principal)
        clean_key = key.strip().upper()
        if not _PROJECT_KEY_RE.fullmatch(clean_key):
            raise ValidationError(
                "Project key must contain 2 to 8 uppercase letters or digits "
                "and start with a letter."
            )
        clean_name = self._text(name, "Project name", 120)
        clean_description = self._text(description, "Project description", 2000, required=False)
        now = self._stamp()
        project_id = self._id_factory()
        workflow_id = self._id_factory()
        status_specs = (
            ("Backlog", StatusCategory.BACKLOG, ColorToken.GRAY),
            ("Todo", StatusCategory.UNSTARTED, ColorToken.BLUE),
            ("In Progress", StatusCategory.STARTED, ColorToken.YELLOW),
            ("Done", StatusCategory.COMPLETED, ColorToken.GREEN),
        )
        status_ids = tuple(self._id_factory() for _item in status_specs)
        try:
            async with self._db.transaction():
                await self._execute(
                    "INSERT INTO board_projects (id, workspace_id, key, name, description, "
                    "revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    project_id,
                    workspace_id,
                    clean_key,
                    clean_name,
                    clean_description,
                    now,
                    now,
                    surface="project",
                )
                await self._execute(
                    "INSERT INTO board_workflows (id, workspace_id, project_id, name, revision, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                    workflow_id,
                    workspace_id,
                    project_id,
                    f"{clean_name} workflow",
                    now,
                    now,
                    surface="project workflow",
                )
                for position, ((status_name, category, color), status_id) in enumerate(
                    zip(status_specs, status_ids, strict=True)
                ):
                    await self._execute(
                        "INSERT INTO board_statuses (id, workspace_id, workflow_id, name, "
                        "category, color_token, position, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        status_id,
                        workspace_id,
                        workflow_id,
                        status_name,
                        category.value,
                        color.value,
                        position,
                        now,
                        now,
                        surface="default workflow status",
                    )
                await self._execute(
                    "INSERT INTO board_project_issue_counters "
                    "(workspace_id, project_id, next_number) VALUES (?, ?, 1)",
                    workspace_id,
                    project_id,
                    surface="project issue counter",
                )
                await self._record(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    issue_id=None,
                    actor_user_id=str(principal.user.id),
                    action="project.created",
                    resource_kind="project",
                    resource_id=project_id,
                    metadata={"key": clean_key},
                    occurred_at=now,
                )
        except QueryError as exc:
            raise ConflictError("A Board project already uses this key in the workspace.") from exc
        return (
            Project(
                ProjectId(project_id),
                workspace_id,
                clean_key,
                clean_name,
                clean_description,
                1,
                now,
                now,
            ),
            Workflow(
                WorkflowId(workflow_id),
                workspace_id,
                ProjectId(project_id),
                f"{clean_name} workflow",
                1,
                now,
                now,
            ),
            tuple(
                WorkflowStatus(
                    StatusId(status_id),
                    workspace_id,
                    WorkflowId(workflow_id),
                    status_name,
                    category,
                    color,
                    position,
                    now,
                    now,
                )
                for position, ((status_name, category, color), status_id) in enumerate(
                    zip(status_specs, status_ids, strict=True)
                )
            ),
        )

    async def projects(
        self, principal: WorkspacePrincipal, *, include_archived: bool = False
    ) -> tuple[Project, ...]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        rows = await self._fetch(
            _ProjectRow,
            "SELECT id, workspace_id, key, name, description, revision, created_at, "
            "updated_at, archived_at FROM board_projects WHERE workspace_id = ? "
            "AND (? = 1 OR archived_at IS NULL) ORDER BY name, key, id",
            workspace_id,
            include_archived,
            surface="projects",
        )
        return tuple(self._project(row) for row in rows)

    async def project(self, principal: WorkspacePrincipal, project_id: str) -> Project:
        require_board_permission(principal, BoardPermission.READ)
        return self._project(await self._project_row(self._workspace(principal), project_id))

    async def update_project(
        self,
        principal: WorkspacePrincipal,
        project_id: str,
        *,
        expected_revision: int,
        name: str,
        description: str,
    ) -> Project:
        require_board_permission(principal, BoardPermission.PROJECT_MANAGE)
        workspace_id = self._workspace(principal)
        clean_name = self._text(name, "Project name", 120)
        clean_description = self._text(description, "Project description", 2000, required=False)
        now = self._stamp()
        updated = await self._execute(
            "UPDATE board_projects SET name = ?, description = ?, revision = revision + 1, "
            "updated_at = ? WHERE workspace_id = ? AND id = ? AND revision = ? "
            "AND archived_at IS NULL",
            clean_name,
            clean_description,
            now,
            workspace_id,
            project_id,
            expected_revision,
            surface="project details",
        )
        if updated != 1:
            raise ConflictError("Board project changed in another session or is archived.")
        await self._record(
            workspace_id=workspace_id,
            project_id=project_id,
            issue_id=None,
            actor_user_id=str(principal.user.id),
            action="project.updated",
            resource_kind="project",
            resource_id=project_id,
            metadata={"revision": expected_revision + 1},
            occurred_at=now,
        )
        return self._project(await self._project_row(workspace_id, project_id))

    async def workflow(
        self, principal: WorkspacePrincipal, project_id: str
    ) -> tuple[Workflow, tuple[WorkflowStatus, ...]]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        workflow = await self._workflow_row(workspace_id, project_id)
        rows = await self._fetch(
            _StatusRow,
            "SELECT id, workspace_id, workflow_id, name, category, color_token, position, "
            "created_at, updated_at, archived_at FROM board_statuses "
            "WHERE workspace_id = ? AND workflow_id = ? ORDER BY position, id",
            workspace_id,
            workflow.id,
            surface="workflow statuses",
        )
        return self._workflow(workflow), tuple(self._status(row) for row in rows)

    async def add_status(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        expected_workflow_revision: int,
        name: str,
        category: StatusCategory,
        color_token: ColorToken,
    ) -> WorkflowStatus:
        require_board_permission(principal, BoardPermission.WORKFLOW_MANAGE)
        workspace_id = self._workspace(principal)
        clean_name = self._text(name, "Status name", 80)
        now = self._stamp()
        status_id = self._id_factory()
        async with self._db.transaction():
            workflow = await self._workflow_row(workspace_id, project_id)
            if workflow.revision != expected_workflow_revision:
                raise ConflictError("Board workflow changed in another session.")
            maximum = await self._fetch_val(
                "SELECT MAX(position) FROM board_statuses WHERE workspace_id = ? "
                "AND workflow_id = ? AND archived_at IS NULL",
                workspace_id,
                workflow.id,
                surface="workflow status position",
            )
            position = int(maximum) + 1 if maximum is not None else 0
            await self._execute(
                "INSERT INTO board_statuses (id, workspace_id, workflow_id, name, category, "
                "color_token, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                status_id,
                workspace_id,
                workflow.id,
                clean_name,
                category.value,
                color_token.value,
                position,
                now,
                now,
                surface="workflow status",
            )
            updated = await self._execute(
                "UPDATE board_workflows SET revision = revision + 1, updated_at = ? "
                "WHERE workspace_id = ? AND id = ? AND revision = ?",
                now,
                workspace_id,
                workflow.id,
                expected_workflow_revision,
                surface="workflow revision",
            )
            if updated != 1:
                raise ConflictError("Board workflow changed in another session.")
            await self._record(
                workspace_id=workspace_id,
                project_id=project_id,
                issue_id=None,
                actor_user_id=str(principal.user.id),
                action="workflow.status-added",
                resource_kind="status",
                resource_id=status_id,
                metadata={"name": clean_name},
                occurred_at=now,
            )
        return WorkflowStatus(
            StatusId(status_id),
            workspace_id,
            WorkflowId(workflow.id),
            clean_name,
            category,
            color_token,
            position,
            now,
            now,
        )

    async def update_status(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        status_id: str,
        expected_workflow_revision: int,
        name: str,
        category: StatusCategory,
        color_token: ColorToken,
    ) -> WorkflowStatus:
        require_board_permission(principal, BoardPermission.WORKFLOW_MANAGE)
        workspace_id = self._workspace(principal)
        clean_name = self._text(name, "Status name", 80)
        now = self._stamp()
        async with self._db.transaction():
            workflow = await self._workflow_row(workspace_id, project_id)
            if workflow.revision != expected_workflow_revision:
                raise ConflictError("Board workflow changed in another session.")
            updated = await self._execute(
                "UPDATE board_statuses SET name = ?, category = ?, color_token = ?, "
                "updated_at = ? WHERE workspace_id = ? AND workflow_id = ? AND id = ? "
                "AND archived_at IS NULL",
                clean_name,
                category.value,
                color_token.value,
                now,
                workspace_id,
                workflow.id,
                status_id,
                surface="workflow status",
            )
            if updated != 1:
                raise NotFoundError("Active Board workflow status was not found.")
            await self._bump_workflow(
                workspace_id,
                workflow.id,
                expected_revision=expected_workflow_revision,
                now=now,
            )
            await self._record(
                workspace_id=workspace_id,
                project_id=project_id,
                issue_id=None,
                actor_user_id=str(principal.user.id),
                action="workflow.status-updated",
                resource_kind="status",
                resource_id=status_id,
                metadata={"name": clean_name},
                occurred_at=now,
            )
        row = await self._fetch_one(
            _StatusRow,
            "SELECT id, workspace_id, workflow_id, name, category, color_token, position, "
            "created_at, updated_at, archived_at FROM board_statuses "
            "WHERE workspace_id = ? AND id = ?",
            workspace_id,
            status_id,
            surface="workflow status",
        )
        if row is None:
            raise NotFoundError("Board workflow status was not found.")
        return self._status(row)

    async def set_status_archived(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        status_id: str,
        expected_workflow_revision: int,
        archived: bool,
    ) -> WorkflowStatus:
        require_board_permission(principal, BoardPermission.WORKFLOW_MANAGE)
        workspace_id = self._workspace(principal)
        now = self._stamp()
        async with self._db.transaction():
            workflow = await self._workflow_row(workspace_id, project_id)
            if workflow.revision != expected_workflow_revision:
                raise ConflictError("Board workflow changed in another session.")
            row = await self._fetch_one(
                _StatusRow,
                "SELECT id, workspace_id, workflow_id, name, category, color_token, position, "
                "created_at, updated_at, archived_at FROM board_statuses "
                "WHERE workspace_id = ? AND workflow_id = ? AND id = ?",
                workspace_id,
                workflow.id,
                status_id,
                surface="workflow status",
            )
            if row is None:
                raise NotFoundError("Board workflow status was not found.")
            if archived:
                issue_count = int(
                    await self._fetch_val(
                        "SELECT COUNT(*) FROM board_issues WHERE workspace_id = ? "
                        "AND status_id = ?",
                        workspace_id,
                        status_id,
                        surface="workflow status usage",
                    )
                )
                active_count = int(
                    await self._fetch_val(
                        "SELECT COUNT(*) FROM board_statuses WHERE workspace_id = ? "
                        "AND workflow_id = ? AND archived_at IS NULL",
                        workspace_id,
                        workflow.id,
                        surface="active workflow statuses",
                    )
                )
                if issue_count:
                    raise ConflictError("A Board status containing issues cannot be archived.")
                if active_count <= 1:
                    raise ConflictError("A Board workflow must retain at least one active status.")
                position = row.position
                archived_at = now
            else:
                maximum = await self._fetch_val(
                    "SELECT MAX(position) FROM board_statuses WHERE workspace_id = ? "
                    "AND workflow_id = ? AND archived_at IS NULL",
                    workspace_id,
                    workflow.id,
                    surface="workflow status position",
                )
                position = int(maximum) + 1 if maximum is not None else 0
                archived_at = None
            updated = await self._execute(
                "UPDATE board_statuses SET archived_at = ?, position = ?, updated_at = ? "
                "WHERE workspace_id = ? AND workflow_id = ? AND id = ?",
                archived_at,
                position,
                now,
                workspace_id,
                workflow.id,
                status_id,
                surface="workflow status archive state",
            )
            if updated != 1:
                raise ConflictError("Board workflow status changed in another session.")
            await self._bump_workflow(
                workspace_id,
                workflow.id,
                expected_revision=expected_workflow_revision,
                now=now,
            )
            await self._record(
                workspace_id=workspace_id,
                project_id=project_id,
                issue_id=None,
                actor_user_id=str(principal.user.id),
                action="workflow.status-archived" if archived else "workflow.status-restored",
                resource_kind="status",
                resource_id=status_id,
                metadata={},
                occurred_at=now,
            )
        return self._status(
            _StatusRow(
                row.id,
                row.workspace_id,
                row.workflow_id,
                row.name,
                row.category,
                row.color_token,
                position,
                row.created_at,
                now,
                archived_at,
            )
        )

    async def _bump_workflow(
        self, workspace_id: str, workflow_id: str, *, expected_revision: int, now: str
    ) -> None:
        updated = await self._execute(
            "UPDATE board_workflows SET revision = revision + 1, updated_at = ? "
            "WHERE workspace_id = ? AND id = ? AND revision = ?",
            now,
            workspace_id,
            workflow_id,
            expected_revision,
            surface="workflow revision",
        )
        if updated != 1:
            raise ConflictError("Board workflow changed in another session.")

    async def reorder_statuses(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        expected_workflow_revision: int,
        ordered_status_ids: Sequence[str],
    ) -> Workflow:
        require_board_permission(principal, BoardPermission.WORKFLOW_MANAGE)
        workspace_id = self._workspace(principal)
        ordered = tuple(dict.fromkeys(ordered_status_ids))
        now = self._stamp()
        async with self._db.transaction():
            workflow = await self._workflow_row(workspace_id, project_id)
            rows = await self._fetch(
                _IdRow,
                "SELECT id FROM board_statuses WHERE workspace_id = ? AND workflow_id = ? "
                "AND archived_at IS NULL ORDER BY position",
                workspace_id,
                workflow.id,
                surface="active workflow statuses",
            )
            if workflow.revision != expected_workflow_revision:
                raise ConflictError("Board workflow changed in another session.")
            if set(ordered) != {row.id for row in rows} or len(ordered) != len(rows):
                raise ValidationError(
                    "Status order must contain every active workflow status once."
                )
            maximum_position = await self._fetch_val(
                "SELECT COALESCE(MAX(position), 0) FROM board_statuses WHERE workspace_id = ? "
                "AND workflow_id = ? AND archived_at IS NULL",
                workspace_id,
                workflow.id,
                surface="workflow status positions",
            )
            temporary_base = int(maximum_position) + len(rows)
            for offset, status_id in enumerate(ordered, start=1):
                await self._execute(
                    "UPDATE board_statuses SET position = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND workflow_id = ? AND id = ?",
                    (temporary_base + offset) * _RANK_STEP,
                    now,
                    workspace_id,
                    workflow.id,
                    status_id,
                    surface="temporary workflow status order",
                )
            for position, status_id in enumerate(ordered):
                await self._execute(
                    "UPDATE board_statuses SET position = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND workflow_id = ? AND id = ?",
                    position,
                    now,
                    workspace_id,
                    workflow.id,
                    status_id,
                    surface="workflow status order",
                )
            updated = await self._execute(
                "UPDATE board_workflows SET revision = revision + 1, updated_at = ? "
                "WHERE workspace_id = ? AND id = ? AND revision = ?",
                now,
                workspace_id,
                workflow.id,
                expected_workflow_revision,
                surface="workflow order revision",
            )
            if updated != 1:
                raise ConflictError("Board workflow changed in another session.")
            await self._record(
                workspace_id=workspace_id,
                project_id=project_id,
                issue_id=None,
                actor_user_id=str(principal.user.id),
                action="workflow.reordered",
                resource_kind="workflow",
                resource_id=workflow.id,
                metadata={"status_count": len(ordered)},
                occurred_at=now,
            )
        return Workflow(
            WorkflowId(workflow.id),
            workspace_id,
            ProjectId(project_id),
            workflow.name,
            expected_workflow_revision + 1,
            workflow.created_at,
            now,
            workflow.archived_at,
        )

    async def create_label(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        name: str,
        color_token: ColorToken,
    ) -> Label:
        require_board_permission(principal, BoardPermission.PROJECT_MANAGE)
        workspace_id = self._workspace(principal)
        project = await self._project_row(workspace_id, project_id)
        if project.archived_at:
            raise ConflictError("Archived Board projects cannot create labels.")
        clean_name = self._text(name, "Label name", 40)
        now = self._stamp()
        label_id = self._id_factory()
        try:
            await self._execute(
                "INSERT INTO board_labels (id, workspace_id, project_id, name, normalized_name, "
                "color_token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                label_id,
                workspace_id,
                project_id,
                clean_name,
                clean_name.casefold(),
                color_token.value,
                now,
                surface="project label",
            )
        except QueryError as exc:
            raise ConflictError("A Board label already uses this name in the project.") from exc
        return Label(
            LabelId(label_id),
            workspace_id,
            ProjectId(project_id),
            clean_name,
            color_token,
            now,
        )

    async def labels(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        include_archived: bool = False,
    ) -> tuple[Label, ...]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        await self._project_row(workspace_id, project_id)
        rows = await self._fetch(
            _LabelRow,
            "SELECT id, workspace_id, project_id, name, color_token, created_at, archived_at "
            "FROM board_labels WHERE workspace_id = ? AND project_id = ? "
            "AND (? = 1 OR archived_at IS NULL) ORDER BY normalized_name, id",
            workspace_id,
            project_id,
            include_archived,
            surface="project labels",
        )
        return tuple(self._label(row) for row in rows)

    async def create_issue(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        title: str,
        description_source: str = "",
        priority: Priority = Priority.NONE,
        status_id: str | None = None,
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_CREATE)
        workspace_id = self._workspace(principal)
        clean_title = self._text(title, "Issue title", 200)
        clean_description = self._text(
            description_source, "Issue description", 100_000, required=False
        )
        now = self._stamp()
        issue_id = self._id_factory()
        async with self._db.transaction():
            project = await self._project_row(workspace_id, project_id)
            if project.archived_at:
                raise ConflictError("Archived Board projects cannot create issues.")
            workflow = await self._workflow_row(workspace_id, project_id)
            if status_id is None:
                status = await self._fetch_one(
                    _StatusRow,
                    "SELECT id, workspace_id, workflow_id, name, category, color_token, "
                    "position, created_at, updated_at, archived_at FROM board_statuses "
                    "WHERE workspace_id = ? AND workflow_id = ? AND archived_at IS NULL "
                    "ORDER BY position, id LIMIT 1",
                    workspace_id,
                    workflow.id,
                    surface="default workflow status",
                )
            else:
                status = await self._fetch_one(
                    _StatusRow,
                    "SELECT id, workspace_id, workflow_id, name, category, color_token, "
                    "position, created_at, updated_at, archived_at FROM board_statuses "
                    "WHERE workspace_id = ? AND workflow_id = ? AND id = ? "
                    "AND archived_at IS NULL",
                    workspace_id,
                    workflow.id,
                    status_id,
                    surface="selected workflow status",
                )
            if status is None:
                raise ValidationError("Issue status is not active in this project workflow.")
            counter = await self._fetch_one(
                _CounterRow,
                "UPDATE board_project_issue_counters SET next_number = next_number + 1 "
                "WHERE workspace_id = ? AND project_id = ? RETURNING next_number",
                workspace_id,
                project_id,
                surface="issue number allocation",
            )
            if counter is None:
                raise SchemaMismatchError("Board project issue counter is missing.")
            number = counter.next_number - 1
            maximum = await self._fetch_val(
                "SELECT MAX(rank) FROM board_issues WHERE workspace_id = ? AND status_id = ? "
                "AND archived_at IS NULL",
                workspace_id,
                status.id,
                surface="issue rank",
            )
            rank = int(maximum) + _RANK_STEP if maximum is not None else _RANK_STEP
            await self._execute(
                "INSERT INTO board_issues (id, workspace_id, project_id, number, title, "
                "description_source, priority, status_id, reporter_user_id, rank, revision, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                issue_id,
                workspace_id,
                project_id,
                number,
                clean_title,
                clean_description,
                priority.value,
                status.id,
                str(principal.user.id),
                rank,
                now,
                now,
                surface="issue",
            )
            await self._execute(
                "INSERT INTO board_issue_subscriptions "
                "(workspace_id, issue_id, user_id, created_at) VALUES (?, ?, ?, ?)",
                workspace_id,
                issue_id,
                str(principal.user.id),
                now,
                surface="reporter subscription",
            )
            await self._record(
                workspace_id=workspace_id,
                project_id=project_id,
                issue_id=issue_id,
                actor_user_id=str(principal.user.id),
                action="issue.created",
                resource_kind="issue",
                resource_id=issue_id,
                metadata={"number": number, "status_id": status.id},
                occurred_at=now,
            )
        return await self.get_issue(principal, issue_id)

    async def get_issue(self, principal: WorkspacePrincipal, issue_id: str) -> Issue:
        require_board_permission(principal, BoardPermission.READ)
        row = await self._issue_row(self._workspace(principal), issue_id)
        return await self._hydrate_issue(row)

    async def list_issues(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        query: str = "",
        status_ids: Sequence[str] = (),
        priorities: Sequence[Priority] = (),
        assignee_ids: Sequence[str] = (),
        label_ids: Sequence[str] = (),
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> IssuePage:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        await self._project_row(workspace_id, project_id)
        if not 1 <= limit <= 100:
            raise ValidationError("Board issue page size must be between 1 and 100.")
        clean_query = self._text(query, "Issue search", 100, required=False).casefold()
        before_updated, before_id = self._decode_cursor(cursor) if cursor else (None, None)
        clauses = ["i.workspace_id = ?", "i.project_id = ?"]
        params: list[Any] = [workspace_id, project_id]
        if not include_archived:
            clauses.append("i.archived_at IS NULL")
        if clean_query:
            clauses.append("LOWER(i.title || ' ' || i.description_source) LIKE ?")
            params.append(f"%{clean_query}%")
        self._append_in_filter(clauses, params, "i.status_id", status_ids)
        self._append_in_filter(
            clauses, params, "i.priority", tuple(item.value for item in priorities)
        )
        if assignee_ids:
            assignees = tuple(dict.fromkeys(assignee_ids))
            placeholders = ", ".join("?" for _item in assignees)
            clauses.append(
                "EXISTS (SELECT 1 FROM board_issue_assignees a WHERE "
                "a.workspace_id = i.workspace_id AND a.issue_id = i.id "
                f"AND a.user_id IN ({placeholders}))"
            )
            params.extend(assignees)
        if label_ids:
            labels = tuple(dict.fromkeys(label_ids))
            placeholders = ", ".join("?" for _item in labels)
            clauses.append(
                "EXISTS (SELECT 1 FROM board_issue_labels il WHERE "
                "il.workspace_id = i.workspace_id AND il.issue_id = i.id "
                f"AND il.label_id IN ({placeholders}))"
            )
            params.extend(labels)
        if before_updated is not None:
            clauses.append("(i.updated_at < ? OR (i.updated_at = ? AND i.id < ?))")
            params.extend((before_updated, before_updated, before_id))
        params.append(limit + 1)
        rows = await self._fetch(
            _IssueRow,
            "SELECT i.id, i.workspace_id, i.project_id, i.number, i.title, "
            "i.description_source, i.priority, i.status_id, i.reporter_user_id, i.rank, "
            "i.revision, i.created_at, i.updated_at, i.archived_at FROM board_issues i WHERE "
            + " AND ".join(clauses)
            + " ORDER BY i.updated_at DESC, i.id DESC LIMIT ?",
            *params,
            surface="issue page",
        )
        page_rows = rows[:limit]
        items = tuple([await self._hydrate_issue(row) for row in page_rows])
        next_cursor = (
            self._encode_cursor(page_rows[-1].updated_at, page_rows[-1].id)
            if len(rows) > limit and page_rows
            else None
        )
        return IssuePage(items, next_cursor)

    @staticmethod
    def _append_in_filter(
        clauses: list[str], params: list[Any], column: str, values: Sequence[str]
    ) -> None:
        unique = tuple(dict.fromkeys(values))
        if not unique:
            return
        clauses.append(f"{column} IN ({', '.join('?' for _item in unique)})")
        params.extend(unique)

    @staticmethod
    def _encode_cursor(updated_at: str, issue_id: str) -> str:
        return (
            base64.urlsafe_b64encode(
                json.dumps([updated_at, issue_id], separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(raw)
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            updated_at, issue_id = value
            if not isinstance(updated_at, str) or not isinstance(issue_id, str):
                raise ValueError
            datetime.fromisoformat(updated_at)
            if not updated_at or not issue_id:
                raise ValueError
            return updated_at, issue_id
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("Board issue cursor is invalid.") from exc

    async def update_issue(
        self,
        principal: WorkspacePrincipal,
        issue_id: str,
        *,
        expected_revision: int,
        title: str,
        description_source: str,
        priority: Priority,
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_UPDATE)
        workspace_id = self._workspace(principal)
        clean_title = self._text(title, "Issue title", 200)
        clean_description = self._text(
            description_source, "Issue description", 100_000, required=False
        )
        now = self._stamp()
        row = await self._issue_row(workspace_id, issue_id)
        if row.archived_at:
            raise ConflictError("Archived Board issues cannot be edited.")
        updated = await self._execute(
            "UPDATE board_issues SET title = ?, description_source = ?, priority = ?, "
            "revision = revision + 1, updated_at = ? WHERE workspace_id = ? AND id = ? "
            "AND revision = ? AND archived_at IS NULL",
            clean_title,
            clean_description,
            priority.value,
            now,
            workspace_id,
            issue_id,
            expected_revision,
            surface="issue",
        )
        if updated != 1:
            raise ConflictError("Board issue changed in another session. Reload and try again.")
        await self._record(
            workspace_id=workspace_id,
            project_id=row.project_id,
            issue_id=issue_id,
            actor_user_id=str(principal.user.id),
            action="issue.updated",
            resource_kind="issue",
            resource_id=issue_id,
            metadata={"revision": expected_revision + 1},
            occurred_at=now,
        )
        return await self.get_issue(principal, issue_id)

    async def move_issue(
        self,
        principal: WorkspacePrincipal,
        issue_id: str,
        *,
        expected_revision: int,
        status_id: str,
        before_issue_id: str | None = None,
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_MOVE)
        workspace_id = self._workspace(principal)
        now = self._stamp()
        async with self._db.transaction():
            issue = await self._issue_row(workspace_id, issue_id)
            if issue.archived_at:
                raise ConflictError("Archived Board issues cannot move.")
            await self._execute(
                "UPDATE board_projects SET updated_at = updated_at "
                "WHERE workspace_id = ? AND id = ?",
                workspace_id,
                issue.project_id,
                surface="project move lock",
            )
            workflow = await self._workflow_row(workspace_id, issue.project_id)
            status = await self._fetch_one(
                _StatusRow,
                "SELECT id, workspace_id, workflow_id, name, category, color_token, position, "
                "created_at, updated_at, archived_at FROM board_statuses WHERE workspace_id = ? "
                "AND workflow_id = ? AND id = ? AND archived_at IS NULL",
                workspace_id,
                workflow.id,
                status_id,
                surface="move target status",
            )
            if status is None:
                raise ValidationError("Move target is not an active status in this project.")
            rank = await self._rank_for_move(
                workspace_id=workspace_id,
                status_id=status_id,
                issue_id=issue_id,
                before_issue_id=before_issue_id,
            )
            updated = await self._execute(
                "UPDATE board_issues SET status_id = ?, rank = ?, revision = revision + 1, "
                "updated_at = ? WHERE workspace_id = ? AND id = ? AND revision = ? "
                "AND archived_at IS NULL",
                status_id,
                rank,
                now,
                workspace_id,
                issue_id,
                expected_revision,
                surface="issue move",
            )
            if updated != 1:
                raise ConflictError("Board issue changed in another session. Reload and try again.")
            await self._record(
                workspace_id=workspace_id,
                project_id=issue.project_id,
                issue_id=issue_id,
                actor_user_id=str(principal.user.id),
                action="issue.moved",
                resource_kind="issue",
                resource_id=issue_id,
                metadata={"from_status_id": issue.status_id, "to_status_id": status_id},
                occurred_at=now,
            )
        return await self.get_issue(principal, issue_id)

    async def _rank_for_move(
        self,
        *,
        workspace_id: str,
        status_id: str,
        issue_id: str,
        before_issue_id: str | None,
    ) -> int:
        if before_issue_id == issue_id:
            raise ValidationError("A Board issue cannot be moved before itself.")
        if before_issue_id is None:
            maximum = await self._fetch_val(
                "SELECT MAX(rank) FROM board_issues WHERE workspace_id = ? AND status_id = ? "
                "AND id != ? AND archived_at IS NULL",
                workspace_id,
                status_id,
                issue_id,
                surface="move rank",
            )
            return int(maximum) + _RANK_STEP if maximum is not None else _RANK_STEP
        before = await self._fetch_one(
            _IssueRow,
            "SELECT id, workspace_id, project_id, number, title, description_source, priority, "
            "status_id, reporter_user_id, rank, revision, created_at, updated_at, archived_at "
            "FROM board_issues WHERE workspace_id = ? AND id = ? AND status_id = ? "
            "AND archived_at IS NULL",
            workspace_id,
            before_issue_id,
            status_id,
            surface="move insertion target",
        )
        if before is None:
            raise ValidationError("Move insertion target is not in the selected status.")
        previous = await self._fetch_one(
            _RankRow,
            "SELECT rank FROM board_issues WHERE workspace_id = ? AND status_id = ? "
            "AND id != ? AND rank < ? AND archived_at IS NULL ORDER BY rank DESC, id DESC LIMIT 1",
            workspace_id,
            status_id,
            issue_id,
            before.rank,
            surface="move predecessor rank",
        )
        lower = previous.rank if previous else 0
        if before.rank - lower > 1:
            return lower + (before.rank - lower) // 2
        rows = await self._fetch(
            _IdRow,
            "SELECT id FROM board_issues WHERE workspace_id = ? AND status_id = ? "
            "AND id != ? AND archived_at IS NULL ORDER BY rank, number, id",
            workspace_id,
            status_id,
            issue_id,
            surface="status rank compaction",
        )
        maximum = int(
            await self._fetch_val(
                "SELECT COALESCE(MAX(rank), 0) FROM board_issues WHERE workspace_id = ? "
                "AND status_id = ?",
                workspace_id,
                status_id,
                surface="status rank compaction",
            )
        )
        current_in_status = await self._fetch_val(
            "SELECT COUNT(*) FROM board_issues WHERE workspace_id = ? AND status_id = ? AND id = ?",
            workspace_id,
            status_id,
            issue_id,
            surface="status rank compaction",
        )
        if current_in_status:
            maximum += _RANK_STEP
            await self._execute(
                "UPDATE board_issues SET rank = ? WHERE workspace_id = ? AND id = ?",
                maximum,
                workspace_id,
                issue_id,
                surface="temporary issue rank",
            )
        for index, row in enumerate(rows, start=1):
            await self._execute(
                "UPDATE board_issues SET rank = ? WHERE workspace_id = ? AND id = ?",
                maximum + index * _RANK_STEP,
                workspace_id,
                row.id,
                surface="temporary status rank compaction",
            )
        for index, row in enumerate(rows, start=1):
            await self._execute(
                "UPDATE board_issues SET rank = ? WHERE workspace_id = ? AND id = ?",
                index * _RANK_STEP,
                workspace_id,
                row.id,
                surface="status rank compaction",
            )
        before_index = next(index for index, row in enumerate(rows, start=1) if row.id == before.id)
        return before_index * _RANK_STEP - _RANK_STEP // 2

    async def replace_assignees(
        self,
        principal: WorkspacePrincipal,
        issue_id: str,
        *,
        expected_revision: int,
        user_ids: Sequence[str],
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_ASSIGN)
        workspace_id = self._workspace(principal)
        users = tuple(dict.fromkeys(user_ids))
        now = self._stamp()
        async with self._db.transaction():
            issue = await self._issue_row(workspace_id, issue_id)
            if issue.archived_at:
                raise ConflictError("Archived Board issues cannot change assignees.")
            if users:
                placeholders = ", ".join("?" for _item in users)
                members = await self._fetch(
                    _UserRow,
                    "SELECT user_id FROM workspace_core_memberships WHERE workspace_id = ? "
                    f"AND user_id IN ({placeholders})",
                    workspace_id,
                    *users,
                    surface="assignment members",
                )
                if {row.user_id for row in members} != set(users):
                    raise ValidationError("Every assignee must be a current workspace member.")
            await self._bump_issue(
                workspace_id, issue_id, expected_revision=expected_revision, now=now
            )
            await self._execute(
                "DELETE FROM board_issue_assignees WHERE workspace_id = ? AND issue_id = ?",
                workspace_id,
                issue_id,
                surface="issue assignees",
            )
            for user_id in users:
                await self._execute(
                    "INSERT INTO board_issue_assignees "
                    "(workspace_id, issue_id, user_id, assigned_at) VALUES (?, ?, ?, ?)",
                    workspace_id,
                    issue_id,
                    user_id,
                    now,
                    surface="issue assignee",
                )
            await self._record(
                workspace_id=workspace_id,
                project_id=issue.project_id,
                issue_id=issue_id,
                actor_user_id=str(principal.user.id),
                action="issue.assignees-replaced",
                resource_kind="issue",
                resource_id=issue_id,
                metadata={"assignee_count": len(users)},
                occurred_at=now,
            )
        return await self.get_issue(principal, issue_id)

    async def replace_labels(
        self,
        principal: WorkspacePrincipal,
        issue_id: str,
        *,
        expected_revision: int,
        label_ids: Sequence[str],
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_UPDATE)
        workspace_id = self._workspace(principal)
        labels = tuple(dict.fromkeys(label_ids))
        now = self._stamp()
        async with self._db.transaction():
            issue = await self._issue_row(workspace_id, issue_id)
            if labels:
                placeholders = ", ".join("?" for _item in labels)
                rows = await self._fetch(
                    _IdRow,
                    "SELECT id FROM board_labels WHERE workspace_id = ? AND project_id = ? "
                    f"AND id IN ({placeholders}) AND archived_at IS NULL",
                    workspace_id,
                    issue.project_id,
                    *labels,
                    surface="selected labels",
                )
                if {row.id for row in rows} != set(labels):
                    raise ValidationError("Every label must be active in this issue's project.")
            await self._bump_issue(
                workspace_id, issue_id, expected_revision=expected_revision, now=now
            )
            await self._execute(
                "DELETE FROM board_issue_labels WHERE workspace_id = ? AND issue_id = ?",
                workspace_id,
                issue_id,
                surface="issue labels",
            )
            for label_id in labels:
                await self._execute(
                    "INSERT INTO board_issue_labels (workspace_id, issue_id, label_id) "
                    "VALUES (?, ?, ?)",
                    workspace_id,
                    issue_id,
                    label_id,
                    surface="issue label",
                )
            await self._record(
                workspace_id=workspace_id,
                project_id=issue.project_id,
                issue_id=issue_id,
                actor_user_id=str(principal.user.id),
                action="issue.labels-replaced",
                resource_kind="issue",
                resource_id=issue_id,
                metadata={"label_count": len(labels)},
                occurred_at=now,
            )
        return await self.get_issue(principal, issue_id)

    async def _bump_issue(
        self, workspace_id: str, issue_id: str, *, expected_revision: int, now: str
    ) -> None:
        updated = await self._execute(
            "UPDATE board_issues SET revision = revision + 1, updated_at = ? "
            "WHERE workspace_id = ? AND id = ? AND revision = ? AND archived_at IS NULL",
            now,
            workspace_id,
            issue_id,
            expected_revision,
            surface="issue revision",
        )
        if updated != 1:
            raise ConflictError("Board issue changed in another session. Reload and try again.")

    async def add_comment(
        self, principal: WorkspacePrincipal, issue_id: str, *, body_source: str
    ) -> Comment:
        require_board_permission(principal, BoardPermission.COMMENT_CREATE)
        workspace_id = self._workspace(principal)
        clean_body = self._text(body_source, "Comment", 20_000)
        now = self._stamp()
        comment_id = self._id_factory()
        async with self._db.transaction():
            issue = await self._issue_row(workspace_id, issue_id)
            if issue.archived_at:
                raise ConflictError("Archived Board issues cannot receive comments.")
            await self._execute(
                "INSERT INTO board_comments (id, workspace_id, issue_id, author_user_id, "
                "body_source, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                comment_id,
                workspace_id,
                issue_id,
                str(principal.user.id),
                clean_body,
                now,
                now,
                surface="issue comment",
            )
            await self._execute(
                "INSERT INTO board_issue_subscriptions (workspace_id, issue_id, user_id, "
                "created_at) VALUES (?, ?, ?, ?) ON CONFLICT (workspace_id, issue_id, user_id) "
                "DO NOTHING",
                workspace_id,
                issue_id,
                str(principal.user.id),
                now,
                surface="commenter subscription",
            )
            await self._record(
                workspace_id=workspace_id,
                project_id=issue.project_id,
                issue_id=issue_id,
                actor_user_id=str(principal.user.id),
                action="comment.created",
                resource_kind="comment",
                resource_id=comment_id,
                metadata={},
                occurred_at=now,
            )
        return Comment(
            CommentId(comment_id),
            workspace_id,
            IssueId(issue_id),
            str(principal.user.id),
            clean_body,
            1,
            now,
            now,
        )

    async def comments(self, principal: WorkspacePrincipal, issue_id: str) -> tuple[Comment, ...]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        await self._issue_row(workspace_id, issue_id)
        rows = await self._fetch(
            _CommentRow,
            "SELECT id, workspace_id, issue_id, author_user_id, body_source, revision, "
            "created_at, updated_at, redacted_at FROM board_comments "
            "WHERE workspace_id = ? AND issue_id = ? ORDER BY created_at, id",
            workspace_id,
            issue_id,
            surface="issue comments",
        )
        return tuple(self._comment(row) for row in rows)

    async def redact_comment(
        self,
        principal: WorkspacePrincipal,
        comment_id: str,
        *,
        expected_revision: int,
    ) -> Comment:
        workspace_id = self._workspace(principal)
        row = await self._fetch_one(
            _CommentRow,
            "SELECT id, workspace_id, issue_id, author_user_id, body_source, revision, "
            "created_at, updated_at, redacted_at FROM board_comments "
            "WHERE workspace_id = ? AND id = ?",
            workspace_id,
            comment_id,
            surface="comment",
        )
        if row is None:
            raise NotFoundError("Board comment does not exist in this workspace.")
        if row.author_user_id == str(principal.user.id):
            require_board_permission(principal, BoardPermission.COMMENT_REDACT_OWN)
        else:
            require_board_permission(principal, BoardPermission.COMMENT_REDACT_ANY)
        if row.redacted_at:
            return self._comment(row)
        issue = await self._issue_row(workspace_id, row.issue_id)
        now = self._stamp()
        updated = await self._execute(
            "UPDATE board_comments SET body_source = '', redacted_at = ?, updated_at = ?, "
            "revision = revision + 1 WHERE workspace_id = ? AND id = ? AND revision = ? "
            "AND redacted_at IS NULL",
            now,
            now,
            workspace_id,
            comment_id,
            expected_revision,
            surface="comment redaction",
        )
        if updated != 1:
            raise ConflictError("Board comment changed in another session.")
        await self._record(
            workspace_id=workspace_id,
            project_id=issue.project_id,
            issue_id=issue.id,
            actor_user_id=str(principal.user.id),
            action="comment.redacted",
            resource_kind="comment",
            resource_id=comment_id,
            metadata={"author_redaction": row.author_user_id == str(principal.user.id)},
            occurred_at=now,
        )
        return Comment(
            CommentId(comment_id),
            workspace_id,
            IssueId(row.issue_id),
            row.author_user_id,
            "",
            expected_revision + 1,
            row.created_at,
            now,
            now,
        )

    async def set_subscription(
        self, principal: WorkspacePrincipal, issue_id: str, *, subscribed: bool
    ) -> bool:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        await self._issue_row(workspace_id, issue_id)
        if subscribed:
            await self._execute(
                "INSERT INTO board_issue_subscriptions (workspace_id, issue_id, user_id, "
                "created_at) VALUES (?, ?, ?, ?) ON CONFLICT (workspace_id, issue_id, user_id) "
                "DO NOTHING",
                workspace_id,
                issue_id,
                str(principal.user.id),
                self._stamp(),
                surface="issue subscription",
            )
        else:
            await self._execute(
                "DELETE FROM board_issue_subscriptions WHERE workspace_id = ? AND issue_id = ? "
                "AND user_id = ?",
                workspace_id,
                issue_id,
                str(principal.user.id),
                surface="issue subscription",
            )
        return subscribed

    async def save_view(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        name: str,
        filters: Mapping[str, object],
        shared: bool = False,
    ) -> SavedView:
        require_board_permission(
            principal,
            BoardPermission.PROJECT_VIEW_MANAGE if shared else BoardPermission.PERSONAL_VIEW_MANAGE,
        )
        workspace_id = self._workspace(principal)
        await self._project_row(workspace_id, project_id)
        clean_name = self._text(name, "Saved view name", 80)
        clean_filters = self._validated_filters(filters)
        owner_user_id = None if shared else str(principal.user.id)
        view_id = self._id_factory()
        now = self._stamp()
        try:
            await self._execute(
                "INSERT INTO board_saved_views (id, workspace_id, project_id, owner_user_id, "
                "name, schema_version, typed_filter_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                view_id,
                workspace_id,
                project_id,
                owner_user_id,
                clean_name,
                json.dumps(clean_filters, sort_keys=True, separators=(",", ":")),
                now,
                now,
                surface="saved view",
            )
        except QueryError as exc:
            raise ConflictError(
                "A saved view already uses this name in the selected scope."
            ) from exc
        return SavedView(
            SavedViewId(view_id),
            workspace_id,
            ProjectId(project_id),
            owner_user_id,
            clean_name,
            1,
            MappingProxyType(clean_filters),
            now,
            now,
        )

    @staticmethod
    def _validated_filters(filters: Mapping[str, object]) -> dict[str, object]:
        unknown = set(filters) - _FILTER_KEYS
        if unknown:
            raise ValidationError(f"Saved view contains unsupported filters: {sorted(unknown)}.")
        clean: dict[str, object] = {}
        query = filters.get("query", "")
        if not isinstance(query, str) or len(query.strip()) > 100:
            raise ValidationError("Saved view query must be text under 101 characters.")
        clean["query"] = query.strip()
        for key in ("status_ids", "assignee_ids", "label_ids"):
            values = filters.get(key, ())
            if not isinstance(values, list | tuple) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ValidationError(f"Saved view {key} must contain non-empty identifiers.")
            clean[key] = list(dict.fromkeys(values))[:100]
        priorities = filters.get("priorities", ())
        if not isinstance(priorities, list | tuple):
            raise ValidationError("Saved view priorities must be a list.")
        try:
            clean["priorities"] = list(
                dict.fromkeys(Priority(str(item)).value for item in priorities)
            )
        except ValueError as exc:
            raise ValidationError("Saved view contains an unsupported priority.") from exc
        include_archived = filters.get("include_archived", False)
        if not isinstance(include_archived, bool):
            raise ValidationError("Saved view archive filter must be boolean.")
        clean["include_archived"] = include_archived
        sort = filters.get("sort", "updated_desc")
        group_by = filters.get("group_by", "status")
        if sort not in _FILTER_SORTS or group_by not in _FILTER_GROUPS:
            raise ValidationError("Saved view sort or grouping is unsupported.")
        clean["sort"] = sort
        clean["group_by"] = group_by
        return clean

    async def saved_views(
        self, principal: WorkspacePrincipal, *, project_id: str
    ) -> tuple[SavedView, ...]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        rows = await self._fetch(
            _SavedViewRow,
            "SELECT id, workspace_id, project_id, owner_user_id, name, schema_version, "
            "typed_filter_json, created_at, updated_at FROM board_saved_views "
            "WHERE workspace_id = ? AND project_id = ? AND "
            "(owner_user_id IS NULL OR owner_user_id = ?) ORDER BY owner_user_id, name, id",
            workspace_id,
            project_id,
            str(principal.user.id),
            surface="saved views",
        )
        return tuple(self._saved_view(row) for row in rows)

    async def set_issue_archived(
        self,
        principal: WorkspacePrincipal,
        issue_id: str,
        *,
        expected_revision: int,
        archived: bool,
    ) -> Issue:
        require_board_permission(principal, BoardPermission.ISSUE_ARCHIVE)
        workspace_id = self._workspace(principal)
        issue = await self._issue_row(workspace_id, issue_id)
        now = self._stamp()
        archived_at = now if archived else None
        updated = await self._execute(
            "UPDATE board_issues SET archived_at = ?, revision = revision + 1, updated_at = ? "
            "WHERE workspace_id = ? AND id = ? AND revision = ?",
            archived_at,
            now,
            workspace_id,
            issue_id,
            expected_revision,
            surface="issue archive state",
        )
        if updated != 1:
            raise ConflictError("Board issue changed in another session. Reload and try again.")
        await self._record(
            workspace_id=workspace_id,
            project_id=issue.project_id,
            issue_id=issue_id,
            actor_user_id=str(principal.user.id),
            action="issue.archived" if archived else "issue.restored",
            resource_kind="issue",
            resource_id=issue_id,
            metadata={},
            occurred_at=now,
        )
        return await self.get_issue(principal, issue_id)

    async def set_project_archived(
        self,
        principal: WorkspacePrincipal,
        project_id: str,
        *,
        expected_revision: int,
        archived: bool,
    ) -> Project:
        require_board_permission(principal, BoardPermission.PROJECT_MANAGE)
        workspace_id = self._workspace(principal)
        now = self._stamp()
        archived_at = now if archived else None
        updated = await self._execute(
            "UPDATE board_projects SET archived_at = ?, revision = revision + 1, updated_at = ? "
            "WHERE workspace_id = ? AND id = ? AND revision = ?",
            archived_at,
            now,
            workspace_id,
            project_id,
            expected_revision,
            surface="project archive state",
        )
        if updated != 1:
            raise ConflictError("Board project changed in another session.")
        await self._record(
            workspace_id=workspace_id,
            project_id=project_id,
            issue_id=None,
            actor_user_id=str(principal.user.id),
            action="project.archived" if archived else "project.restored",
            resource_kind="project",
            resource_id=project_id,
            metadata={},
            occurred_at=now,
        )
        return self._project(await self._project_row(workspace_id, project_id))

    async def activity(
        self,
        principal: WorkspacePrincipal,
        *,
        project_id: str,
        issue_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Activity, ...]:
        require_board_permission(principal, BoardPermission.READ)
        workspace_id = self._workspace(principal)
        if not 1 <= limit <= 500:
            raise ValidationError("Board activity limit must be between 1 and 500.")
        rows = await self._fetch(
            _ActivityRow,
            "SELECT id, workspace_id, project_id, issue_id, actor_user_id, action, "
            "resource_kind, resource_id, safe_metadata_json, occurred_at FROM board_activity "
            "WHERE workspace_id = ? AND project_id = ? AND (? IS NULL OR issue_id = ?) "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            workspace_id,
            project_id,
            issue_id,
            issue_id,
            limit,
            surface="Board activity",
        )
        return tuple(self._activity(row) for row in rows)
