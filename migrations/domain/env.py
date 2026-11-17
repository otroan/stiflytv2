"""Alembic environment for the domain track (core, history): the owned data.

Run as stiflyt2_migrate, or as a member of stiflyt2_owner. Objects are created
as stiflyt2_owner.

    alembic -n domain upgrade head
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
            version_table="alembic_version_domain",
            version_table_schema="core",
        )
        with context.begin_transaction():
            context.run_migrations()
        conn.commit()


run()
