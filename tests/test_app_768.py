"""Issue #768 acceptance for Board/list views and issue flows."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from chirp.config import AppConfig
from chirp.testing import TestClient
from chirp_workspace_core import Role, workspace_templates_dir

from board_app.application import BoardRuntime, create_board_runtime
from board_app.settings import STATIC, TEMPLATES, BoardSettings

pytestmark = pytest.mark.issue(768)

PASSWORD = "correct horse battery staple"


def extract_csrf(html: str) -> str:
    patterns = (
        r'name="_csrf_token" value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="_csrf_token"',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    raise AssertionError("rendered form did not include a CSRF token")


def response_cookie(response: object) -> str | None:
    for name, value in getattr(response, "headers", ()):
        if name.lower() == "set-cookie" and value.startswith("chirp_session="):
            return value.split(";", 1)[0].partition("=")[2]
    return None


def cookie_headers(cookie: str | None, **headers: str) -> dict[str, str]:
    if cookie is not None:
        headers["Cookie"] = f"chirp_session={cookie}"
    return headers


@dataclass(slots=True)
class RunningBoard:
    runtime: BoardRuntime
    client: TestClient
    workspace_id: str
    cookie: str


def build_runtime(database_url: str) -> BoardRuntime:
    settings = BoardSettings.for_tests(database_url)
    config = AppConfig(
        template_dir=TEMPLATES,
        component_dirs=(workspace_templates_dir(),),
        static_dir=STATIC,
        secret_key=settings.secret_key,
        csp_nonce_enabled=True,
        workers=1,
        worker_mode="async",
        htmx=True,
    )
    return create_board_runtime(settings, config=config)


async def bootstrap_owner(client: TestClient) -> tuple[str, str]:
    page = await client.get("/setup")
    assert page.status == 200
    token = extract_csrf(page.text)
    cookie = response_cookie(page)
    response = await client.post(
        "/setup",
        data={
            "setup_token": "local-board-setup-token-24chars",
            "email": "owner@example.test",
            "display_name": "Owner",
            "password": PASSWORD,
            "workspace_slug": "primary",
            "workspace_name": "Primary Workspace",
            "_csrf_token": token,
        },
        headers=cookie_headers(cookie),
    )
    assert response.status in {302, 303}
    location = response.header("location", "") or ""
    match = re.search(r"/workspaces/([^/]+)", location)
    assert match, location
    return match.group(1), response_cookie(response) or cookie or ""


async def login(
    client: TestClient, *, email: str, password: str = PASSWORD, cookie: str | None = None
) -> str:
    page = await client.get("/login", headers=cookie_headers(cookie))
    token = extract_csrf(page.text)
    current = response_cookie(page) or cookie
    response = await client.post(
        "/login",
        data={"email": email, "password": password, "_csrf_token": token, "next": "/"},
        headers=cookie_headers(current),
    )
    assert response.status in {302, 303}
    return response_cookie(response) or current or ""


async def post_form(
    board: RunningBoard,
    path: str,
    *,
    via: str,
    data: dict[str, str],
    htmx: bool = False,
    cookie: str | None = None,
):
    active = cookie if cookie is not None else board.cookie
    page = await board.client.get(via, headers=cookie_headers(active))
    assert page.status == 200, page.text[:500]
    token = extract_csrf(page.text)
    current = response_cookie(page) or active
    if cookie is None:
        board.cookie = current
    headers = cookie_headers(current)
    if htmx:
        headers["HX-Request"] = "true"
        headers["X-CSRF-Token"] = token
    response = await board.client.post(
        path,
        data={**data, "_csrf_token": token},
        headers=headers,
    )
    refreshed = response_cookie(response)
    if refreshed and cookie is None:
        board.cookie = refreshed
    return response


@pytest.fixture
async def board(tmp_path: Path) -> AsyncIterator[RunningBoard]:
    runtime = build_runtime(f"sqlite:///{tmp_path / 'board-app.db'}")
    async with TestClient(runtime.app) as client:
        workspace_id, cookie = await bootstrap_owner(client)
        yield RunningBoard(runtime, client, workspace_id, cookie)


async def create_project(board: RunningBoard, *, key: str = "ENG") -> str:
    response = await post_form(
        board,
        f"/workspaces/{board.workspace_id}/projects",
        via=f"/workspaces/{board.workspace_id}",
        data={"key": key, "name": f"{key} Project", "description": "Ship the workflow"},
    )
    assert response.status in {302, 303}, response.text
    location = response.header("location", "") or ""
    match = re.search(r"/projects/([^/]+)/", location)
    assert match, location
    return match.group(1)


async def test_home_board_list_and_empty_states(board: RunningBoard) -> None:
    home = await board.client.get(
        f"/workspaces/{board.workspace_id}",
        headers=cookie_headers(board.cookie),
    )
    assert home.status == 200
    assert "No projects yet" in home.text

    project_id = await create_project(board)
    board_page = await board.client.get(
        f"/workspaces/{board.workspace_id}/projects/{project_id}/board",
        headers=cookie_headers(board.cookie),
    )
    assert board_page.status == 200
    assert "This board is empty" in board_page.text
    assert 'id="board-search"' in board_page.text
    assert "Backlog" in board_page.text

    list_page = await board.client.get(
        f"/workspaces/{board.workspace_id}/projects/{project_id}/list",
        headers=cookie_headers(board.cookie),
    )
    assert list_page.status == 200
    assert "No issues yet" in list_page.text


async def test_create_issue_plain_and_htmx_move_preserve_filters(board: RunningBoard) -> None:
    project_id = await create_project(board)
    base = f"/workspaces/{board.workspace_id}/projects/{project_id}"

    created = await post_form(
        board,
        f"{base}/issues/new",
        via=f"{base}/issues/new",
        data={
            "title": "Triage keyboard path",
            "description_source": "Needs a normal-form move.",
            "priority": "high",
            "status_id": "",
        },
    )
    assert created.status in {302, 303}
    issue_url = created.header("location", "") or ""
    assert "/issues/" in issue_url

    detail = await board.client.get(issue_url, headers=cookie_headers(board.cookie))
    assert detail.status == 200
    assert "Triage keyboard path" in detail.text
    assert 'id="move-control"' in detail.text

    board_page = await board.client.get(f"{base}/board", headers=cookie_headers(board.cookie))
    status_ids = re.findall(r'<option value="([0-9a-f-]{36})"', board_page.text)
    assert len(status_ids) >= 2
    target_status = status_ids[1]
    revision = re.search(r'name="expected_revision" value="(\d+)"', board_page.text)
    assert revision
    issue_id = issue_url.rsplit("/", 1)[-1]

    moved = await post_form(
        board,
        f"{base}/issues/{issue_id}/move",
        via=f"{base}/board?q=Triage",
        data={
            "expected_revision": revision.group(1),
            "status_id": target_status,
            "view": "board",
            "q": "Triage",
        },
        htmx=True,
    )
    assert moved.status == 200
    assert "Triage keyboard path" in moved.text
    assert f'id="column-{target_status}"' in moved.text

    plain = await post_form(
        board,
        f"{base}/issues/{issue_id}/move",
        via=f"{base}/list",
        data={
            "expected_revision": str(int(revision.group(1)) + 1),
            "status_id": status_ids[0],
            "view": "list",
        },
    )
    assert plain.status in {302, 303}
    assert "/list" in (plain.header("location", "") or "")


async def test_validation_preserves_values_and_conflict_is_visible(board: RunningBoard) -> None:
    project_id = await create_project(board)
    base = f"/workspaces/{board.workspace_id}/projects/{project_id}"
    created = await post_form(
        board,
        f"{base}/issues/new",
        via=f"{base}/issues/new",
        data={"title": "Keep my draft", "description_source": "x", "priority": "low"},
    )
    issue_url = created.header("location", "") or ""
    detail = await board.client.get(issue_url, headers=cookie_headers(board.cookie))
    revision = re.search(r'name="expected_revision" value="(\d+)"', detail.text)
    assert revision

    invalid = await post_form(
        board,
        f"{issue_url}/edit",
        via=issue_url,
        data={
            "expected_revision": revision.group(1),
            "title": "",
            "description_source": "preserved body",
            "priority": "urgent",
        },
    )
    assert invalid.status == 422
    assert "preserved body" in invalid.text
    assert "urgent" in invalid.text

    conflict = await post_form(
        board,
        f"{issue_url}/edit",
        via=issue_url,
        data={
            "expected_revision": "999",
            "title": "Stale write",
            "description_source": "nope",
            "priority": "medium",
        },
    )
    assert conflict.status == 409
    assert "Reload the current state" in conflict.text


async def test_viewer_cannot_mutate_and_missing_blocks_stay_named(board: RunningBoard) -> None:
    project_id = await create_project(board)
    base = f"/workspaces/{board.workspace_id}/projects/{project_id}"
    created = await post_form(
        board,
        f"{base}/issues/new",
        via=f"{base}/issues/new",
        data={"title": "Protected controls", "description_source": "", "priority": "none"},
    )
    issue_url = created.header("location", "") or ""

    viewer = await board.runtime.identities.create_user(
        email="viewer@example.test",
        display_name="Viewer",
        password=PASSWORD,
    )
    owner_user = await board.runtime.identities.authenticate("owner@example.test", PASSWORD)
    assert owner_user is not None
    invitation = await board.runtime.identities.issue_invitation(
        actor_user_id=owner_user.id,
        workspace_id=board.workspace_id,
        email=viewer.email,
        role=Role.VIEWER,
    )
    await board.runtime.identities.accept_invitation(token=invitation.token, user_id=viewer.id)
    viewer_cookie = await login(board.client, email="viewer@example.test")

    detail = await board.client.get(issue_url, headers=cookie_headers(viewer_cookie))
    assert detail.status == 200
    assert "Protected controls" in detail.text
    assert "Move issue" not in detail.text
    assert "Add comment" not in detail.text

    denied = await post_form(
        board,
        f"{issue_url}/comment",
        via=issue_url,
        data={"body_source": "should fail"},
        cookie=viewer_cookie,
    )
    assert denied.status in {403, 422}

    board_src = (TEMPLATES / "board.html").read_text()
    list_src = (TEMPLATES / "list.html").read_text()
    issue_src = (TEMPLATES / "issue.html").read_text()
    assert "{% block board_view %}" in board_src
    assert "{% block status_counts %}" in board_src
    assert "{% block list_view %}" in list_src
    assert "{% block issue_detail %}" in issue_src
    assert "{% block comment_form %}" in issue_src


async def test_comment_assign_archive_saved_view_and_search(board: RunningBoard) -> None:
    project_id = await create_project(board)
    base = f"/workspaces/{board.workspace_id}/projects/{project_id}"
    created = await post_form(
        board,
        f"{base}/issues/new",
        via=f"{base}/issues/new",
        data={
            "title": "Findable issue",
            "description_source": "needle in a board",
            "priority": "medium",
        },
    )
    issue_url = created.header("location", "") or ""
    detail = await board.client.get(issue_url, headers=cookie_headers(board.cookie))
    revision = re.search(r'name="expected_revision" value="(\d+)"', detail.text)
    assert revision
    owner = await board.runtime.identities.authenticate("owner@example.test", PASSWORD)
    assert owner is not None

    commented = await post_form(
        board,
        f"{issue_url}/comment",
        via=issue_url,
        data={"body_source": "Looks good for ship."},
    )
    assert commented.status in {302, 303}

    assigned = await post_form(
        board,
        f"{issue_url}/assign",
        via=issue_url,
        data={"expected_revision": revision.group(1), "assignee": str(owner.id)},
    )
    assert assigned.status in {302, 303}

    detail = await board.client.get(issue_url, headers=cookie_headers(board.cookie))
    revision = re.search(r'name="expected_revision" value="(\d+)"', detail.text)
    assert revision
    assert "Looks good for ship." in detail.text

    saved = await post_form(
        board,
        f"{base}/views",
        via=f"{base}/list?q=Findable",
        data={"name": "Findable work", "view": "list", "q": "Findable"},
    )
    assert saved.status in {302, 303}
    assert "q=Findable" in (saved.header("location", "") or "")

    filtered = await board.client.get(
        f"{base}/board?q=Findable",
        headers=cookie_headers(board.cookie),
    )
    assert filtered.status == 200
    assert "Findable issue" in filtered.text

    missing = await board.client.get(
        f"{base}/board?q=no-such-issue",
        headers=cookie_headers(board.cookie),
    )
    assert missing.status == 200
    assert "No issues match the current filters" in missing.text

    archived = await post_form(
        board,
        f"{issue_url}/archive",
        via=issue_url,
        data={"expected_revision": revision.group(1), "archived": "1"},
    )
    assert archived.status in {302, 303}

    board_page = await board.client.get(f"{base}/board", headers=cookie_headers(board.cookie))
    assert "Findable issue" not in board_page.text


async def test_project_landing_remembers_last_view_and_keyboard_hooks(board: RunningBoard) -> None:
    project_id = await create_project(board)
    base = f"/workspaces/{board.workspace_id}/projects/{project_id}"
    list_page = await board.client.get(f"{base}/list", headers=cookie_headers(board.cookie))
    assert list_page.status == 200
    assert "/static/board.js" in list_page.text
    board.cookie = response_cookie(list_page) or board.cookie

    landing = await board.client.get(base, headers=cookie_headers(board.cookie))
    assert landing.status in {302, 303}
    location = landing.header("location", "") or ""
    assert location.endswith("/list")


async def test_malformed_create_project_keeps_submitted_values(board: RunningBoard) -> None:
    response = await post_form(
        board,
        f"/workspaces/{board.workspace_id}/projects",
        via=f"/workspaces/{board.workspace_id}",
        data={"key": "bad key", "name": "Still here", "description": "keep me"},
    )
    assert response.status == 422
    assert "Still here" in response.text
    assert "keep me" in response.text
