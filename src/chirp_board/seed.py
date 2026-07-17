"""Idempotent, application-owned demo data for local Board evaluation."""

from dataclasses import dataclass

from chirp_workspace_core import WorkspacePrincipal

from .models import ColorToken, Issue, Priority, Project
from .repository import BoardRepository


@dataclass(frozen=True, slots=True)
class DemoSeed:
    project: Project
    issues: tuple[Issue, ...]


async def seed_demo(repository: BoardRepository, principal: WorkspacePrincipal) -> DemoSeed:
    """Ensure a compact demo project exists, without duplicating data on replay."""
    projects = await repository.projects(principal, include_archived=True)
    project = next((item for item in projects if item.key == "DEMO"), None)
    if project is None:
        project, _workflow, _statuses = await repository.create_project(
            principal,
            key="DEMO",
            name="Chirp Board Demo",
            description="A small workspace for evaluating the Board domain.",
        )

    _workflow, statuses = await repository.workflow(principal, str(project.id))
    active_statuses = tuple(item for item in statuses if item.archived_at is None)
    if not active_statuses:
        raise RuntimeError("The demo Board project has no active workflow status.")

    labels = await repository.labels(principal, project_id=str(project.id))
    label_by_name = {item.name.casefold(): item for item in labels}
    for name, color in (("bug", ColorToken.RED), ("feature", ColorToken.BLUE)):
        if name not in label_by_name:
            label_by_name[name] = await repository.create_label(
                principal,
                project_id=str(project.id),
                name=name,
                color_token=color,
            )

    page = await repository.list_issues(
        principal,
        project_id=str(project.id),
        include_archived=True,
        limit=100,
    )
    issue_by_title = {item.title: item for item in page.items}
    specs = (
        ("Confirm the first workflow", Priority.HIGH, "feature"),
        ("Capture product feedback", Priority.MEDIUM, "bug"),
    )
    for title, priority, label_name in specs:
        if title in issue_by_title:
            continue
        issue = await repository.create_issue(
            principal,
            project_id=str(project.id),
            title=title,
            description_source="Seeded by Chirp Board's idempotent demo helper.",
            priority=priority,
            status_id=str(active_statuses[0].id),
        )
        issue_by_title[title] = await repository.replace_labels(
            principal,
            str(issue.id),
            expected_revision=issue.revision,
            label_ids=(str(label_by_name[label_name].id),),
        )

    return DemoSeed(project, tuple(issue_by_title[title] for title, _priority, _label in specs))
