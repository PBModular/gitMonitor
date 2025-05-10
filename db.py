from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy import BigInteger, UniqueConstraint, Index, Integer, Boolean
from sqlalchemy.sql import expression # Added for server_default
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

    # Issue (Open)
    last_known_issue_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    issue_etag: Mapped[Optional[str]] = mapped_column(nullable=True)

    # Issue (Closed)
    last_closed_issue_update_ts: Mapped[Optional[str]] = mapped_column(nullable=True)
    closed_issue_etag: Mapped[Optional[str]] = mapped_column(nullable=True)

    # Monitoring flags
    monitor_commits: Mapped[bool] = mapped_column(server_default=expression.true(), default=True, nullable=False)
    monitor_issues: Mapped[bool] = mapped_column(server_default=expression.true(), default=True, nullable=False)

    # Ensure a chat can only monitor a specific repo URL once
    __table_args__ = (
        UniqueConstraint('chat_id', 'repo_url', name='uq_chat_repo_url'),
        Index('ix_chat_id', 'chat_id'),
    )

    def __repr__(self):
        interval = self.check_interval or 'default'
        sha = self.last_commit_sha[:7] if self.last_commit_sha else 'None'
        issue_num = self.last_known_issue_number if self.last_known_issue_number else 'None'
        closed_ts = self.last_closed_issue_update_ts if self.last_closed_issue_update_ts else 'None'
        commits_mon = 'C✓' if self.monitor_commits else 'C✗'
        issues_mon = 'I✓' if self.monitor_issues else 'I✗'
        return (f"MonitoredRepo(id={self.id}, chat_id={self.chat_id}, repo={self.owner}/{self.repo}, "
                f"interval={interval}, last_sha={sha}, last_issue_num={issue_num}, last_closed_ts={closed_ts}, "
                f"mon=({commits_mon},{issues_mon}))")
