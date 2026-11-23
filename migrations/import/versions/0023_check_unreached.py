"""A source that could not be reached is told apart from a delivery refused as faulty

`unreached`: the service did not answer, or said it was too busy (Overpass at
capacity). Nothing was wrong with what it holds, and it is asked again the next
night. `rejected` stays for a delivery that was fetched and looked faulty, which
a person has to look at.
"""
from alembic import op

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE import_control.check_log DROP CONSTRAINT check_log_outcome_check;
        ALTER TABLE import_control.check_log ADD CONSTRAINT check_log_outcome_check
            CHECK (outcome IN ('unchanged', 'imported', 'rejected', 'unreached', 'error'))""")


def downgrade() -> None:
    op.execute("""
        UPDATE import_control.check_log SET outcome = 'rejected' WHERE outcome = 'unreached';
        ALTER TABLE import_control.check_log DROP CONSTRAINT check_log_outcome_check;
        ALTER TABLE import_control.check_log ADD CONSTRAINT check_log_outcome_check
            CHECK (outcome IN ('unchanged', 'imported', 'rejected', 'error'))""")
