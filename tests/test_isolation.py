"""Database-level guarantees: source_raw is append-only and the import role
cannot reach core or history. These run against a bootstrapped, migrated
database that holds at least one complete snapshot.
"""
import psycopg
import pytest
from conftest import TEST_DB as DB
from conftest import role_dsn


def connect(role: str):
    return psycopg.connect(role_dsn(role))


def rejected(role: str, sql: str) -> str:
    """Run sql as role, expect it to fail, return the SQLSTATE."""
    with connect(role) as conn:
        with pytest.raises(psycopg.Error) as e:
            conn.execute(sql)
        conn.rollback()
        return e.value.sqlstate


RESTRICT = "23001"
DENIED = "42501"


@pytest.mark.parametrize(
    "sql",
    [
        ("UPDATE source_raw.feature_version SET properties = '{}' "
        "WHERE id = (SELECT min(id) FROM source_raw.feature_version)"),
        ("DELETE FROM source_raw.feature_version "
        "WHERE id = (SELECT min(id) FROM source_raw.feature_version)"),
        "TRUNCATE source_raw.feature_version CASCADE",
        "UPDATE source_raw.snapshot SET counts = '{}' WHERE state = 'complete'",
        "DELETE FROM source_raw.snapshot",
        ("DELETE FROM source_raw.snapshot_member WHERE snapshot_id = "
        "(SELECT id FROM source_raw.snapshot WHERE state = 'complete' LIMIT 1)"),
    ],
)
def test_import_role_cannot_change_stored_source_data(have_snapshot, sql):
    assert rejected("stiflyt2_import", sql) in (RESTRICT, DENIED)


@pytest.mark.parametrize(
    "sql",
    [
        ("UPDATE source_raw.feature_version SET properties = '{}' "
        "WHERE id = (SELECT min(id) FROM source_raw.feature_version)"),
        "DELETE FROM source_raw.snapshot",
    ],
)
def test_even_the_owner_is_stopped_by_the_triggers(have_snapshot, sql):
    with psycopg.connect(f"dbname={DB}") as conn:
        conn.execute("SET ROLE stiflyt2_owner")
        with pytest.raises(psycopg.Error) as e:
            conn.execute(sql)
        assert e.value.sqlstate == RESTRICT


@pytest.mark.parametrize("schema", ["core", "history"])
def test_import_role_has_no_access_to_owned_schemas(schema):
    assert rejected("stiflyt2_import", f"CREATE TABLE {schema}.x (i int)") == DENIED
    with connect("stiflyt2_import") as conn:
        ok = conn.execute(
            "SELECT has_schema_privilege(%s, 'USAGE') OR has_schema_privilege(%s, 'CREATE')",
            (schema, schema),
        ).fetchone()[0]
    assert ok is False


def test_api_role_cannot_read_or_write_raw_source_data():
    assert rejected("stiflyt2_api", "SELECT count(*) FROM source_raw.feature_version") == DENIED
    assert rejected("stiflyt2_api", "DELETE FROM import_control.active_snapshot") == DENIED


def test_no_foreign_keys_cross_the_boundary():
    """Nothing in core/history may reference the import side, or vice versa."""
    own, imp = ("core", "history"), ("source_raw", "source_index", "import_control")
    with psycopg.connect(f"dbname={DB}") as conn:
        rows = conn.execute(
            """SELECT c.conname FROM pg_constraint c
               JOIN pg_class a ON a.oid = c.conrelid JOIN pg_namespace an ON an.oid = a.relnamespace
               JOIN pg_class b ON b.oid = c.confrelid JOIN pg_namespace bn ON bn.oid = b.relnamespace
               WHERE c.contype = 'f'
                 AND ((an.nspname = ANY(%s) AND bn.nspname = ANY(%s))
                   OR (an.nspname = ANY(%s) AND bn.nspname = ANY(%s)))""",
            (list(own), list(imp), list(imp), list(own)),
        ).fetchall()
    assert rows == []
