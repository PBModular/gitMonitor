from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from .db import MonitoredRepo

async def get_repo_by_url(session: AsyncSession, chat_id: int, repo_url: str) -> Optional[MonitoredRepo]:
    """Fetches a monitored repository by its URL for a specific chat."""
    result = await session.execute(
        select(MonitoredRepo)
        .where(MonitoredRepo.chat_id == chat_id)
        .where(MonitoredRepo.repo_url == repo_url)
    )
    return result.scalar_one_or_none()

async def get_repo_by_id(session: AsyncSession, repo_id: int) -> Optional[MonitoredRepo]:
    """Fetches a monitored repository by its database ID."""
    return await session.get(MonitoredRepo, repo_id)

async def create_repo_entry(
    session: AsyncSession,
    chat_id: int,
    repo_url: str,
    owner: str,
    repo_name: str,
    check_interval: Optional[int] = None,
    last_commit_sha: Optional[str] = None,
    etag: Optional[str] = None
) -> MonitoredRepo:
    """Creates and adds a new MonitoredRepo entry to the session, then flushes."""
    new_repo_entry = MonitoredRepo(
        chat_id=chat_id,
        repo_url=repo_url,
        owner=owner,
        repo=repo_name,
        check_interval=check_interval,
        last_commit_sha=last_commit_sha,
        etag=etag
    )
    session.add(new_repo_entry)
    await session.flush()
    return new_repo_entry

async def delete_repo_entry(session: AsyncSession, chat_id: int, repo_id: int) -> bool:
    """Deletes a MonitoredRepo entry from the database."""
    stmt = (
        delete(MonitoredRepo)
        .where(MonitoredRepo.id == repo_id)
        .where(MonitoredRepo.chat_id == chat_id)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0

async def get_repos_for_chat(session: AsyncSession, chat_id: int) -> List[MonitoredRepo]:
    """Lists all monitored repositories for a specific chat."""
    result = await session.execute(
        select(MonitoredRepo)
        .where(MonitoredRepo.chat_id == chat_id)
        .order_by(MonitoredRepo.repo_url)
    )
    return result.scalars().all()

async def set_repo_interval(
    session: AsyncSession,
    repo_entry: MonitoredRepo,
    new_interval: int
) -> MonitoredRepo:
    """Updates the check_interval for a given MonitoredRepo entry and flushes."""
    repo_entry.check_interval = new_interval
    await session.flush()
    return repo_entry

async def get_all_active_repos(session: AsyncSession) -> List[MonitoredRepo]:
    """Fetches all monitored repositories from the database."""
    result = await session.execute(select(MonitoredRepo))
    return result.scalars().all()

async def update_repo_fields(session: AsyncSession, repo_db_id: int, **fields_to_update) -> bool:
    """
    Updates specified fields for a MonitoredRepo entry.
    Fields not present in fields_to_update will not be changed.
    To set a field to NULL, pass field_name=None in fields_to_update.
    """
    if not fields_to_update:
        return True

    stmt = (
        update(MonitoredRepo)
        .where(MonitoredRepo.id == repo_db_id)
        .values(**fields_to_update)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0
