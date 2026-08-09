"""Production-shaped Chirp Board application using typed returns and named blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from chirp.data import Database, migrate
from chirp.middleware.auth import AuthMiddleware
from chirp.middleware.csrf import CSRFMiddleware
from chirp.middleware.security_headers import SecurityHeadersConfig, SecurityHeadersMiddleware
from chirp.middleware.sessions import SessionConfig, SessionMiddleware, get_session
from chirp_workspace_core import (
    AuthenticationError,
    AuthorizationError,
    Breadcrumb,
    ConflictError,
    NavigationItem,
    NotFoundError,
    ShellCommand,
    WorkspaceChoice,
    WorkspaceCoreError,
    WorkspacePrincipal,
    WorkspaceRepository,
    build_shell_context,
    chirp_auth_config,
    register_shell_assets,
    workspace_templates_dir,
)
from chirp_workspace_core import (
    ValidationError as CoreValidationError,
)
from chirp_workspace_core.migrations import migration_directory as core_migrations

from chirp_board import (
    BoardPermission,
    BoardRepository,
    Priority,
    permissions_for_role,
    seed_demo,
)
from chirp_board.migrations import migration_directory as board_migrations

from .filters import IssueFilters, filters_from_saved, parse_filters
from .settings import STATIC, TEMPLATES, BoardSettings

if TYPE_CHECKING:
    from chirp.app import App
    from chirp.config import AppConfig
    from chirp.health import HealthCheck
    from chirp.http.request import Request
    from chirp.http.response import Redirect, Response
    from chirp.middleware.auth import get_user, login, logout
    from chirp.templating.returns import FormAction, Fragment, Page, Template, ValidationError
else:
    from chirp import (
        App,
        AppConfig,
        FormAction,
        Fragment,
        HealthCheck,
        Page,
        Redirect,
        Request,
        Response,
        Template,
        ValidationError,
        get_user,
        login,
        logout,
    )

PRODUCT_NAME = "Chirp Board"
LAST_VIEW_SESSION_KEY = "board_last_view"


@dataclass(frozen=True, slots=True)
class BoardRuntime:
    """Runnable Board app plus the repositories tests and routes share."""

    app: App
    database: Database = field(repr=False)
    identities: WorkspaceRepository = field(repr=False)
    board: BoardRepository = field(repr=False)
    settings: BoardSettings = field(repr=False)


def _form_text(form: object, name: str, default: str = "") -> str:
    getter = getattr(form, "get", None)
    value = getter(name, default) if callable(getter) else default
    return value if isinstance(value, str) else default


def _form_list(form: object, name: str) -> tuple[str, ...]:
    getter = getattr(form, "getlist", None)
    if callable(getter):
        values = getter(name)
        if isinstance(values, list | tuple):
            return tuple(item for item in values if isinstance(item, str) and item)
    raw = _form_text(form, name)
    return (raw,) if raw else ()


def _issue_key(project_key: str, number: int) -> str:
    return f"{project_key}-{number}"


def create_board_runtime(
    settings: BoardSettings | None = None,
    *,
    config: AppConfig | None = None,
) -> BoardRuntime:
    """Build the Board application without creating external network state."""

    resolved = settings or BoardSettings.from_env()
    app_config = config or AppConfig.from_env(
        template_dir=TEMPLATES,
        component_dirs=(workspace_templates_dir(),),
        static_dir=STATIC,
        secret_key=resolved.secret_key,
        csp_nonce_enabled=True,
        strict_transport_security="max-age=86400",
        workers=1,
        worker_mode="async",
        htmx=True,
    )
    database = Database(resolved.database_url)
    app = App(config=app_config, db=database)
    identities = WorkspaceRepository(database)
    board = BoardRepository(database)

    app.add_middleware(
        SecurityHeadersMiddleware(
            SecurityHeadersConfig(
                content_security_policy=None,
                strict_transport_security=app_config.strict_transport_security,
            )
        )
    )
    app.add_middleware(SessionMiddleware(SessionConfig(secret_key=resolved.secret_key)))
    app.add_middleware(AuthMiddleware(chirp_auth_config(identities)))
    app.add_middleware(CSRFMiddleware())
    register_shell_assets(app)

    app.add_health_check(
        HealthCheck(
            "board-schema",
            check=_schema_probe(identities),
            message="Board/Workspace schema is unavailable or incompatible",
        )
    )

    def remember_view(project_id: str, view: str) -> None:
        session = get_session()
        stored = session.get(LAST_VIEW_SESSION_KEY)
        mapping = dict(stored) if isinstance(stored, dict) else {}
        mapping[project_id] = view
        session[LAST_VIEW_SESSION_KEY] = mapping

    def recalled_view(project_id: str) -> str:
        session = get_session()
        stored = session.get(LAST_VIEW_SESSION_KEY)
        if isinstance(stored, dict):
            value = stored.get(project_id)
            if value in {"board", "list"}:
                return value
        return "board"

    async def current_principal(workspace_id: str) -> WorkspacePrincipal | None:
        user = get_user()
        if not getattr(user, "is_authenticated", False):
            return None
        try:
            return await identities.principal(user_id=str(user.id), workspace_id=workspace_id)
        except AuthorizationError, NotFoundError:
            return None

    async def require_principal(workspace_id: str) -> WorkspacePrincipal | Response | Redirect:
        principal = await current_principal(workspace_id)
        if principal is None:
            user = get_user()
            if not getattr(user, "is_authenticated", False):
                return Redirect(f"/login?next=/workspaces/{workspace_id}")
            return Response("Not authorized for this workspace.", status=403)
        return principal

    def can(principal: WorkspacePrincipal, permission: BoardPermission) -> bool:
        return permission in permissions_for_role(principal.membership.role)

    async def shell_for(
        principal: WorkspacePrincipal,
        *,
        breadcrumbs: tuple[Breadcrumb, ...],
        active: str,
        commands: tuple[ShellCommand, ...] = (),
        product_navigation: tuple[NavigationItem, ...] = (),
    ):
        principals = await identities.principals_for_user(str(principal.user.id))
        base = f"/workspaces/{principal.workspace.id}"
        return build_shell_context(
            principal,
            product_name=PRODUCT_NAME,
            primary_navigation=(
                NavigationItem("Projects", base, "board", active=active == "projects"),
            ),
            product_navigation=product_navigation,
            workspace_choices=tuple(
                WorkspaceChoice(
                    candidate.workspace.id,
                    candidate.workspace.name,
                    f"/workspaces/{candidate.workspace.id}",
                    current=candidate.workspace.id == principal.workspace.id,
                )
                for candidate in principals
            ),
            breadcrumbs=breadcrumbs,
            commands=commands,
            commands_url=f"{base}/commands",
        )

    async def project_nav(principal: WorkspacePrincipal, project_id: str, *, active: str):
        project = await board.project(principal, project_id)
        base = f"/workspaces/{principal.workspace.id}/projects/{project.id}"
        return (
            NavigationItem("Board", f"{base}/board", "board", active=active == "board"),
            NavigationItem("List", f"{base}/list", "board", active=active == "list"),
            NavigationItem("Activity", f"{base}/activity", "board", active=active == "activity"),
        ), project

    async def known_members(principal: WorkspacePrincipal, project_id: str) -> list[dict[str, str]]:
        """Collect assignable people from Board projections plus the current user.

        Workspace Core does not yet expose a membership directory API. Board keeps
        authorization checks in the repository (current members only) and offers the
        people already visible in this project's issues for progressive forms.
        """

        page = await board.list_issues(
            principal, project_id=project_id, include_archived=True, limit=100
        )
        ids = {str(principal.user.id)}
        for issue in page.items:
            ids.add(issue.reporter_user_id)
            ids.update(issue.assignee_user_ids)
        members: list[dict[str, str]] = []
        for user_id in sorted(ids):
            user = await identities.load_user(user_id)
            if user is None:
                continue
            members.append({"id": str(user.id), "display_name": user.display_name})
        return members

    async def load_issue_bundle(
        principal: WorkspacePrincipal, project_id: str, filters: IssueFilters
    ):
        project = await board.project(principal, project_id)
        workflow, statuses = await board.workflow(principal, project_id)
        labels = await board.labels(principal, project_id=project_id)
        page = await board.list_issues(
            principal,
            project_id=project_id,
            query=filters.query,
            status_ids=filters.status_ids,
            priorities=filters.priorities,
            assignee_ids=filters.assignee_ids,
            label_ids=filters.label_ids,
            include_archived=filters.include_archived,
            cursor=filters.cursor,
            limit=50,
        )
        issues = list(page.items)
        if filters.sort == "rank":
            issues.sort(key=lambda item: (item.rank, item.number, item.id))
        elif filters.sort == "created_desc":
            issues.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        elif filters.sort == "priority_desc":
            order = {p: idx for idx, p in enumerate(reversed(list(Priority)))}
            issues.sort(key=lambda item: (order.get(item.priority, 0), item.number), reverse=True)
        active_statuses = tuple(item for item in statuses if item.archived_at is None)
        columns: list[dict[str, object]] = []
        for status in active_statuses:
            column_issues = [item for item in issues if item.status_id == status.id]
            column_issues.sort(key=lambda item: (item.rank, item.number, item.id))
            columns.append({"status": status, "issues": column_issues, "count": len(column_issues)})
        counts = {
            status.id: sum(1 for issue in issues if issue.status_id == status.id)
            for status in active_statuses
        }
        saved = await board.saved_views(principal, project_id=project_id)
        activity = await board.activity(principal, project_id=project_id, limit=12)
        members = await known_members(principal, project_id)
        next_page_url = None
        if page.next_cursor:
            next_filters = IssueFilters(
                query=filters.query,
                status_ids=filters.status_ids,
                priorities=filters.priorities,
                assignee_ids=filters.assignee_ids,
                label_ids=filters.label_ids,
                include_archived=filters.include_archived,
                cursor=page.next_cursor,
                view=filters.view,
                sort=filters.sort,
                group_by=filters.group_by,
            )
            next_page_url = (
                f"/workspaces/{principal.workspace.id}/projects/{project.id}/"
                f"{filters.view}{next_filters.query_string()}"
            )
        return {
            "project": project,
            "workflow": workflow,
            "statuses": active_statuses,
            "all_statuses": statuses,
            "labels": labels,
            "issues": issues,
            "columns": columns,
            "counts": counts,
            "page": page,
            "next_page_url": next_page_url,
            "saved_views": saved,
            "activity": activity,
            "members": members,
            "filters": filters,
            "can_create": can(principal, BoardPermission.ISSUE_CREATE),
            "can_update": can(principal, BoardPermission.ISSUE_UPDATE),
            "can_move": can(principal, BoardPermission.ISSUE_MOVE),
            "can_assign": can(principal, BoardPermission.ISSUE_ASSIGN),
            "can_archive": can(principal, BoardPermission.ISSUE_ARCHIVE),
            "can_comment": can(principal, BoardPermission.COMMENT_CREATE),
            "can_personal_view": can(principal, BoardPermission.PERSONAL_VIEW_MANAGE),
            "can_project_view": can(principal, BoardPermission.PROJECT_VIEW_MANAGE),
            "can_manage_project": can(principal, BoardPermission.PROJECT_MANAGE),
            "priorities": list(Priority),
            "issue_key": _issue_key,
            "query_string": filters.query_string(),
            "base_url": f"/workspaces/{principal.workspace.id}/projects/{project.id}",
        }

    def project_page(template: str, block: str, shell, **ctx) -> Page:
        ctx.setdefault("errors", None)
        ctx.setdefault("form", None)
        return Page(template, block, shell=shell, product_name=PRODUCT_NAME, **ctx)

    def conflict_page(message: str, *, reload_url: str) -> Response:
        html = app.render(
            Template(
                "error.html",
                title="Conflict",
                message=message,
                reload_url=reload_url,
            )
        )
        return Response(html, status=409)

    def validation_form(
        template: str,
        block: str,
        *,
        errors: dict[str, str],
        **ctx: Any,
    ) -> ValidationError:
        return ValidationError(template, block, errors=errors, **ctx)

    # ------------------------------------------------------------------ routes
    @app.route("/")
    async def home() -> Redirect:
        user = get_user()
        if not getattr(user, "is_authenticated", False):
            state = await identities.bootstrap_state()
            if state.completed_at is None:
                return Redirect("/setup")
            return Redirect("/login")
        principals = await identities.principals_for_user(str(user.id))
        if not principals:
            return Redirect("/login")
        return Redirect(f"/workspaces/{principals[0].workspace.id}")

    @app.route("/setup", methods=["GET", "POST"])
    async def setup(request: Request):
        state = await identities.bootstrap_state()
        if state.completed_at is not None:
            return Redirect("/login")
        error = ""
        if request.method == "POST":
            form = await request.form()
            try:
                result = await identities.bootstrap_first_owner(
                    expected_setup_token=resolved.setup_token,
                    supplied_setup_token=_form_text(form, "setup_token"),
                    email=_form_text(form, "email"),
                    display_name=_form_text(form, "display_name"),
                    password=_form_text(form, "password"),
                    workspace_slug=_form_text(form, "workspace_slug") or "primary",
                    workspace_name=_form_text(form, "workspace_name") or "Primary Workspace",
                )
                login(result.user)
                return Redirect(f"/workspaces/{result.workspace.id}")
            except (AuthenticationError, CoreValidationError, WorkspaceCoreError) as exc:
                error = str(exc)
        html = app.render(Template("public.html", mode="setup", error=error))
        return Response(html, status=400 if error else 200)

    @app.route("/login", methods=["GET", "POST"])
    async def login_page(request: Request):
        error = ""
        next_url = request.query.get("next", "/")
        if (
            not isinstance(next_url, str)
            or not next_url.startswith("/")
            or next_url.startswith("//")
        ):
            next_url = "/"
        if request.method == "POST":
            form = await request.form()
            next_url = _form_text(form, "next", next_url) or "/"
            user = await identities.authenticate(
                _form_text(form, "email"),
                _form_text(form, "password"),
            )
            if user is None:
                error = "Email or password is incorrect."
            else:
                login(user)
                return Redirect(next_url)
        html = app.render(Template("public.html", mode="login", error=error, next_url=next_url))
        return Response(html, status=401 if error else 200)

    @app.route("/logout", methods=["POST"])
    async def logout_page() -> Redirect:
        logout()
        return Redirect("/login")

    @app.route("/workspaces/{workspace_id}")
    async def workspace_home(request: Request, workspace_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        projects = await board.projects(principal)
        shell = await shell_for(
            principal,
            breadcrumbs=(Breadcrumb(principal.workspace.name), Breadcrumb("Projects")),
            active="projects",
            commands=(
                ShellCommand(
                    "new-project", "Create project", f"/workspaces/{workspace_id}#new-project"
                )
                if can(principal, BoardPermission.PROJECT_MANAGE)
                else ShellCommand("projects", "Projects", f"/workspaces/{workspace_id}"),
            ),
        )
        return project_page(
            "home.html",
            "page_content",
            shell,
            projects=projects,
            can_manage_project=can(principal, BoardPermission.PROJECT_MANAGE),
            workspace_id=workspace_id,
            empty=not projects,
        )

    @app.route("/workspaces/{workspace_id}/commands")
    async def commands_page(workspace_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        shell = await shell_for(
            principal,
            breadcrumbs=(
                Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                Breadcrumb("Commands"),
            ),
            active="projects",
            commands=(ShellCommand("projects", "Projects", f"/workspaces/{workspace_id}"),),
        )
        return project_page("commands.html", "page_content", shell, workspace_id=workspace_id)

    @app.route("/workspaces/{workspace_id}/projects", methods=["POST"])
    async def create_project(request: Request, workspace_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        try:
            project, _workflow, _statuses = await board.create_project(
                principal,
                key=_form_text(form, "key"),
                name=_form_text(form, "name"),
                description=_form_text(form, "description"),
            )
        except CoreValidationError as exc:
            projects = await board.projects(principal)
            shell = await shell_for(
                principal,
                breadcrumbs=(Breadcrumb(principal.workspace.name), Breadcrumb("Projects")),
                active="projects",
            )
            return validation_form(
                "home.html",
                "project_form",
                errors={"form": str(exc)},
                shell=shell,
                projects=projects,
                can_manage_project=True,
                workspace_id=workspace_id,
                form={
                    "key": _form_text(form, "key"),
                    "name": _form_text(form, "name"),
                    "description": _form_text(form, "description"),
                },
                empty=not projects,
            )
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"/workspaces/{workspace_id}")
        if _form_text(form, "seed_demo") in {"1", "on", "true"}:
            await seed_demo(board, principal)
        return FormAction(f"/workspaces/{workspace_id}/projects/{project.id}/board")

    @app.route("/workspaces/{workspace_id}/projects/{project_id}")
    async def project_landing(workspace_id: str, project_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        view = recalled_view(project_id)
        return Redirect(f"/workspaces/{workspace_id}/projects/{project_id}/{view}")

    async def _render_project_view(
        request: Request,
        workspace_id: str,
        project_id: str,
        *,
        view: str,
    ):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        filters = parse_filters(request.query, default_view=view)
        filters = IssueFilters(
            query=filters.query,
            status_ids=filters.status_ids,
            priorities=filters.priorities,
            assignee_ids=filters.assignee_ids,
            label_ids=filters.label_ids,
            include_archived=filters.include_archived,
            cursor=filters.cursor,
            view=view,
            sort=filters.sort,
            group_by=filters.group_by,
        )
        remember_view(project_id, view)
        try:
            ctx = await load_issue_bundle(principal, project_id, filters)
        except NotFoundError:
            return Response("Project not found.", status=404)
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except CoreValidationError as exc:
            return Response(str(exc), status=400)
        product_nav, project = await project_nav(principal, project_id, active=view)
        shell = await shell_for(
            principal,
            breadcrumbs=(
                Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                Breadcrumb(project.name),
                Breadcrumb(view.title()),
            ),
            active="projects",
            product_navigation=product_nav,
            commands=(
                ShellCommand(
                    "create-issue",
                    "Create issue",
                    f"{ctx['base_url']}/issues/new",
                    shortcut="c",
                    required_permissions=frozenset(),
                ),
                ShellCommand(
                    "search", "Focus search", f"{ctx['base_url']}/{view}#board-search", shortcut="/"
                ),
            ),
        )
        template = "board.html" if view == "board" else "list.html"
        block = "board_view" if view == "board" else "list_view"
        return project_page(template, block, shell, **ctx)

    @app.route("/workspaces/{workspace_id}/projects/{project_id}/board")
    async def board_view(request: Request, workspace_id: str, project_id: str):
        return await _render_project_view(request, workspace_id, project_id, view="board")

    @app.route("/workspaces/{workspace_id}/projects/{project_id}/list")
    async def list_view(request: Request, workspace_id: str, project_id: str):
        return await _render_project_view(request, workspace_id, project_id, view="list")

    @app.route("/workspaces/{workspace_id}/projects/{project_id}/activity")
    async def activity_view(workspace_id: str, project_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        try:
            project = await board.project(principal, project_id)
            activity = await board.activity(principal, project_id=project_id, limit=100)
        except NotFoundError:
            return Response("Project not found.", status=404)
        product_nav, _project = await project_nav(principal, project_id, active="activity")
        shell = await shell_for(
            principal,
            breadcrumbs=(
                Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                Breadcrumb(project.name, f"/workspaces/{workspace_id}/projects/{project_id}"),
                Breadcrumb("Activity"),
            ),
            active="projects",
            product_navigation=product_nav,
        )
        return project_page(
            "activity.html",
            "page_content",
            shell,
            project=project,
            activity=activity,
            base_url=f"/workspaces/{workspace_id}/projects/{project_id}",
        )

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/new", methods=["GET", "POST"]
    )
    async def create_issue(request: Request, workspace_id: str, project_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        try:
            project = await board.project(principal, project_id)
            _workflow, statuses = await board.workflow(principal, project_id)
            labels = await board.labels(principal, project_id=project_id)
        except NotFoundError:
            return Response("Project not found.", status=404)
        active_statuses = tuple(item for item in statuses if item.archived_at is None)
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        form_values = {
            "title": "",
            "description_source": "",
            "priority": Priority.NONE.value,
            "status_id": active_statuses[0].id if active_statuses else "",
        }
        if request.method == "GET":
            product_nav, _project = await project_nav(principal, project_id, active="board")
            shell = await shell_for(
                principal,
                breadcrumbs=(
                    Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                    Breadcrumb(project.name, base),
                    Breadcrumb("New issue"),
                ),
                active="projects",
                product_navigation=product_nav,
            )
            return project_page(
                "issue_form.html",
                "issue_form",
                shell,
                project=project,
                statuses=active_statuses,
                labels=labels,
                priorities=list(Priority),
                form=form_values,
                base_url=base,
                can_create=can(principal, BoardPermission.ISSUE_CREATE),
                mode="create",
            )
        form = await request.form()
        form_values = {
            "title": _form_text(form, "title"),
            "description_source": _form_text(form, "description_source"),
            "priority": _form_text(form, "priority", Priority.NONE.value),
            "status_id": _form_text(form, "status_id"),
        }
        try:
            priority = Priority(form_values["priority"])
        except ValueError:
            priority = Priority.NONE
            form_values["priority"] = Priority.NONE.value
        try:
            issue = await board.create_issue(
                principal,
                project_id=project_id,
                title=form_values["title"],
                description_source=form_values["description_source"],
                priority=priority,
                status_id=form_values["status_id"] or None,
            )
        except CoreValidationError as exc:
            product_nav, _project = await project_nav(principal, project_id, active="board")
            shell = await shell_for(
                principal,
                breadcrumbs=(
                    Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                    Breadcrumb(project.name, base),
                    Breadcrumb("New issue"),
                ),
                active="projects",
                product_navigation=product_nav,
            )
            return validation_form(
                "issue_form.html",
                "issue_form",
                errors={"form": str(exc)},
                shell=shell,
                project=project,
                statuses=active_statuses,
                labels=labels,
                priorities=list(Priority),
                form=form_values,
                base_url=base,
                can_create=True,
                mode="create",
            )
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/board")
        return FormAction(f"{base}/issues/{issue.id}")

    @app.route("/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}")
    async def issue_detail(workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        try:
            project = await board.project(principal, project_id)
            issue = await board.get_issue(principal, issue_id)
            if issue.project_id != project.id:
                return Response("Issue not found in this project.", status=404)
            _workflow, statuses = await board.workflow(principal, project_id)
            labels = await board.labels(principal, project_id=project_id)
            comments = await board.comments(principal, issue_id)
            activity = await board.activity(
                principal, project_id=project_id, issue_id=issue_id, limit=50
            )
            members = await known_members(principal, project_id)
        except NotFoundError:
            return Response("Issue not found.", status=404)
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        product_nav, _project = await project_nav(principal, project_id, active="board")
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        shell = await shell_for(
            principal,
            breadcrumbs=(
                Breadcrumb(principal.workspace.name, f"/workspaces/{workspace_id}"),
                Breadcrumb(project.name, base),
                Breadcrumb(_issue_key(project.key, issue.number)),
            ),
            active="projects",
            product_navigation=product_nav,
        )
        return project_page(
            "issue.html",
            "issue_detail",
            shell,
            project=project,
            issue=issue,
            statuses=tuple(item for item in statuses if item.archived_at is None),
            labels=labels,
            comments=comments,
            activity=activity,
            members=members,
            priorities=list(Priority),
            base_url=base,
            issue_url=f"{base}/issues/{issue.id}",
            issue_key=_issue_key(project.key, issue.number),
            can_update=can(principal, BoardPermission.ISSUE_UPDATE),
            can_move=can(principal, BoardPermission.ISSUE_MOVE),
            can_assign=can(principal, BoardPermission.ISSUE_ASSIGN),
            can_archive=can(principal, BoardPermission.ISSUE_ARCHIVE),
            can_comment=can(principal, BoardPermission.COMMENT_CREATE),
            current_user_id=str(principal.user.id),
            issue_label_ids=frozenset(str(label.id) for label in issue.labels),
        )

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/move",
        methods=["POST"],
    )
    async def move_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        filters = parse_filters(form, default_view=_form_text(form, "view", "board") or "board")
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            expected = int(_form_text(form, "expected_revision", "0") or "0")
            await board.move_issue(
                principal,
                issue_id,
                expected_revision=expected,
                status_id=_form_text(form, "status_id"),
                before_issue_id=_form_text(form, "before_issue_id") or None,
            )
        except CoreValidationError as exc:
            return Response(str(exc), status=422)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/issues/{issue_id}")
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except NotFoundError:
            return Response("Issue not found.", status=404)
        if request.headers.get("HX-Request"):
            ctx = await load_issue_bundle(principal, project_id, filters)
            ctx.setdefault("errors", None)
            ctx.setdefault("form", None)
            return Fragment("board.html", "board_view", **ctx)
        return FormAction(f"{base}/{filters.view}{filters.query_string()}")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/edit",
        methods=["POST"],
    )
    async def edit_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            priority = Priority(_form_text(form, "priority", Priority.NONE.value))
        except ValueError:
            priority = Priority.NONE
        try:
            issue = await board.update_issue(
                principal,
                issue_id,
                expected_revision=int(_form_text(form, "expected_revision", "0") or "0"),
                title=_form_text(form, "title"),
                description_source=_form_text(form, "description_source"),
                priority=priority,
            )
        except CoreValidationError as exc:
            project = await board.project(principal, project_id)
            current = await board.get_issue(principal, issue_id)
            _workflow, statuses = await board.workflow(principal, project_id)
            return validation_form(
                "issue.html",
                "issue_edit_form",
                errors={"form": str(exc)},
                project=project,
                issue=current,
                statuses=tuple(item for item in statuses if item.archived_at is None),
                priorities=list(Priority),
                form={
                    "title": _form_text(form, "title"),
                    "description_source": _form_text(form, "description_source"),
                    "priority": _form_text(form, "priority"),
                },
                base_url=base,
                issue_url=f"{base}/issues/{issue_id}",
                issue_key=_issue_key(project.key, current.number),
                can_update=True,
            )
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/issues/{issue_id}")
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        return FormAction(f"{base}/issues/{issue.id}")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/assign",
        methods=["POST"],
    )
    async def assign_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            await board.replace_assignees(
                principal,
                issue_id,
                expected_revision=int(_form_text(form, "expected_revision", "0") or "0"),
                user_ids=_form_list(form, "assignee"),
            )
        except CoreValidationError as exc:
            return Response(str(exc), status=422)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/issues/{issue_id}")
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        return FormAction(f"{base}/issues/{issue_id}")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/labels",
        methods=["POST"],
    )
    async def label_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            await board.replace_labels(
                principal,
                issue_id,
                expected_revision=int(_form_text(form, "expected_revision", "0") or "0"),
                label_ids=_form_list(form, "label"),
            )
        except CoreValidationError as exc:
            return Response(str(exc), status=422)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/issues/{issue_id}")
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        return FormAction(f"{base}/issues/{issue_id}")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/comment",
        methods=["POST"],
    )
    async def comment_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        body = _form_text(form, "body_source")
        try:
            await board.add_comment(principal, issue_id, body_source=body)
        except CoreValidationError as exc:
            return validation_form(
                "issue.html",
                "comment_form",
                errors={"form": str(exc)},
                form={"body_source": body},
                issue_url=f"{base}/issues/{issue_id}",
                can_comment=True,
            )
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except NotFoundError:
            return Response("Issue not found.", status=404)
        return FormAction(f"{base}/issues/{issue_id}#comments")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/subscribe",
        methods=["POST"],
    )
    async def subscribe_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        subscribed = _form_text(form, "subscribed", "1") in {"1", "on", "true"}
        await board.set_subscription(principal, issue_id, subscribed=subscribed)
        return FormAction(f"/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/issues/{issue_id}/archive",
        methods=["POST"],
    )
    async def archive_issue(request: Request, workspace_id: str, project_id: str, issue_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        archived = _form_text(form, "archived", "1") in {"1", "on", "true"}
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            await board.set_issue_archived(
                principal,
                issue_id,
                expected_revision=int(_form_text(form, "expected_revision", "0") or "0"),
                archived=archived,
            )
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/issues/{issue_id}")
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        return FormAction(f"{base}/board")

    @app.route(
        "/workspaces/{workspace_id}/projects/{project_id}/views",
        methods=["POST"],
    )
    async def save_view(request: Request, workspace_id: str, project_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        form = await request.form()
        filters = parse_filters(form, default_view=_form_text(form, "view", "board") or "board")
        shared = _form_text(form, "shared") in {"1", "on", "true"}
        base = f"/workspaces/{workspace_id}/projects/{project_id}"
        try:
            view = await board.save_view(
                principal,
                project_id=project_id,
                name=_form_text(form, "name"),
                filters=filters.saved_view_payload(),
                shared=shared,
            )
        except CoreValidationError as exc:
            return Response(str(exc), status=422)
        except AuthorizationError:
            return Response("Not authorized.", status=403)
        except ConflictError as exc:
            return conflict_page(str(exc), reload_url=f"{base}/{filters.view}")
        applied = filters_from_saved(view.filters, view=filters.view)
        return FormAction(f"{base}/{applied.view}{applied.query_string()}")

    @app.route("/workspaces/{workspace_id}/projects/{project_id}/views/{view_id}")
    async def open_saved_view(workspace_id: str, project_id: str, view_id: str):
        principal = await require_principal(workspace_id)
        if not isinstance(principal, WorkspacePrincipal):
            return principal
        views = await board.saved_views(principal, project_id=project_id)
        match = next((item for item in views if item.id == view_id), None)
        if match is None:
            return Response("Saved view not found.", status=404)
        applied = filters_from_saved(match.filters, view="list")
        return Redirect(
            f"/workspaces/{workspace_id}/projects/{project_id}/{applied.view}{applied.query_string()}"
        )

    @app.on_startup
    async def apply_migrations() -> None:
        await migrate(database, core_migrations())
        await migrate(database, board_migrations())

    return BoardRuntime(app, database, identities, board, resolved)


def _schema_probe(identities: WorkspaceRepository):
    async def ready() -> bool:
        try:
            await identities.bootstrap_state()
        except WorkspaceCoreError:
            return False
        return True

    return ready
