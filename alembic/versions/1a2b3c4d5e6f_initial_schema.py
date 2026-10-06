"""Initial schema

Revision ID: 1a2b3c4d5e6f
Revises: 
Create Date: 2024-04-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table('import_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('file_name', sa.String(), nullable=False),
        sa.Column('upload_date', sa.DateTime(), nullable=True),
        sa.Column('total_records', sa.Integer(), nullable=True),
        sa.Column('successful_records', sa.Integer(), nullable=True),
        sa.Column('failed_records', sa.Integer(), nullable=True),
        sa.Column('duplicate_records', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('mapping_config', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    op.create_table('staging_records',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('import_id', sa.Uuid(), nullable=True),
        sa.Column('sheet_name', sa.String(), nullable=False),
        sa.Column('target_table', sa.String(), nullable=True),
        sa.Column('row_index', sa.Integer(), nullable=False),
        sa.Column('raw_data', sa.JSON(), nullable=False),
        sa.Column('mapped_data', sa.JSON(), nullable=True),
        sa.Column('is_valid', sa.Boolean(), nullable=True),
        sa.Column('errors', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['import_id'], ['import_history.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    op.create_index(op.f('ix_staging_records_import_id'), 'staging_records', ['import_id'], unique=False)
    op.create_index(op.f('ix_staging_records_sheet_name'), 'staging_records', ['sheet_name'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_staging_records_sheet_name'), table_name='staging_records')
    op.drop_index(op.f('ix_staging_records_import_id'), table_name='staging_records')
    op.drop_table('staging_records')
    op.drop_table('import_history')
