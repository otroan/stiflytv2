"""fkb_wfs: licence confirmed as CC BY 4.0, derived geometry may be published"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute("""UPDATE source_raw.source SET licence = 'CC BY 4.0', export_policy = 'allowed'
                  WHERE key = 'fkb_wfs'""")


def downgrade() -> None:
    op.execute("""UPDATE source_raw.source SET export_policy = 'unresolved',
                  licence = 'unclear: catalogued as open data, capabilities state Norge digitalt'
                  WHERE key = 'fkb_wfs'""")
