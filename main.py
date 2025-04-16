import asyncio
import aiohttp
import logging
from urllib.parse import urlparse

from pyrogram.types import Message
from pyrogram.errors import RPCError
from sqlalchemy import select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession

from base.module import BaseModule, command
from .db import Base, ChatState

class gitMonitorModule(BaseModule):
    def on_init(self):
        self.monitor_tasks: dict[int, asyncio.Task] = {}
        self.github_token = self.module_config.get("api_token")
        if not self.github_token:
            self.logger.warning("Valid GitHub API token ('api_token') not found in module config. API rate limits will be lower.")
        self._async_session_maker: sessionmaker | None = None

    @property
    def db_meta(self):
        return Base.metadata

    @property
    def async_session(self) -> sessionmaker[AsyncSession]:
        if self._async_session_maker is None and self.db:
            self._async_session_maker = sessionmaker(
                self.db.engine, class_=AsyncSession, expire_on_commit=False
            )
        elif self._async_session_maker is None and not self.db:
            self.logger.error("Database is not available for GitMonitorModule.")
            raise RuntimeError("Database required but not available.")
        return self._async_session_maker

    async def on_db_ready(self):
        self.logger.info("Database ready. Loading existing monitor states...")
        try:
            async with self.async_session() as session:
                result = await session.execute(select(ChatState).where(ChatState.repo_url != None))
                chat_states = result.scalars().all()

                count = 0
                for chat_state in chat_states:
                    chat_id = chat_state.chat_id
                    repo_url = chat_state.repo_url

                    # Ensure required data is present before starting monitor
                    if repo_url:
                        self.logger.info(f"Restarting monitor for chat {chat_id} on repo {repo_url}")
                        if chat_id in self.monitor_tasks:
                            self.monitor_tasks[chat_id].cancel()
                        self.monitor_tasks[chat_id] = asyncio.create_task(
                            self._monitor_repo(chat_id, repo_url)
                        )
                        count += 1
                    else:
                        self.logger.warning(f"Skipping chat {chat_id}: Missing repo_url in DB.")
                self.logger.info(f"Successfully restarted {count} monitors.")

        except Exception as e:
            self.logger.error(f"Error loading chat states from database: {e}", exc_info=True)

    def on_unload(self):
        self.logger.info(f"Unloading GitMonitorModule. Cancelling {len(self.monitor_tasks)} tasks...")
        for task in self.monitor_tasks.values():
            if not task.done():
                task.cancel()
        self.monitor_tasks.clear()
        self.logger.info("All monitoring tasks cancelled.")

    @property
    def help_page(self):
        return self.S["help"]

    async def _stop_monitor(self, session: AsyncSession, chat_id: int):
        """Helper to stop monitoring and clean up DB."""
        if chat_id in self.monitor_tasks:
            if not self.monitor_tasks[chat_id].done():
                self.monitor_tasks[chat_id].cancel()
            del self.monitor_tasks[chat_id]
            self.logger.info(f"Cancelled monitor task for chat {chat_id}")

        await session.execute(delete(ChatState).where(ChatState.chat_id == chat_id))
        await session.commit()
        self.logger.info(f"Removed database entry for chat {chat_id}")

    async def _monitor_repo(self, chat_id: int, repo_url: str):
        """Core task to monitor a GitHub repository for new commits."""
        owner, repo = self._parse_github_url(repo_url)
        if not owner or not repo:
            self.logger.error(f"[{chat_id}] Invalid repo URL format passed to monitor: {repo_url}")
            return

        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        last_commit_sha: str | None = None
        etag: str | None = None
        retries = 0
        max_retries = 5
        base_sleep_time = 60

        self.logger.info(f"[{chat_id}] Starting monitor for {owner}/{repo}")

        headers = {}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        headers["Accept"] = "application/vnd.github.v3+json"

        try:
            while True:
                current_headers = headers.copy()
                if etag:
                    current_headers["If-None-Match"] = etag

                try:
                    async with aiohttp.ClientSession(headers=current_headers) as http_session:
                        async with http_session.get(api_url, timeout=30) as response:
                            # Handle ETag response
                            if response.status == 304:
                                self.logger.debug(f"[{chat_id}] No new commits for {owner}/{repo} (ETag match).")
                                retries = 0 # Reset retries on success/304
                                await asyncio.sleep(base_sleep_time)
                                continue

                            # Handle specific errors before raise_for_status
                            if response.status == 404:
                                self.logger.error(f"[{chat_id}] Repository {owner}/{repo} not found (404). Stopping monitor.")
                                await self.bot.send_message(chat_id, self.S["monitor"]["repo_not_found"].format(repo_url=repo_url))
                                async with self.async_session() as db_session:
                                    await self._stop_monitor(db_session, chat_id)
                                return
                            if response.status == 403:
                                rate_limit_reset = response.headers.get('X-RateLimit-Reset')
                                reset_time_str = f" (resets at {rate_limit_reset})" if rate_limit_reset else ""
                                self.logger.warning(f"[{chat_id}] Forbidden (403) accessing {owner}/{repo}. Check token or rate limits{reset_time_str}.")
                                await asyncio.sleep(base_sleep_time * (retries + 2))
                                continue
                            if response.status == 401:
                                self.logger.error(f"[{chat_id}] Unauthorized (401) accessing {owner}/{repo}. Check token.")
                                await self.bot.send_message(chat_id, self.S["monitor"]["auth_error"].format(repo_url=repo_url))
                                async with self.async_session() as db_session:
                                    await self._stop_monitor(db_session, chat_id)
                                return


                            # Raise exceptions for other bad statuses (5xx etc)
                            response.raise_for_status()

                            # Process successful response
                            data = await response.json()
                            new_etag = response.headers.get("ETag")

                    # Check data validity after session closes
                    if not data or not isinstance(data, list):
                        self.logger.warning(f"[{chat_id}] Received empty or invalid data from GitHub API for {owner}/{repo}.")
                        await asyncio.sleep(base_sleep_time)
                        continue

                    latest_commit = data[0]
                    current_commit_sha = latest_commit.get("sha")

                    if not current_commit_sha:
                        self.logger.warning(f"[{chat_id}] Could not find SHA in latest commit data for {owner}/{repo}.")
                        await asyncio.sleep(base_sleep_time)
                        continue

                    # Initialize last_commit_sha on first successful fetch
                    if last_commit_sha is None:
                        last_commit_sha = current_commit_sha
                        self.logger.info(f"[{chat_id}] Initialized monitor. Last commit: {last_commit_sha[:7]}")
                    # Check for new commit
                    elif current_commit_sha != last_commit_sha:
                        self.logger.info(f"[{chat_id}] New commit detected for {owner}/{repo}: {current_commit_sha[:7]}")
                        commit_info = latest_commit.get("commit", {})
                        commit_url = latest_commit.get("html_url", "#")
                        author_info = commit_info.get("author", {})
                        author_name = author_info.get("name", "Unknown Author")
                        commit_message = commit_info.get("message", "No message").split('\n')[0]
                        sha_short = current_commit_sha[:7]

                        text = self.S["monitor"]["new_commit"].format(
                            owner=owner, repo=repo, author=author_name,
                            message=commit_message, sha=sha_short, commit_url=commit_url
                        )
                        try:
                            await self.bot.send_message(
                                chat_id,
                                text,
                                disable_web_page_preview=True
                            )
                        except RPCError as e:
                            self.logger.error(f"[{chat_id}] Failed to send commit notification: {e}. Stopping monitor.")
                            async with self.async_session() as db_session:
                                await self._stop_monitor(db_session, chat_id)
                            return

                        last_commit_sha = current_commit_sha

                    etag = new_etag
                    retries = 0

                # Handle network/client errors
                except aiohttp.ClientError as e:
                    retries += 1
                    self.logger.warning(f"[{chat_id}] Network error monitoring {repo_url}: {e}. Retry {retries}/{max_retries}.")
                    if retries >= max_retries:
                        self.logger.error(f"[{chat_id}] Max retries exceeded for {repo_url}. Stopping monitor.")
                        try:
                            await self.bot.send_message(chat_id, self.S["monitor"]["network_error"].format(repo_url=repo_url))
                        except RPCError as send_e:
                            self.logger.error(f"[{chat_id}] Failed to send network error notification: {send_e}")
                        async with self.async_session() as db_session:
                            await self._stop_monitor(db_session, chat_id)
                        return
                    await asyncio.sleep(base_sleep_time * retries)
                    continue

                # Handle unexpected errors
                except Exception as e:
                    self.logger.error(f"[{chat_id}] Unexpected error monitoring {repo_url}: {e}", exc_info=True)
                    try:
                        await self.bot.send_message(chat_id, self.S["monitor"]["internal_error"].format(repo_url=repo_url))
                    except RPCError as send_e:
                        self.logger.error(f"[{chat_id}] Failed to send internal error notification: {send_e}")
                    async with self.async_session() as db_session:
                        await self._stop_monitor(db_session, chat_id)
                    return

                await asyncio.sleep(base_sleep_time)

        except asyncio.CancelledError:
            self.logger.info(f"[{chat_id}] Monitor task for {owner}/{repo} was cancelled.")
        finally:
            # Ensure task reference is removed if task stops unexpectedly or is cancelled
            if chat_id in self.monitor_tasks and self.monitor_tasks.get(chat_id, None) is asyncio.current_task():
                del self.monitor_tasks[chat_id]
            self.logger.info(f"[{chat_id}] Monitor task for {owner}/{repo} finished.")

    @command("git_monitor")
    async def set_repo_cmd(self, _, message: Message):
        """Sets or updates the GitHub repository to monitor for this chat."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply_text(self.S["set_repo"]["usage"])
            return

        repo_url = message.command[1]
        owner, repo = self._parse_github_url(repo_url)

        if not owner or not repo:
            await message.reply_text(self.S["set_repo"]["invalid_url"].format(repo_url=repo_url))
            return

        try:
            confirmation_message = await message.reply_text(
                self.S["set_repo"]["starting"].format(owner=owner, repo=repo)
            )
        except RPCError as e:
            self.logger.error(f"[{chat_id}] Failed to send confirmation message: {e}")
            try:
                await self.bot.send_message(chat_id, self.S["set_repo"]["starting"].format(owner=owner, repo=repo))
            except RPCError as e2:
                self.logger.error(f"[{chat_id}] Failed to send confirmation message even without reply: {e2}")
                return
            confirmation_message = None

        try:
            async with self.async_session() as session:
                if chat_id in self.monitor_tasks:
                    self.logger.info(f"[{chat_id}] Stopping existing monitor before setting new repo.")
                    task = self.monitor_tasks.pop(chat_id)
                    if not task.done():
                        task.cancel()
                        try:
                            await asyncio.wait_for(task, timeout=1.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass
                        except Exception as e:
                            self.logger.warning(f"[{chat_id}] Error waiting for previous task cancellation: {e}")

                chat_state = await session.get(ChatState, chat_id)
                if not chat_state:
                    chat_state = ChatState(chat_id=chat_id)
                    session.add(chat_state)
                    self.logger.info(f"[{chat_id}] Creating new ChatState entry.")
                chat_state.repo_url = repo_url
                await session.commit()

                self.logger.info(f"[{chat_id}] Updated database: repo={repo_url}")

                new_task = asyncio.create_task(
                    self._monitor_repo(chat_id, repo_url)
                )
                self.monitor_tasks[chat_id] = new_task

                if confirmation_message:
                    await confirmation_message.edit_text(
                        self.S["set_repo"]["success"].format(owner=owner, repo=repo)
                    )
                else:
                     await self.bot.send_message(
                        chat_id,
                        self.S["set_repo"]["success"].format(owner=owner, repo=repo)
                    )
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error setting repository {repo_url}: {e}", exc_info=True)
            try:
                error_text = self.S["set_repo"]["error_generic"]
                if confirmation_message:
                    await confirmation_message.edit_text(error_text)
                else:
                    await message.reply_text(error_text)
            except RPCError as e_report:
                self.logger.error(f"[{chat_id}] Failed to report error to user: {e_report}")

            if chat_id in self.monitor_tasks:
                task = self.monitor_tasks.get(chat_id)
                if task and task is locals().get('new_task'):
                    if not task.done():
                        task.cancel()
                    del self.monitor_tasks[chat_id]

    @command("git_stop")
    async def stop_cmd(self, _, message: Message):
        """Stops monitoring commits for this chat."""
        chat_id = message.chat.id
        is_monitoring = chat_id in self.monitor_tasks

        try:
            async with self.async_session() as session:
                state = await session.get(ChatState, chat_id)
                db_has_repo = state and state.repo_url is not None

                if not is_monitoring and not db_has_repo:
                    await message.reply_text(self.S["stop"]["not_monitoring"])
                    return

                if not is_monitoring and db_has_repo:
                    self.logger.warning(f"[{chat_id}] Found DB state but no active task during /git_stop. Cleaning DB.")

                await self._stop_monitor(session, chat_id)
            await message.reply_text(self.S["stop"]["success"])

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error stopping monitor: {e}", exc_info=True)
            await message.reply_text(self.S["stop"]["error"])
