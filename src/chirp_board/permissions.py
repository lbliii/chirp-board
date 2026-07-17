"""Exact Board permissions compiled from Workspace Core roles."""

from enum import StrEnum

from chirp_workspace_core import AuthorizationError, Role, WorkspacePrincipal


class BoardPermission(StrEnum):
    READ = "board:read"
    ISSUE_CREATE = "board:issue:create"
    ISSUE_UPDATE = "board:issue:update"
    ISSUE_MOVE = "board:issue:move"
    ISSUE_ASSIGN = "board:issue:assign"
    ISSUE_ARCHIVE = "board:issue:archive"
    COMMENT_CREATE = "board:comment:create"
    COMMENT_REDACT_OWN = "board:comment:redact:own"
    COMMENT_REDACT_ANY = "board:comment:redact:any"
    PERSONAL_VIEW_MANAGE = "board:view:personal:manage"
    PROJECT_VIEW_MANAGE = "board:view:project:manage"
    PROJECT_MANAGE = "board:project:manage"
    WORKFLOW_MANAGE = "board:workflow:manage"


_VIEWER = frozenset({BoardPermission.READ})
_MEMBER = _VIEWER | {
    BoardPermission.ISSUE_CREATE,
    BoardPermission.ISSUE_UPDATE,
    BoardPermission.ISSUE_MOVE,
    BoardPermission.ISSUE_ASSIGN,
    BoardPermission.ISSUE_ARCHIVE,
    BoardPermission.COMMENT_CREATE,
    BoardPermission.COMMENT_REDACT_OWN,
    BoardPermission.PERSONAL_VIEW_MANAGE,
}
_ADMIN = _MEMBER | {
    BoardPermission.COMMENT_REDACT_ANY,
    BoardPermission.PROJECT_VIEW_MANAGE,
    BoardPermission.PROJECT_MANAGE,
    BoardPermission.WORKFLOW_MANAGE,
}


def permissions_for_role(role: Role) -> frozenset[BoardPermission]:
    if role is Role.VIEWER:
        return _VIEWER
    if role is Role.MEMBER:
        return _MEMBER
    if role in {Role.ADMIN, Role.OWNER}:
        return _ADMIN
    return frozenset()


def require_board_permission(principal: WorkspacePrincipal, permission: BoardPermission) -> None:
    if permission not in permissions_for_role(principal.membership.role):
        raise AuthorizationError(
            f"Workspace role {principal.membership.role.value!r} does not grant "
            f"Board permission {permission.value!r}."
        )
