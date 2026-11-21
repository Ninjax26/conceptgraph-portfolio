from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.document_upload import Base


class RevisionPair(Base):
    """The reviewer declares direction explicitly for two immutable upload identities."""
    __tablename__ = "revision_pairs"
    __table_args__ = (CheckConstraint("earlier_id <> later_id", name="ck_revision_distinct_uploads"),)

    earlier_id: Mapped[str] = mapped_column(String(64), ForeignKey("document_uploads.upload_id", ondelete="CASCADE"), primary_key=True)
    later_id: Mapped[str] = mapped_column(String(64), ForeignKey("document_uploads.upload_id", ondelete="CASCADE"), primary_key=True)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_matches: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
