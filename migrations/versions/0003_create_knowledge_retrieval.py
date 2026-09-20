"""지식 문서와 pgvector 검색 Chunk 테이블을 생성한다.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """문서 버전과 768차원 임베딩을 저장할 검색 스키마를 추가한다."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_document",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "current_version >= 1", name="ck_knowledge_document_version_positive"
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_knowledge_document"),
        sa.UniqueConstraint("source_path", name="uq_knowledge_document_source_path"),
    )
    op.create_table(
        "knowledge_chunk",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "document_version >= 1", name="ck_knowledge_chunk_version_positive"
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunk_index_nonnegative"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.document_id"],
            name="fk_knowledge_chunk_document_id_knowledge_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_knowledge_chunk"),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            "chunk_index",
            name="uq_knowledge_chunk_document_version_index",
        ),
    )
    op.execute(
        "ALTER TABLE knowledge_chunk "
        "ALTER COLUMN embedding TYPE vector(768) USING embedding::vector"
    )
    op.create_index(
        "ix_knowledge_chunk_document_version",
        "knowledge_chunk",
        ["document_id", "document_version"],
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_embedding_hnsw "
        "ON knowledge_chunk USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """검색 인덱스와 지식 문서 테이블을 역순으로 제거한다."""
    op.drop_index("ix_knowledge_chunk_embedding_hnsw", table_name="knowledge_chunk")
    op.drop_index("ix_knowledge_chunk_document_version", table_name="knowledge_chunk")
    op.drop_table("knowledge_chunk")
    op.drop_table("knowledge_document")
