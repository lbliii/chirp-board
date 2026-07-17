"""Shared, real-database acceptance fixtures for Chirp Board."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from chirp.data import Database, migrate
from chirp_workspace_core import Role, WorkspacePrincipal, WorkspaceRepository
from chirp_workspace_core.migrations import migration_directory as core_migration_directory

from chirp_board import BoardRepository
from chirp_board.migrations import migration_directory as board_migration_directory

PASSWORD = "correct horse battery staple"


@dataclass(frozen=True, slots=True)
class BoardHarness:
    database: Database
    identities: WorkspaceRepository
    board: BoardRepository
    owner: WorkspacePrincipal

    async def add_principal(self, role: Role, *, prefix: str) -> WorkspacePrincipal:
        user = await self.identities.create_user(
            email=f"{prefix}@example.test",
            display_name=prefix.title(),
            password=PASSWORD,
        )
        invitation = await self.identities.issue_invitation(
            actor_user_id=self.owner.user.id,
            workspace_id=self.owner.workspace.id,
            email=user.email,
            role=role,
        )
        await self.identities.accept_invitation(token=invitation.token, user_id=user.id)
        return await self.identities.principal(
            user_id=user.id,
            workspace_id=self.owner.workspace.id,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[BoardHarness]:
    database = Database(f"sqlite:///{tmp_path / 'board.db'}", pool_size=8)
    await database.connect()
    await migrate(database, core_migration_directory())
    await migrate(database, board_migration_directory())
    identities = WorkspaceRepository(database)
    result = await identities.bootstrap_first_owner(
        expected_setup_token="deployment-setup-token",
        supplied_setup_token="deployment-setup-token",
        email="owner@example.test",
        display_name="Owner",
        password=PASSWORD,
        workspace_slug="primary",
        workspace_name="Primary Workspace",
    )
    owner = await identities.principal(
        user_id=result.user.id,
        workspace_id=result.workspace.id,
    )
    yield BoardHarness(database, identities, BoardRepository(database), owner)
    await database.disconnect()
