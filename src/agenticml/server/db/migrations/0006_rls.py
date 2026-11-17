"""Enable and force row-level security on every workspace-scoped table."""


def upgrade(tx, dialect):
    if dialect.name == 'sqlite':
        return
    rows = tx.fetchall(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
        "WHERE c.table_schema=current_schema() AND c.column_name='workspace_id' "
        "AND t.table_type='BASE TABLE'")
    for row in rows:
        # enable_workspace_rls is REQUIRED for any later migration that adds a
        # workspace-scoped table; 0006 will not run again for those tables.
        dialect.enable_workspace_rls(tx, row['table_name'])
