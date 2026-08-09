"""Bookmarkable Board filter and view-state helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from chirp_board import Priority


@dataclass(frozen=True, slots=True)
class IssueFilters:
    """Typed filter state encoded in ordinary query parameters."""

    query: str = ""
    status_ids: tuple[str, ...] = ()
    priorities: tuple[Priority, ...] = ()
    assignee_ids: tuple[str, ...] = ()
    label_ids: tuple[str, ...] = ()
    include_archived: bool = False
    cursor: str | None = None
    view: str = "board"
    sort: str = "updated_desc"
    group_by: str = "status"

    def as_query(self, *, omit_cursor: bool = False) -> dict[str, str | list[str]]:
        """Return a mapping suitable for urlencode / form hidden fields."""

        params: dict[str, str | list[str]] = {"view": self.view}
        if self.query:
            params["q"] = self.query
        if self.status_ids:
            params["status"] = list(self.status_ids)
        if self.priorities:
            params["priority"] = [item.value for item in self.priorities]
        if self.assignee_ids:
            params["assignee"] = list(self.assignee_ids)
        if self.label_ids:
            params["label"] = list(self.label_ids)
        if self.include_archived:
            params["archived"] = "1"
        if self.sort and self.sort != "updated_desc":
            params["sort"] = self.sort
        if self.group_by and self.group_by != "status":
            params["group"] = self.group_by
        if self.cursor and not omit_cursor:
            params["cursor"] = self.cursor
        return params

    def query_string(self, *, omit_cursor: bool = False) -> str:
        """Return a bookmarkable query string including a leading '?' when non-empty."""

        encoded = urlencode(self.as_query(omit_cursor=omit_cursor), doseq=True)
        return f"?{encoded}" if encoded else ""

    def saved_view_payload(self) -> dict[str, object]:
        """Return the validated saved-view filter schema payload."""

        return {
            "query": self.query,
            "status_ids": list(self.status_ids),
            "priorities": [item.value for item in self.priorities],
            "assignee_ids": list(self.assignee_ids),
            "label_ids": list(self.label_ids),
            "include_archived": self.include_archived,
            "sort": self.sort,
            "group_by": self.group_by,
        }


def _multi(params: Mapping[str, str], name: str) -> tuple[str, ...]:
    getter = getattr(params, "getlist", None)
    if callable(getter):
        values = getter(name)
        if isinstance(values, list | tuple):
            return tuple(item for item in values if isinstance(item, str) and item)
    raw = params.get(name, "")
    if not isinstance(raw, str) or not raw:
        return ()
    return tuple(part for part in raw.split(",") if part)


def parse_filters(params: Mapping[str, str], *, default_view: str = "board") -> IssueFilters:
    """Parse request query/form parameters into typed Board filters."""

    view = params.get("view", default_view)
    if view not in {"board", "list"}:
        view = default_view
    query = params.get("q", params.get("query", ""))
    if not isinstance(query, str):
        query = ""
    query = query.strip()[:100]
    priorities: list[Priority] = []
    for value in _multi(params, "priority"):
        try:
            priorities.append(Priority(value))
        except ValueError:
            continue
    include_archived = params.get("archived", "") in {"1", "true", "on", "yes"}
    sort = params.get("sort", "updated_desc")
    if sort not in {"updated_desc", "created_desc", "priority_desc", "rank"}:
        sort = "updated_desc"
    group_by = params.get("group", "status")
    if group_by not in {"status", "priority", "assignee", "none"}:
        group_by = "status"
    cursor = params.get("cursor") or None
    if cursor is not None and not isinstance(cursor, str):
        cursor = None
    return IssueFilters(
        query=query,
        status_ids=_multi(params, "status"),
        priorities=tuple(priorities),
        assignee_ids=_multi(params, "assignee"),
        label_ids=_multi(params, "label"),
        include_archived=include_archived,
        cursor=cursor,
        view=view,
        sort=sort,
        group_by=group_by,
    )


def filters_from_saved(filters: Mapping[str, object], *, view: str = "board") -> IssueFilters:
    """Hydrate IssueFilters from a typed saved-view payload."""

    query = filters.get("query", "")
    if not isinstance(query, str):
        query = ""
    status_ids = filters.get("status_ids", ())
    assignee_ids = filters.get("assignee_ids", ())
    label_ids = filters.get("label_ids", ())
    priorities_raw = filters.get("priorities", ())
    priorities: list[Priority] = []
    if isinstance(priorities_raw, list | tuple):
        for value in priorities_raw:
            if isinstance(value, str):
                try:
                    priorities.append(Priority(value))
                except ValueError:
                    continue
    include_archived = bool(filters.get("include_archived", False))
    sort = filters.get("sort", "updated_desc")
    if not isinstance(sort, str):
        sort = "updated_desc"
    group_by = filters.get("group_by", "status")
    if not isinstance(group_by, str):
        group_by = "status"

    def _ids(value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            return ()
        return tuple(item for item in value if isinstance(item, str) and item)

    return IssueFilters(
        query=query.strip()[:100],
        status_ids=_ids(status_ids),
        priorities=tuple(priorities),
        assignee_ids=_ids(assignee_ids),
        label_ids=_ids(label_ids),
        include_archived=include_archived,
        view=view if view in {"board", "list"} else "board",
        sort=sort
        if sort in {"updated_desc", "created_desc", "priority_desc", "rank"}
        else "updated_desc",
        group_by=group_by if group_by in {"status", "priority", "assignee", "none"} else "status",
    )
