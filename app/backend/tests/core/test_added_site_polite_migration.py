"""Migration 0171 adds `js_sites.polite` and lets a parse job be of the kind `terms`: a database brought forward is
the database `db.create_schema` makes from the models (same column, same check, same indexes), no row changes, and a
second run changes nothing. Everything here is made up."""

import importlib.util

import pytest
from sqlalchemy import create_engine, text

from app import db

_path = db.BACKEND_DIR / "migrations" / "versions" / "0171_added_site_polite.py"
_spec = importlib.util.spec_from_file_location("migration_0171", _path)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

OLD_KINDS = "'posting', 'resume', 'profile', 'structure', 'rank', 'recipe', 'claims'"


def _made(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    with engine.begin() as connection:
        db.create_schema(connection)
    return engine


def _old(connection) -> None:
    """Take a made database back to what 0170 left: no `polite`, and the kind check without `terms`."""
    connection.execute(text("ALTER TABLE js_sites DROP COLUMN polite"))
    sql = connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'js_parse_jobs'")).scalar()
    assert "'terms'" in sql
    indexes = [r[0] for r in connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'js_parse_jobs' AND sql IS NOT NULL"))]
    connection.execute(text(sql.replace("js_parse_jobs", "js_parse_jobs__old", 1).replace(", 'terms'", "")))
    connection.execute(text("DROP TABLE js_parse_jobs"))
    connection.execute(text("ALTER TABLE js_parse_jobs__old RENAME TO js_parse_jobs"))
    for statement in indexes:
        connection.execute(text(statement))


def _shape(connection) -> dict:
    tables = {name: sql for name, sql in connection.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN ('js_sites', 'js_parse_jobs')"))}
    indexes = sorted(r[0] for r in connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'js_parse_jobs' AND sql IS NOT NULL")))
    # a column made by ALTER sits last, where the models put it among the others: the set is the same
    columns = sorted((r[1], r[2], r[3], r[4]) for r in connection.execute(text("PRAGMA table_info(js_sites)")))
    return {"columns": columns, "indexes": indexes, "check": "ck_js_parse_jobs_kind" in tables["js_parse_jobs"]}


def test_a_database_brought_forward_is_the_database_the_models_make(tmp_path):
    engine = _made(tmp_path)
    with engine.begin() as connection:
        made = _shape(connection)
        _old(connection)
        old = connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'js_parse_jobs'")).scalar()
        assert "'terms'" not in old
        assert m.add_column(connection) is True and m.widen_kinds(connection) is True
        assert _shape(connection) == made
        sql = connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'js_parse_jobs'")).scalar()
        assert f"({m.KINDS})" in sql


_JOB = ("INSERT INTO js_parse_jobs (id, owner, kind, text, state, created_at) "
        "VALUES ('0000000000000000000000000000000{n}', 'o', '{kind}', '{text}', 'waiting', '2026-11-17 00:00:00')")


def test_before_the_migration_a_job_of_the_new_kind_is_refused(tmp_path):
    engine = _made(tmp_path)
    with engine.begin() as connection:
        _old(connection)
        with pytest.raises(Exception, match="ck_js_parse_jobs_kind|CHECK"):
            connection.execute(text(_JOB.format(n=2, kind="terms", text="")))


def test_the_rows_survive_and_a_job_of_the_new_kind_is_then_accepted(tmp_path):
    engine = _made(tmp_path)
    with engine.begin() as connection:
        _old(connection)
        connection.execute(text(_JOB.format(n=1, kind="recipe", text="kept text")))
        m.add_column(connection)
        m.widen_kinds(connection)
        connection.execute(text(_JOB.format(n=3, kind="terms", text="")))
        rows = sorted(connection.execute(text("SELECT kind, text FROM js_parse_jobs")).fetchall())
        assert rows == [("recipe", "kept text"), ("terms", "")]


def test_a_second_run_and_a_database_made_from_the_models_change_nothing(tmp_path):
    engine = _made(tmp_path)
    with engine.begin() as connection:
        made = _shape(connection)
        assert m.add_column(connection) is False and m.widen_kinds(connection) is False
        assert _shape(connection) == made
        _old(connection)
        m.add_column(connection)
        m.widen_kinds(connection)
        once = _shape(connection)
        assert m.add_column(connection) is False and m.widen_kinds(connection) is False
        assert _shape(connection) == once


def test_the_column_is_nullable_json_and_every_existing_site_starts_with_none(tmp_path):
    engine = _made(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE js_sites DROP COLUMN polite"))
        connection.execute(text(
            "INSERT INTO js_sites (id, owner, url, kind, \"on\", created_at, added_by, list_name, read_by, "
            "learn_tries, relearn) VALUES ('00000000000000000000000000000009', 'o', 'https://x.example.test/', "
            "'job_board', 1, '2026-11-17 00:00:00', 'owner', 'search', 'auto', 0, 0)"))
        m.add_column(connection)
        assert connection.execute(text("SELECT polite FROM js_sites")).scalar() is None
        info = {r[1]: r for r in connection.execute(text("PRAGMA table_info(js_sites)"))}["polite"]
        assert info[2].upper() == "JSON" and info[3] == 0
