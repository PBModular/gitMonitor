import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
import aiohttp

from ..db import MonitoredRepo
from ..api.github_api import GitHubAPIClient, APIError
from .base_checker import BaseChecker
from .commit_checker import CommitChecker
from .issue_checker import IssueChecker
from .error_handler import handle_api_error

if TYPE_CHECKING:
    from pyrogram import Client as PyrogramClient


class RepoMonitorOrchestrator:
    def __init__(
        self,
        bot: 'PyrogramClient',
        chat_id: int,
        repo_entry: MonitoredRepo,
        base_check_interval: int,
        max_retries: int,
        github_token: Optional[str],
        strings: Dict[str, Any],
        async_session_maker: async_sessionmaker[AsyncSession],
        module_config: Dict[str, Any],
        parent_logger: logging.Logger
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.repo_entry_initial = repo_entry
        self.base_check_interval = base_check_interval
        self.max_retries = max_retries
        self.github_token = github_token
        self.strings = strings
        self.async_session_maker = async_session_maker
        self.module_config = module_config
        
        self.repo_db_id = repo_entry.id
        self.owner = repo_entry.owner
        self.repo_name = repo_entry.repo
        self.repo_url = repo_entry.repo_url

        self.logger = parent_logger
        self.api_client: Optional[GitHubAPIClient] = None
        self.checkers: List[BaseChecker] = []
        self._current_retry_attempt = 0
        self._running = False
        self._stop_permanently_requested = False

    async def _refresh_repo_entry_from_db(self) -> Optional[MonitoredRepo]:
        """Fetches the latest version of MonitoredRepo from the DB."""
        async with self.async_session_maker() as session:
            from .. import db_ops
            fresh_repo_entry = await db_ops.get_repo_by_id(session, self.repo_db_id)
            if not fresh_repo_entry:
                self.logger.error(f"Repo ID {self.repo_db_id} no longer found in DB. Requesting permanent stop.")
                self._stop_permanently_requested = True
                return None
            return fresh_repo_entry

    async def _initialize_checkers(self, current_repo_config: MonitoredRepo):
        """Initializes checkers based on the provided (latest) repo_config."""
        self.checkers = []
        common_args_tuple = (
            self.api_client, current_repo_config, self.async_session_maker,
            self.logger, self.strings, self.bot, self.module_config
        )

        commit_checker_active_in_db = current_repo_config.monitor_commits

        if commit_checker_active_in_db:
            checker = CommitChecker(*common_args_tuple)
            await checker.load_initial_state()
            self.checkers.append(checker)
            self.logger.info("Commit monitoring ENABLED.")
        else:
            temp_checker = CommitChecker(*common_args_tuple)
            await temp_checker.load_initial_state()
            await temp_checker.clear_state_on_disable()
            self.logger.info("Commit monitoring DISABLED, ETag cleared if was present.")

        issue_checker_active_in_db = current_repo_config.monitor_issues

        if issue_checker_active_in_db:
            checker = IssueChecker(*common_args_tuple)
            await checker.load_initial_state()
            self.checkers.append(checker)
            self.logger.info("Issue monitoring ENABLED.")
        else:
            temp_checker = IssueChecker(*common_args_tuple)
            await temp_checker.load_initial_state()
            await temp_checker.clear_state_on_disable()
            self.logger.info("Issue monitoring DISABLED, ETags cleared if were present.")

        if not self.checkers:
            self.logger.warning(f"No checkers active for {self.owner}/{self.repo_name}. Monitor will idle and periodically re-check config.")

    async def run(self) -> bool:
        """
        Main monitoring loop. Returns True to stop permanently, False if cancelled.
        """
        self.api_client = GitHubAPIClient(token=self.github_token, loop=asyncio.get_event_loop())
        self._running = True

        self.logger.info(f"Starting monitor. Interval: {self.base_check_interval}s.")

        is_cancelled = False
        try:
            while self._running:
                latest_repo_config = await self._refresh_repo_entry_from_db()
                if self._stop_permanently_requested or latest_repo_config is None:
                    self._running = False; break

                commit_flag_db = latest_repo_config.monitor_commits
                issue_flag_db = latest_repo_config.monitor_issues
                commit_checker_present = any(isinstance(c, CommitChecker) for c in self.checkers)
                issue_checker_present = any(isinstance(c, IssueChecker) for c in self.checkers)

                if (commit_flag_db != commit_checker_present) or \
                   (issue_flag_db != issue_checker_present) or \
                   not self.checkers:
                    self.logger.info("Monitoring flags changed or first run with checkers. Re-initializing checkers.")
                    await self._initialize_checkers(latest_repo_config)
                    if self._stop_permanently_requested:
                        self._running = False; break

                if not self.checkers:
                    await asyncio.sleep(self.base_check_interval)
                    continue

                try:
                    for checker in self.checkers:
                        if not self._running: break
                        await checker.check()
                    self._current_retry_attempt = 0
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                    self.logger.warning(f"Error during check cycle: {type(e).__name__} - {str(e)}", exc_info=isinstance(e, (APIError, Exception)))
                    self._current_retry_attempt += 1

                    should_stop_now = False
                    wait_duration = self.base_check_interval * (2 ** (self._current_retry_attempt -1))

                    if isinstance(e, APIError):
                        should_stop_now, wait_duration = await handle_api_error(
                            e, self.owner, self.repo_name, self.repo_url,
                            self._current_retry_attempt, self.max_retries, self.base_check_interval,
                            self.logger, self.bot, self.chat_id, self.strings
                        )
                    elif isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
                        if self._current_retry_attempt >= self.max_retries:
                            self.logger.error(f"Max retries for network error. Stopping.")
                            try: await self.bot.send_message(self.chat_id, self.strings["monitor"]["network_error"].format(repo_url=self.repo_url))
                            except: pass
                            should_stop_now = True
                        else: self.logger.info(f"Network error. Waiting {wait_duration:.2f}s.")
                    else:
                        self.logger.error(f"Unexpected critical error in check cycle. Stopping.", exc_info=True)
                        try: await self.bot.send_message(self.chat_id, self.strings["monitor"]["internal_error"].format(repo_url=self.repo_url))
                        except: pass
                        should_stop_now = True

                    if should_stop_now:
                        self._stop_permanently_requested = True
                        self._running = False; break 

                    if wait_duration > 0:
                        self.logger.info(f"Monitor sleeping for {wait_duration:.2f}s due to error.")
                        await asyncio.sleep(wait_duration)
                    continue

                if self._running:
                    await asyncio.sleep(self.base_check_interval)

        except asyncio.CancelledError:
            self.logger.info(f"Monitor cancelled.")
            is_cancelled = True
        except Exception as outer_e:
            self.logger.critical(f"Critical unexpected error in orchestrator: {outer_e}", exc_info=True)
            try:
                await self.bot.send_message(
                    self.chat_id,
                    self.strings["monitor"]["internal_error"].format(repo_url=self.repo_url)
                )
            except Exception as send_err_outer:
                self.logger.warning(f"Failed to send 'internal_error' (orchestrator outer) notification for {self.owner}/{self.repo_name}: {send_err_outer}")
            self._stop_permanently_requested = True
        finally:
            self._running = False
            if self.api_client:
                await self.api_client.close()
            self.logger.info(f"Monitor finished. Permanent stop requested: {self._stop_permanently_requested}, Cancelled: {is_cancelled}")

        return self._stop_permanently_requested and not is_cancelled
