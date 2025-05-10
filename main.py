import asyncio
from pyrogram.types import Message
from pyrogram.errors import RPCError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from base.module import BaseModule, command
from .db import Base, MonitoredRepo
from . import db_ops
from .monitor import monitor_repo
from .utils import parse_github_url
from typing import Dict, Optional

class gitMonitorModule(BaseModule):
    def on_init(self):
        self.monitor_tasks: Dict[int, Dict[int, asyncio.Task]] = {} # chat_id -> repo_id -> Task
        self.github_token = self.module_config.get("api_token")
        self.default_check_interval = self.module_config.get("default_check_interval", 60)
        self.max_retries = self.module_config.get("max_retries", 5)
        self.min_interval = 10
        self.max_commits_in_notification = self.module_config.get("max_commits_to_list", 4)
        self.max_issues_in_notification = self.module_config.get("max_issues_to_list", 4)

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
                repos_to_monitor = await db_ops.get_all_active_repos(session)

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
        return self.S["help"].format(min_interval=self.min_interval)

    async def _start_monitor_task(self, repo_entry: MonitoredRepo):
        """Starts a monitor task for a given MonitoredRepo entry and stores it."""
        chat_id = repo_entry.chat_id
        repo_id = repo_entry.id
        check_interval = repo_entry.check_interval or self.default_check_interval
        check_interval = max(check_interval, self.min_interval)

        if chat_id not in self.monitor_tasks:
            self.monitor_tasks[chat_id] = {}

        if repo_id in self.monitor_tasks.get(chat_id, {}):
            existing_task = self.monitor_tasks[chat_id].pop(repo_id, None)
            if existing_task and not existing_task.done():
                self.logger.warning(f"Found existing task for repo ID {repo_id} in chat {chat_id}. Cancelling it before starting new one.")
                existing_task.cancel()
                try:
                    await existing_task
                except asyncio.CancelledError:
                    self.logger.info(f"Existing task for repo ID {repo_id} properly cancelled.")
                except Exception as e:
                    self.logger.error(f"Error awaiting existing task cancellation for {repo_id}: {e}")

        task = asyncio.create_task(
            self._monitor_wrapper(
                repo_entry=repo_entry,
                check_interval=check_interval,
            )
        )
        self.monitor_tasks[chat_id][repo_id] = task
        self.logger.info(f"Created monitor task for chat {chat_id}, repo ID {repo_id} ({repo_entry.owner}/{repo_entry.repo}), interval {check_interval}s")

    async def _monitor_wrapper(self, repo_entry: MonitoredRepo, check_interval: int):
        """Wraps monitor_repo to handle task completion, cleanup, and DB removal on permanent stop."""
        chat_id = repo_entry.chat_id
        repo_id = repo_entry.id
        repo_url = repo_entry.repo_url
        should_stop_permanently = False
        try:
            should_stop_permanently = await monitor_repo(
                bot=self.bot,
                chat_id=chat_id,
                repo_entry=repo_entry,
                check_interval=check_interval,
                max_retries=self.max_retries,
                github_token=self.github_token,
                strings=self.S,
                async_session_maker=self.async_session
            )
            if should_stop_permanently:
                self.logger.info(f"Monitor for repo ID {repo_id} ({repo_url}) requested permanent stop. Removing DB entry.")
                await self._remove_repo_from_db_and_task(chat_id, repo_id, task_already_stopped=True)

        except asyncio.CancelledError:
            self.logger.info(f"Monitor wrapper for repo ID {repo_id} ({repo_url}) was cancelled.")
        except Exception as e:
            self.logger.error(f"Unexpected error in monitor wrapper for repo ID {repo_id} ({repo_url}): {e}", exc_info=True)
            should_stop_permanently = True 
            await self._remove_repo_from_db_and_task(chat_id, repo_id, task_already_stopped=True)
        finally:
            if not should_stop_permanently:
                if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
                    del self.monitor_tasks[chat_id][repo_id]
                    if not self.monitor_tasks[chat_id]:
                        del self.monitor_tasks[chat_id]
                    self.logger.debug(f"Removed task entry for repo ID {repo_id} from chat {chat_id} in wrapper's finally block.")

    async def _stop_monitor_task(self, chat_id: int, repo_id: int) -> bool:
        """Stops a specific monitor task. Does NOT remove from DB."""
        if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
            task = self.monitor_tasks[chat_id].pop(repo_id)
            if not self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id]
            
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    self.logger.info(f"Monitor task for chat {chat_id}, repo ID {repo_id} successfully cancelled.")
                except Exception as e:
                    self.logger.error(f"Error awaiting task cancellation for repo ID {repo_id}: {e}")
            else:
                self.logger.info(f"Monitor task for chat {chat_id}, repo ID {repo_id} was already done.")
            return True
        return False

    async def _remove_repo_from_db_and_task(self, chat_id: int, repo_id: int, task_already_stopped: bool = False):
        """Stops task (if not already) and removes repo from DB."""
        if not task_already_stopped:
            await self._stop_monitor_task(chat_id, repo_id)
        else:
            if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id][repo_id]
                if not self.monitor_tasks[chat_id]:
                    del self.monitor_tasks[chat_id]

        try:
            async with self.async_session() as session:
                async with session.begin():
                    deleted = await db_ops.delete_repo_entry(session, chat_id, repo_id)
                if deleted:
                    self.logger.info(f"Removed repo ID {repo_id} for chat {chat_id} from database.")
                else:
                    self.logger.warning(f"Attempted to remove repo ID {repo_id} (chat {chat_id}) from DB, but it was not found or not owned by chat.")
        except Exception as e:
            self.logger.error(f"Failed to remove repo ID {repo_id} for chat {chat_id} from DB: {e}", exc_info=True)

    @command("git_add")
    async def add_repo_cmd(self, _, message: Message):
        """Adds a GitHub repository to monitor for this chat."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["add_repo"]["usage"])
            return

        repo_url = message.command[1].strip().rstrip('/')
        owner, repo_name_parsed = parse_github_url(repo_url)
        if not owner or not repo_name_parsed:
            await message.reply(self.S["add_repo"]["invalid_url"].format(repo_url=repo_url))
            return

        confirmation_msg = None
        try:
            confirmation_msg = await message.reply(
                self.S["add_repo"]["starting"].format(owner=owner, repo=repo_name_parsed)
            )
        except RPCError as e:
            self.logger.error(f"[{chat_id}] Failed to send 'starting' message for {repo_url}: {e}")


        try:
            async with self.async_session() as session:
                async with session.begin():
                    existing_repo = await db_ops.get_repo_by_url(session, chat_id, repo_url)
                    if existing_repo:
                        error_text = self.S["add_repo"]["already_monitoring"].format(owner=owner, repo=repo_name_parsed)
                        if confirmation_msg: await confirmation_msg.edit_text(error_text)
                        else: await message.reply(error_text)
                        return

                    new_repo_entry = await db_ops.create_repo_entry(
                        session,
                        chat_id=chat_id,
                        repo_url=repo_url,
                        owner=owner,
                        repo_name=repo_name_parsed,
                    )
                
                self.logger.info(f"Added repo {owner}/{repo_name_parsed} (ID: {new_repo_entry.id}) to DB for chat {chat_id}")
                await self._start_monitor_task(new_repo_entry)

                success_text = self.S["add_repo"]["success"].format(owner=owner, repo=repo_name_parsed)
                if confirmation_msg: await confirmation_msg.edit_text(success_text)
                else: await self.bot.send_message(chat_id, success_text)

        except IntegrityError:
            self.logger.warning(f"[{chat_id}] Integrity error (likely race condition) adding {repo_url}.")
            error_text = self.S["add_repo"]["already_monitoring"].format(owner=owner, repo=repo_name_parsed)
            if confirmation_msg: await confirmation_msg.edit_text(error_text)
            else: await message.reply(error_text)
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error adding repo {repo_url}: {e}", exc_info=True)
            error_text = self.S["add_repo"]["error_generic"]
            if confirmation_msg:
                try: await confirmation_msg.edit_text(error_text)
                except RPCError: pass
            else: await message.reply(error_text)

    @command("git_remove")
    async def remove_repo_cmd(self, _, message: Message):
        """Removes a specific GitHub repository from monitoring."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["remove_repo"]["usage"] + "\n" + self.S["remove_repo"]["usage_hint"])
            return

        repo_url_to_remove = message.command[1].strip().rstrip('/')
        
        repo_id_to_remove: Optional[int] = None
        owner_of_repo: Optional[str] = None
        name_of_repo: Optional[str] = None

        try:
            async with self.async_session() as session:
                repo_to_remove = await db_ops.get_repo_by_url(session, chat_id, repo_url_to_remove)

                if repo_to_remove is None:
                    await message.reply(self.S["remove_repo"]["not_found"].format(repo_url=repo_url_to_remove))
                    return
                
                repo_id_to_remove = repo_to_remove.id
                owner_of_repo = repo_to_remove.owner
                name_of_repo = repo_to_remove.repo

            await self._remove_repo_from_db_and_task(chat_id, repo_id_to_remove)

            await message.reply(self.S["remove_repo"]["success"].format(owner=owner_of_repo, repo=name_of_repo))

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error removing repo {repo_url_to_remove}: {e}", exc_info=True)
            await message.reply(self.S["remove_repo"]["error"])

    @command("git_list")
    async def list_repos_cmd(self, _, message: Message):
        """Lists the repositories currently being monitored in this chat."""
        chat_id = message.chat.id
        repos_list_text = []
        try:
            async with self.async_session() as session:
                monitored_repos = await db_ops.get_repos_for_chat(session, chat_id)

                if not monitored_repos:
                    await message.reply(self.S["list_repos"]["none"])
                    return

                for repo_entry in monitored_repos:
                    interval_val = repo_entry.check_interval or self.default_check_interval
                    interval_str = f"{interval_val}s"
                    # TODO: Add indicators for what's being monitored (commits, issues) if flags are added to DB
                    repos_list_text.append(f"• <code>{repo_entry.repo_url}</code> ({interval_str})")
            
            response_text = self.S["list_repos"]["header"] + "\n" + "\n".join(repos_list_text)
            await message.reply(response_text, disable_web_page_preview=True)

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error listing repos: {e}", exc_info=True)
            await message.reply(self.S["list_repos"]["error"])

    @command("git_interval")
    async def set_interval_cmd(self, _, message: Message):
        """Sets the update interval for a specific monitored repository."""
        chat_id = message.chat.id
        if len(message.command) < 3:
            await message.reply(self.S["git_interval"]["usage"] + "\n" + self.S["git_interval"]["usage_hint"])
            return

        repo_url_to_update = message.command[1].strip().rstrip('/')
        interval_str = message.command[2]

        try:
            seconds = int(interval_str)
            if seconds < self.min_interval:
                await message.reply(self.S["git_interval"]["min_interval"].format(min_interval=self.min_interval))
                return
        except ValueError:
            await message.reply(self.S["git_interval"]["invalid_interval"])
            return

        repo_id_for_restart: Optional[int] = None
        owner_for_reply: Optional[str] = None
        repo_name_for_reply: Optional[str] = None

        try:
            async with self.async_session() as session:
                async with session.begin():
                    repo_entry = await db_ops.get_repo_by_url(session, chat_id, repo_url_to_update)

                    if repo_entry is None:
                        await message.reply(self.S["git_interval"]["not_found"].format(repo_url=repo_url_to_update))
                        return

                    updated_repo_entry = await db_ops.set_repo_interval(session, repo_entry, seconds)
                    
                    repo_id_for_restart = updated_repo_entry.id
                    owner_for_reply = updated_repo_entry.owner
                    repo_name_for_reply = updated_repo_entry.repo
            
            self.logger.info(f"Interval updated for repo {owner_for_reply}/{repo_name_for_reply} (ID: {repo_id_for_restart}) "
                             f"in chat {chat_id} to {seconds}s. Restarting monitor task.")

            async with self.async_session() as session:
                final_repo_entry_for_restart = await db_ops.get_repo_by_id(session, repo_id_for_restart)
            
            if final_repo_entry_for_restart:
                await self._start_monitor_task(final_repo_entry_for_restart)
                await message.reply(self.S["git_interval"]["success"].format(
                    owner=owner_for_reply, repo=repo_name_for_reply, seconds=seconds)
                )
            else:
                self.logger.error(f"Could not find repo entry {repo_id_for_restart} after interval update commit. Cannot restart monitor.")
                await message.reply(self.S["git_interval"]["error_restart"])

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error setting interval for {repo_url_to_update}: {e}", exc_info=True)
            await message.reply(self.S["git_interval"]["error_generic"])
