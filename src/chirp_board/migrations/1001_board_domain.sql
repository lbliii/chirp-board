-- Chirp Board domain schema frozen by Chirp decision #763.
-- IDs and timestamps are application-generated opaque UUID and ISO-8601 text.
-- DDL uses the SQLite/PostgreSQL common subset consumed by Chirp migrations.

CREATE TABLE board_projects (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CONSTRAINT uq_board_projects_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_board_projects_workspace_key UNIQUE (workspace_id, key),
    CONSTRAINT ck_board_projects_key CHECK (length(key) BETWEEN 2 AND 8),
    CONSTRAINT ck_board_projects_name CHECK (length(name) BETWEEN 1 AND 120),
    CONSTRAINT ck_board_projects_description CHECK (length(description) <= 2000),
    CONSTRAINT ck_board_projects_revision CHECK (revision >= 1),
    CONSTRAINT fk_board_projects_workspace FOREIGN KEY (workspace_id)
        REFERENCES workspace_core_workspaces (id) ON DELETE CASCADE
);

CREATE TABLE board_workflows (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CONSTRAINT uq_board_workflows_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT ck_board_workflows_name CHECK (length(name) BETWEEN 1 AND 80),
    CONSTRAINT ck_board_workflows_revision CHECK (revision >= 1),
    CONSTRAINT fk_board_workflows_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_board_workflows_active_project
    ON board_workflows (workspace_id, project_id)
    WHERE archived_at IS NULL;

CREATE TABLE board_statuses (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    color_token TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CONSTRAINT uq_board_statuses_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT ck_board_statuses_name CHECK (length(name) BETWEEN 1 AND 80),
    CONSTRAINT ck_board_statuses_category CHECK (
        category IN ('backlog', 'unstarted', 'started', 'completed', 'canceled')
    ),
    CONSTRAINT ck_board_statuses_color CHECK (
        color_token IN ('gray', 'blue', 'green', 'yellow', 'orange', 'red', 'purple', 'pink')
    ),
    CONSTRAINT ck_board_statuses_position CHECK (position >= 0),
    CONSTRAINT fk_board_statuses_workflow FOREIGN KEY (workspace_id, workflow_id)
        REFERENCES board_workflows (workspace_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_board_statuses_active_position
    ON board_statuses (workspace_id, workflow_id, position)
    WHERE archived_at IS NULL;

CREATE TABLE board_project_issue_counters (
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    next_number INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, project_id),
    CONSTRAINT ck_board_issue_counters_next CHECK (next_number >= 1),
    CONSTRAINT fk_board_issue_counters_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE board_issues (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description_source TEXT NOT NULL,
    priority TEXT NOT NULL,
    status_id TEXT NOT NULL,
    reporter_user_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CONSTRAINT uq_board_issues_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_board_issues_project_number UNIQUE (workspace_id, project_id, number),
    CONSTRAINT uq_board_issues_status_rank UNIQUE (workspace_id, status_id, rank),
    CONSTRAINT ck_board_issues_title CHECK (length(title) BETWEEN 1 AND 200),
    CONSTRAINT ck_board_issues_description CHECK (length(description_source) <= 100000),
    CONSTRAINT ck_board_issues_priority CHECK (
        priority IN ('none', 'low', 'medium', 'high', 'urgent')
    ),
    CONSTRAINT ck_board_issues_number CHECK (number >= 1),
    CONSTRAINT ck_board_issues_rank CHECK (rank >= 0),
    CONSTRAINT ck_board_issues_revision CHECK (revision >= 1),
    CONSTRAINT fk_board_issues_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_board_issues_status FOREIGN KEY (workspace_id, status_id)
        REFERENCES board_statuses (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_board_issues_reporter FOREIGN KEY (workspace_id, reporter_user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE RESTRICT
);

CREATE INDEX ix_board_issues_project_active
    ON board_issues (workspace_id, project_id, archived_at, status_id, rank, number);
CREATE INDEX ix_board_issues_updated
    ON board_issues (workspace_id, project_id, updated_at, id);

CREATE TABLE board_issue_assignees (
    workspace_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, issue_id, user_id),
    CONSTRAINT fk_board_assignees_issue FOREIGN KEY (workspace_id, issue_id)
        REFERENCES board_issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_assignees_member FOREIGN KEY (workspace_id, user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE board_labels (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    color_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT,
    CONSTRAINT uq_board_labels_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_board_labels_project_name UNIQUE (
        workspace_id, project_id, normalized_name
    ),
    CONSTRAINT ck_board_labels_name CHECK (length(name) BETWEEN 1 AND 40),
    CONSTRAINT ck_board_labels_color CHECK (
        color_token IN ('gray', 'blue', 'green', 'yellow', 'orange', 'red', 'purple', 'pink')
    ),
    CONSTRAINT fk_board_labels_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE board_issue_labels (
    workspace_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    label_id TEXT NOT NULL,
    PRIMARY KEY (workspace_id, issue_id, label_id),
    CONSTRAINT fk_board_issue_labels_issue FOREIGN KEY (workspace_id, issue_id)
        REFERENCES board_issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_issue_labels_label FOREIGN KEY (workspace_id, label_id)
        REFERENCES board_labels (workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE board_comments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    author_user_id TEXT NOT NULL,
    body_source TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    redacted_at TEXT,
    CONSTRAINT uq_board_comments_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT ck_board_comments_body CHECK (length(body_source) <= 20000),
    CONSTRAINT ck_board_comments_revision CHECK (revision >= 1),
    CONSTRAINT ck_board_comments_redaction CHECK (
        (redacted_at IS NULL AND length(body_source) >= 1)
        OR (redacted_at IS NOT NULL AND body_source = '')
    ),
    CONSTRAINT fk_board_comments_issue FOREIGN KEY (workspace_id, issue_id)
        REFERENCES board_issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_comments_author FOREIGN KEY (workspace_id, author_user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE RESTRICT
);

CREATE INDEX ix_board_comments_issue_created
    ON board_comments (workspace_id, issue_id, created_at, id);

CREATE TABLE board_issue_subscriptions (
    workspace_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, issue_id, user_id),
    CONSTRAINT fk_board_subscriptions_issue FOREIGN KEY (workspace_id, issue_id)
        REFERENCES board_issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_subscriptions_member FOREIGN KEY (workspace_id, user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE CASCADE
);

CREATE TABLE board_saved_views (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    owner_user_id TEXT,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    typed_filter_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT uq_board_saved_views_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT ck_board_saved_views_name CHECK (length(name) BETWEEN 1 AND 80),
    CONSTRAINT ck_board_saved_views_schema CHECK (schema_version = 1),
    CONSTRAINT fk_board_saved_views_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_saved_views_owner FOREIGN KEY (workspace_id, owner_user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_board_saved_views_personal_name
    ON board_saved_views (workspace_id, project_id, owner_user_id, name)
    WHERE owner_user_id IS NOT NULL;
CREATE UNIQUE INDEX uq_board_saved_views_shared_name
    ON board_saved_views (workspace_id, project_id, name)
    WHERE owner_user_id IS NULL;

CREATE TABLE board_activity (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    issue_id TEXT,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    safe_metadata_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    CONSTRAINT uq_board_activity_workspace_id UNIQUE (workspace_id, id),
    CONSTRAINT ck_board_activity_action CHECK (length(action) BETWEEN 1 AND 80),
    CONSTRAINT ck_board_activity_kind CHECK (length(resource_kind) BETWEEN 1 AND 40),
    CONSTRAINT fk_board_activity_project FOREIGN KEY (workspace_id, project_id)
        REFERENCES board_projects (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_activity_issue FOREIGN KEY (workspace_id, issue_id)
        REFERENCES board_issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_board_activity_actor FOREIGN KEY (workspace_id, actor_user_id)
        REFERENCES workspace_core_memberships (workspace_id, user_id) ON DELETE RESTRICT
);

CREATE INDEX ix_board_activity_project_occurred
    ON board_activity (workspace_id, project_id, occurred_at, id);
CREATE INDEX ix_board_activity_issue_occurred
    ON board_activity (workspace_id, issue_id, occurred_at, id);
