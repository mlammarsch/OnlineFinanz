"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# Revision-Kennungen fuer Alembic (nicht von Hand aendern).
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """Wendet die Migration an."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Macht die Migration rueckgaengig."""
    ${downgrades if downgrades else "pass"}
