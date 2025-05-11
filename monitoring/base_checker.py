import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, Any

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from ..api.github_api import GitHubAPIClient
from ..db import MonitoredRepo
from .. import db_ops

if TYPE_CHECKING:
    from pyrogram import Client as PyrogramClient


class BaseChecker(ABC):
    def __init__(
        self,
        api_client: GitHubAPIClient,
        repo_entry: MonitoredRepo,
        async_session_maker: async_sessionmaker[AsyncSession],
        logger: logging.Logger,
        strings: Dict[str, Any],
        bot: 'PyrogramClient',
        config: Dict[str, Any]
    ):
        self.api_client = api_client
        self.repo_entry = repo_entry
        self.async_session_maker = async_session_maker
        self.logger = logger.getChild(self.__class__.__name__)
        self.strings = strings
        self.bot = bot
        self.config = config

        self.owner = repo_entry.owner
        self.repo_name = repo_entry.repo 
        self.repo_db_id = repo_entry.id
        self.chat_id = repo_entry.chat_id

    @abstractmethod
    async def check(self) -> None:
        """
        Perform the specific check for this checker.
        Should raise APIError exceptions for the orchestrator to handle.
        """
        pass

    @abstractmethod
    async def load_initial_state(self) -> None:
        """Load initial state from self.repo_entry."""
        pass

    @abstractmethod
    async def clear_state_on_disable(self) -> None:
        """Clear persistent state (e.g., ETags in DB) if checker is disabled."""
        pass

    async def _update_db(self, updates: Dict[str, Any]):
        if not updates:
            return
        try:
            async with self.async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, self.repo_db_id, **updates)
            self.logger.debug(f"Updated DB for {self.owner}/{self.repo_name} with: {updates}")
        except Exception as db_e:
            self.logger.error(f"Failed to update DB state for repo ID {self.repo_db_id}: {db_e}. State might be stale.", exc_info=True)
            raise
