"""Immutable Board domain records and constrained value enums."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import NewType

ProjectId = NewType("ProjectId", str)
WorkflowId = NewType("WorkflowId", str)
StatusId = NewType("StatusId", str)
IssueId = NewType("IssueId", str)
LabelId = NewType("LabelId", str)
CommentId = NewType("CommentId", str)
SavedViewId = NewType("SavedViewId", str)
ActivityId = NewType("ActivityId", str)


class StatusCategory(StrEnum):
    BACKLOG = "backlog"
    UNSTARTED = "unstarted"
    STARTED = "started"
    COMPLETED = "completed"
    CANCELED = "canceled"


class Priority(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ColorToken(StrEnum):
    GRAY = "gray"
    BLUE = "blue"
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"
    PURPLE = "purple"
    PINK = "pink"


@dataclass(frozen=True, slots=True)
class Project:
    id: ProjectId
    workspace_id: str
    key: str
    name: str
    description: str
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class Workflow:
    id: WorkflowId
    workspace_id: str
    project_id: ProjectId
    name: str
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStatus:
    id: StatusId
    workspace_id: str
    workflow_id: WorkflowId
    name: str
    category: StatusCategory
    color_token: ColorToken
    position: int
    created_at: str
    updated_at: str
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class Label:
    id: LabelId
    workspace_id: str
    project_id: ProjectId
    name: str
    color_token: ColorToken
    created_at: str
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    id: IssueId
    workspace_id: str
    project_id: ProjectId
    number: int
    title: str
    description_source: str
    priority: Priority
    status_id: StatusId
    reporter_user_id: str
    rank: int
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None = None
    assignee_user_ids: tuple[str, ...] = ()
    labels: tuple[Label, ...] = ()


@dataclass(frozen=True, slots=True)
class Comment:
    id: CommentId
    workspace_id: str
    issue_id: IssueId
    author_user_id: str
    body_source: str
    revision: int
    created_at: str
    updated_at: str
    redacted_at: str | None = None


@dataclass(frozen=True, slots=True)
class SavedView:
    id: SavedViewId
    workspace_id: str
    project_id: ProjectId
    owner_user_id: str | None
    name: str
    schema_version: int
    filters: MappingProxyType[str, object]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Activity:
    id: ActivityId
    workspace_id: str
    project_id: ProjectId
    issue_id: IssueId | None
    actor_user_id: str
    action: str
    resource_kind: str
    resource_id: str
    metadata: MappingProxyType[str, str | int | bool | None]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class IssuePage:
    items: tuple[Issue, ...]
    next_cursor: str | None
