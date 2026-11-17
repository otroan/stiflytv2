"""Alembic environment for the import track (source_raw, source_index, import_control).

Run as stiflyt2_migrate. Objects are created as stiflyt2_owner.
"""
import os

from alembic import context
from sqlalchemy import create_engine, text

url = os.environ.get("STIFLYT2_MIGRATE_URL", "postgresql+psycopg:///stiflyt2")


def run() -> None:
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text("SET ROLE stiflyt2_owner"))
        context.configure(
            connection=conn,
            target_metadata=None,
            version_table="alembic_version_import",
            version_table_schema="import_control",
        )
        with context.begin_transaction():
            context.run_migrations()
        conn.commit()


run()
