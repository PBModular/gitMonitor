from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy import BigInteger, UniqueConstraint, Index, Integer
from typing import Optional

class Base(DeclarativeBase):
    pass

class MonitoredRepo(Base):
    __tablename__ = 'monitored_repo'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_url: Mapped[str] = mapped_column(nullable=False)
    owner: Mapped[str] = mapped_column(nullable=False)
    repo: Mapped[str] = mapped_column(nullable=False)
    check_interval: Mapped[Optional[int]] = mapped_column(nullable=True)

    # Commit
    last_commit_sha: Mapped[Optional[str]] = mapped_column(nullable=True)
    commit_etag: Mapped[Optional[str]] = mapped_column(nullable=True)

    # Issue
    last_known_issue_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    issue_etag: Mapped[Optional[str]] = mapped_column(nullable=True)

    # TODO: Add flags to enable/disable monitoring for commits, issues, etc.

    # Ensure a chat can only monitor a specific repo URL once
    __table_args__ = (
        UniqueConstraint('chat_id', 'repo_url', name='uq_chat_repo_url'),
        Index('ix_chat_id', 'chat_id'),
    )

    def __repr__(self):
        interval = self.check_interval or 'default'
        sha = self.last_commit_sha[:7] if self.last_commit_sha else 'None'
        issue_num = self.last_known_issue_number if self.last_known_issue_number else 'None'
        return (f"MonitoredRepo(id={self.id}, chat_id={self.chat_id}, repo={self.owner}/{self.repo}, "
                f"interval={interval}, last_sha={sha}, last_issue_num={issue_num})")
