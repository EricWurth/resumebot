"""What an added site's own terms ask of a reader (2.6.0): `js_sites.polite`, and the parse-job kind `terms`.

A site the user adds is read after the terms warning; the site's own asks only slow that read. They are kept in one
nullable JSON column on the site's row, `polite`: `{rate_seconds, avoid_hours, identify, feed, source_url, read_at}`,
None until read. Every existing row stays None. The column is made only if missing.

A job that reads a site's terms is a parse job of the new kind `terms`, so the table's closed list of kinds
(`ck_js_parse_jobs_kind`) is made again with it. SQLite cannot change a check in place, so the table is made again
beside the old one from its own stored definition with that one list changed, the rows copied across in the same
order, the old table dropped, the new one renamed and its indexes made again. Nothing refers to the table, and no row
is changed. It is done only when the check does not already list `terms` (a second run, or a database made from the
models, changes nothing).

Forward only.

Revision ID: 0171
Revises: 0170
"""

import sqlalchemy as sa
from alembic import op

revision = "0171"
down_revision = "0170"
branch_labels = None
depends_on = None

TABLE = "js_parse_jobs"
CHECK = "ck_js_parse_jobs_kind"
KINDS = "'posting', 'resume', 'profile', 'structure', 'rank', 'recipe', 'claims', 'terms'"


def add_column(bind) -> bool:
    """Make `js_sites.polite` if it is not there. Returns whether it was made."""
    if "polite" in {c["name"] for c in sa.inspect(bind).get_columns("js_sites")}:
        return False
    bind.execute(sa.text("ALTER TABLE js_sites ADD COLUMN polite JSON"))
    return True


def _definition(bind) -> str | None:
    return bind.execute(sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
                        {"name": TABLE}).scalar()


def widen_kinds(bind) -> bool:
    """Make the kind check list `terms`. Returns whether the table was changed."""
    if bind.dialect.name != "sqlite":
        bind.execute(sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK}"))
        bind.execute(sa.text(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK (kind IN ({KINDS}))"))
        return True
    sql = _definition(bind)
    if sql is None or "'terms'" in sql or CHECK not in sql:
        return False
    head, _, rest = sql.partition(CHECK)
    start = rest.index("(")
    depth, end = 0, start
    for end in range(start, len(rest)):
        depth += rest[end] == "("
        depth -= rest[end] == ")"
        if depth == 0:
            break
    # `rest` begins `CHECK (kind IN (...))`: the list is made again whole, as the models write it.
    check = rest[:end + 1]
    assert "kind" in check, check
    made = head + CHECK + " CHECK (kind IN (" + KINDS + "))" + rest[end + 1:]
    indexes = [row[0] for row in bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :name AND sql IS NOT NULL"),
        {"name": TABLE})]
    new = f"{TABLE}__new"
    bind.execute(sa.text(made.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {new}", 1)
                         .replace(f'CREATE TABLE "{TABLE}"', f'CREATE TABLE "{new}"', 1)))
    bind.execute(sa.text(f"INSERT INTO {new} SELECT * FROM {TABLE}"))
    bind.execute(sa.text(f"DROP TABLE {TABLE}"))
    bind.execute(sa.text(f"ALTER TABLE {new} RENAME TO {TABLE}"))
    for statement in indexes:
        bind.execute(sa.text(statement))
    return True


def upgrade() -> None:
    bind = op.get_bind()
    add_column(bind)
    widen_kinds(bind)


def downgrade() -> None:
    # Forward only.
    pass
