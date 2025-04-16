import asyncio
from pyrogram.types import Message
from pyrogram.errors import RPCError
from sqlalchemy import select, delete, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from base.module import BaseModule, command
from .db import Base, MonitoredRepo
from .monitor import monitor_repo
from .utils import parse_github_url
from typing import Dict, Optional

class gitMonitorModule(BaseModule):
    def on_init(self):
        self.monitor_tasks: Dict[int, Dict[int, asyncio.Task]] = {}
        self.github_token = self.module_config.get("api_token")
        self.default_check_interval = self.module_config.get("default_check_interval", 60)
        self.max_retries = self.module_config.get("max_retries", 5)
        self.min_interval = 10
        if not self.github_token:
            self.logger.warning("Valid GitHub API token not found in config. Rate limits will be lower.")
        self._async_session_maker: Optional[sessionmaker[AsyncSession]] = None

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
        restarted_count = 0
        try:
            async with self.async_session() as session:
                result = await session.execute(select(MonitoredRepo))
                repos_to_monitor = result.scalars().all()

                for repo_entry in repos_to_monitor:
                    self.logger.info(f"Restarting monitor for chat {repo_entry.chat_id} on repo {repo_entry.repo_url} (DB ID: {repo_entry.id})")
                    await self._start_monitor_task(repo_entry)
                    restarted_count += 1

                self.logger.info(f"Successfully restarted {restarted_count} monitors.")
        except Exception as e:
            self.logger.error(f"Error loading monitoring states from DB: {e}", exc_info=True)

    def on_unload(self):
        self.logger.info(f"Cancelling monitor tasks...")
        count = 0
        for chat_id, repo_tasks in list(self.monitor_tasks.items()):
            for repo_id, task in list(repo_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
                del self.monitor_tasks[chat_id][repo_id]
            if not self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id]
        self.logger.info(f"Cancelled {count} monitoring tasks.")

    @property
    def help_page(self):
        return self.S["help"]

    async def _start_monitor_task(self, repo_entry: MonitoredRepo):
        """Starts a monitor task for a given MonitoredRepo entry and stores it."""
        chat_id = repo_entry.chat_id
        repo_id = repo_entry.id
        check_interval = repo_entry.check_interval or self.default_check_interval

        if chat_id not in self.monitor_tasks:
            self.monitor_tasks[chat_id] = {}

        if repo_id in self.monitor_tasks.get(chat_id, {}):
            existing_task = self.monitor_tasks[chat_id].pop(repo_id, None)
            if existing_task and not existing_task.done():
                self.logger.warning(f"Found existing task for repo ID {repo_id} in chat {chat_id}. Cancelling it before starting new one.")
                existing_task.cancel()

        task = asyncio.create_task(
            self._monitor_wrapper(
                repo_entry=repo_entry,
                check_interval=check_interval,
            )
        )
        self.monitor_tasks[chat_id][repo_id] = task
        self.logger.info(f"Created monitor task for chat {chat_id}, repo ID {repo_id} ({repo_entry.owner}/{repo_entry.repo})")

    async def _monitor_wrapper(self, repo_entry: MonitoredRepo, check_interval: int):
        """Wraps monitor_repo to handle task completion and cleanup."""
        chat_id = repo_entry.chat_id
        repo_id = repo_entry.id
        repo_url = repo_entry.repo_url
        try:
            should_stop_permanently = await monitor_repo(
                bot=self.bot,
                chat_id=chat_id,
                repo_db_id=repo_id,
                repo_url=repo_url,
                check_interval=check_interval,
                max_retries=self.max_retries,
                github_token=self.github_token,
                strings=self.S,
                initial_last_sha=repo_entry.last_commit_sha,
                initial_etag=repo_entry.etag,
                async_session_maker=self.async_session
            )
            if should_stop_permanently:
                self.logger.info(f"Monitor for repo ID {repo_id} ({repo_url}) requested permanent stop. Removing DB entry.")
                await self._remove_repo_from_db(chat_id, repo_id)

        except asyncio.CancelledError:
            self.logger.info(f"Monitor wrapper for repo ID {repo_id} ({repo_url}) was cancelled.")
        except Exception as e:
            self.logger.error(f"Unexpected error in monitor wrapper for repo ID {repo_id} ({repo_url}): {e}", exc_info=True)
            await self._remove_repo_from_db(chat_id, repo_id)
        finally:
            if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id][repo_id]
                if not self.monitor_tasks[chat_id]:
                    del self.monitor_tasks[chat_id]
                self.logger.debug(f"Removed task entry for repo ID {repo_id} from chat {chat_id}")


    async def _stop_monitor_task(self, chat_id: int, repo_id: int):
        """Stops a specific monitor task."""
        if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
            task = self.monitor_tasks[chat_id].pop(repo_id)
            if not task.done():
                task.cancel()
            if not self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id]
            self.logger.info(f"Cancelled and removed monitor task for chat {chat_id}, repo ID {repo_id}")
            return True
        return False

    async def _remove_repo_from_db(self, chat_id: int, repo_id: int):
        """Removes a specific monitored repo entry from the database."""
        try:
            async with self.async_session() as session:
                async with session.begin():
                    await session.execute(
                        delete(MonitoredRepo)
                        .where(MonitoredRepo.chat_id == chat_id)
                        .where(MonitoredRepo.id == repo_id)
                    )
                self.logger.info(f"Removed repo ID {repo_id} for chat {chat_id} from database.")
        except Exception as e:
            self.logger.error(f"Failed to remove repo ID {repo_id} for chat {chat_id} from DB: {e}", exc_info=True)

    @command("git_add")
    async def add_repo_cmd(self, _, message: Message):
        """Adds a GitHub repository to monitor for this chat."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["add_repo"]["usage"])
            return

        repo_url = message.command[1]
        owner, repo = parse_github_url(repo_url)
        if not owner or not repo:
            await message.reply(self.S["add_repo"]["invalid_url"].format(repo_url=repo_url))
            return

        try:
            confirmation_message = await message.reply(
                self.S["add_repo"]["starting"].format(owner=owner, repo=repo)
            )
        except RPCError as e:
            self.logger.error(f"[{chat_id}] Failed to send confirmation: {e}")
            confirmation_message = None

        try:
            async with self.async_session() as session:
                async with session.begin():
                    existing = await session.execute(
                        select(MonitoredRepo.id)
                        .where(MonitoredRepo.chat_id == chat_id)
                        .where(MonitoredRepo.repo_url == repo_url)
                    )
                    if existing.scalar_one_or_none() is not None:
                        error_text = self.S["add_repo"]["already_monitoring"].format(owner=owner, repo=repo)
                        if confirmation_message: await confirmation_message.edit_text(error_text)
                        else: await message.reply(error_text)
                        return

                    new_repo_entry = MonitoredRepo(
                        chat_id=chat_id,
                        repo_url=repo_url,
                        owner=owner,
                        repo=repo,
                        check_interval=None,
                        last_commit_sha=None,
                        etag=None
                    )
                    session.add(new_repo_entry)
                    await session.flush()
                    repo_id = new_repo_entry.id
                    self.logger.info(f"Added repo {owner}/{repo} (ID: {repo_id}) to DB for chat {chat_id}")

                await session.commit()
                await self._start_monitor_task(new_repo_entry)

                success_text = self.S["add_repo"]["success"].format(owner=owner, repo=repo)
                if confirmation_message:
                    await confirmation_message.edit_text(success_text)
                else:
                    await self.bot.send_message(chat_id, success_text)

        except IntegrityError:
            await session.rollback()
            self.logger.warning(f"[{chat_id}] Integrity error likely due to race condition adding {repo_url}.")
            error_text = self.S["add_repo"]["already_monitoring"].format(owner=owner, repo=repo)
            if confirmation_message: await confirmation_message.edit_text(error_text)
            else: await message.reply(error_text)
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error adding repo {repo_url}: {e}", exc_info=True)
            error_text = self.S["add_repo"]["error_generic"]
            if confirmation_message:
                try: await confirmation_message.edit_text(error_text)
                except RPCError: pass
            else:
                await message.reply(error_text)

    @command("git_remove")
    async def remove_repo_cmd(self, _, message: Message):
        """Removes a specific GitHub repository from monitoring."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["remove_repo"]["usage"])
            await message.reply(self.S["remove_repo"]["usage_hint"])
            return

        repo_url_to_remove = message.command[1].strip().rstrip('/')

        try:
            async with self.async_session() as session:
                result = await session.execute(
                    select(MonitoredRepo.id, MonitoredRepo.owner, MonitoredRepo.repo)
                    .where(MonitoredRepo.chat_id == chat_id)
                    .where(MonitoredRepo.repo_url == repo_url_to_remove)
                )
                repo_data = result.first()

                if repo_data is None:
                    await message.reply(self.S["remove_repo"]["not_found"].format(repo_url=repo_url_to_remove))
                    return

                repo_id, owner, repo = repo_data

                stopped = await self._stop_monitor_task(chat_id, repo_id)
                if stopped:
                    self.logger.info(f"Stopped monitor task for {owner}/{repo} (ID: {repo_id}) in chat {chat_id}.")
                else:
                    self.logger.warning(f"No active task found for {owner}/{repo} (ID: {repo_id}) in chat {chat_id}, but DB entry exists. Proceeding with DB removal.")

                await self._remove_repo_from_db(chat_id, repo_id)

            await message.reply(self.S["remove_repo"]["success"].format(owner=owner, repo=repo))

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error removing repo {repo_url_to_remove}: {e}", exc_info=True)
            await message.reply(self.S["remove_repo"]["error"])

    @command("git_list")
    async def list_repos_cmd(self, _, message: Message):
        """Lists the repositories currently being monitored in this chat."""
        chat_id = message.chat.id
        repos_list = []
        try:
            async with self.async_session() as session:
                result = await session.execute(
                    select(MonitoredRepo.repo_url, MonitoredRepo.check_interval)
                    .where(MonitoredRepo.chat_id == chat_id)
                    .order_by(MonitoredRepo.repo_url)
                )
                monitored_repos = result.all()

                if not monitored_repos:
                    await message.reply(self.S["list_repos"]["none"])
                    return

                for url, interval in monitored_repos:
                    interval_str = f"{interval}s" if interval else f"Default ({self.default_check_interval}s)"
                    repos_list.append(f"• <code>{url}</code> ({interval_str})")

            response_text = self.S["list_repos"]["header"] + "\n" + "\n".join(repos_list)
            await message.reply(response_text, disable_web_page_preview=True)

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error listing repos: {e}", exc_info=True)
            await message.reply(self.S["list_repos"]["error"])

    @command("git_interval")
    async def set_interval_cmd(self, _, message: Message):
        """Sets the update interval for a specific monitored repository."""
        chat_id = message.chat.id
        if len(message.command) < 3:
            await message.reply(self.S["git_interval"]["usage"])
            await message.reply(self.S["git_interval"]["usage_hint"])
            return

        repo_url_to_update = message.command[1].strip().rstrip('/')
        interval_str = message.command[2]

        try:
            seconds = int(interval_str)
            if seconds < self.min_interval:
                await message.reply(self.S["git_interval"]["min_interval"])
                return
        except ValueError:
            await message.reply(self.S["git_interval"]["invalid_interval"])
            return

        try:
            async with self.async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(MonitoredRepo)
                        .where(MonitoredRepo.chat_id == chat_id)
                        .where(MonitoredRepo.repo_url == repo_url_to_update)
                    )
                    repo_entry = result.scalar_one_or_none()

                    if repo_entry is None:
                        await message.reply(self.S["git_interval"]["not_found"].format(repo_url=repo_url_to_update))
                        return

                    repo_entry.check_interval = seconds
                    await session.flush()
                    repo_id = repo_entry.id
                    owner = repo_entry.owner
                    repo = repo_entry.repo

                    self.logger.info(f"Updating interval for repo {owner}/{repo} (ID: {repo_id}) in chat {chat_id} to {seconds}s.")

                await session.commit()

            stopped = await self._stop_monitor_task(chat_id, repo_id)
            if not stopped:
                self.logger.warning(f"No active task found for repo ID {repo_id} ({owner}/{repo}) after interval update. Starting new task anyway.")

            async with self.async_session() as session:
                updated_repo_entry = await session.get(MonitoredRepo, repo_id)
                if updated_repo_entry:
                    await self._start_monitor_task(updated_repo_entry)
                    await message.reply(self.S["git_interval"]["success"].format(owner=owner, repo=repo, seconds=seconds))
                else:
                    self.logger.error(f"Could not find repo entry {repo_id} after interval update commit. Cannot restart monitor.")
                    await message.reply(self.S["git_interval"]["error_restart"])

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error setting interval for {repo_url_to_update}: {e}", exc_info=True)
            await message.reply(self.S["git_interval"]["error_generic"])
