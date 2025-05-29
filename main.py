import asyncio
import logging
from pyrogram.types import Message, CallbackQuery
from pyrogram.errors import RPCError
from pyrogram import filters
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from base.module import BaseModule, command, callback_query, allowed_for
from .db import Base, MonitoredRepo
from . import db_ops
from .monitoring.orchestrator import RepoMonitorOrchestrator
from .utils import parse_github_url
from .buttons.buttons_handler import send_repo_selection_list, send_repo_settings_panel, handle_settings_callback
from typing import Dict, Optional, Any

class gitMonitorModule(BaseModule):
    def on_init(self):
        self.monitor_tasks: Dict[int, Dict[int, asyncio.Task]] = {} # chat_id -> repo_id -> Task
        self.default_check_interval = self.module_config.get("default_check_interval", 60)
        self.max_retries = self.module_config.get("max_retries", 5)
        self.min_interval = 10 
        self.active_branch: Dict[int, Dict[str, Any]] = {}

        self.github_token = self.module_config.get("api_token")
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
                    self.logger.info(f"Restarting monitor for chat {repo_entry.chat_id} on repo {repo_entry.repo_url} "
                                     f"(DB ID: {repo_entry.id}, Branch: {repo_entry.branch or 'default'})")
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
                if repo_id in self.monitor_tasks[chat_id]:
                    del self.monitor_tasks[chat_id][repo_id]
            if not self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id]
        self.logger.info(f"Cancelled {count} monitoring tasks.")
        if hasattr(self.bot, 'ext_module_gitMonitorModule'):
            del self.bot.ext_module_gitMonitorModule

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

        existing_task = self.monitor_tasks.get(chat_id, {}).pop(repo_id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()
            try:
                await existing_task
            except asyncio.CancelledError:
                self.logger.info(f"Existing task for repo ID {repo_id} properly cancelled.")
            except Exception as e:
                self.logger.error(f"Error awaiting existing task cancellation for {repo_id}: {e}")

        if not self.monitor_tasks.get(chat_id):
            if chat_id in self.monitor_tasks:
                del self.monitor_tasks[chat_id]

        if not repo_entry.owner or not repo_entry.repo:
            self.logger.error(f"Attempted to start monitor for repo ID {repo_entry.id} with invalid owner/repo. Skipping.")
            return

        task_logger_name = f"MonitorTask[{chat_id}][{repo_id}][{repo_entry.owner}/{repo_entry.repo}{f'@{repo_entry.branch}' if repo_entry.branch else ''}]"
        task_specific_logger = self.logger.getChild(task_logger_name)

        orchestrator_module_config = {
            "max_commits": self.module_config.get("max_commits", 4),
            "max_issues": self.module_config.get("max_issues", 4),
            "max_tags": self.module_config.get("max_tags", 3),
        }
        task = asyncio.create_task(
            self._monitor_wrapper(
                repo_entry=repo_entry,
                check_interval=check_interval,
                module_config_for_orchestrator=orchestrator_module_config,
                task_logger=task_specific_logger
            )
        )
        if chat_id not in self.monitor_tasks:
            self.monitor_tasks[chat_id] = {}
        self.monitor_tasks[chat_id][repo_id] = task
        self.logger.info(f"Created/restarted monitor task for chat {chat_id}, repo ID {repo_id} ({repo_entry.owner}/{repo_entry.repo}, "
                         f"Branch: {repo_entry.branch or 'default'}, Interval: {check_interval}s). "
                         f"C:{'✓' if repo_entry.monitor_commits else '✗'} I:{'✓' if repo_entry.monitor_issues else '✗'} T:{'✓' if repo_entry.monitor_tags else '✗'}")

    async def _monitor_wrapper(self, repo_entry: MonitoredRepo, check_interval: int, module_config_for_orchestrator: Dict[str, Any], task_logger: logging.Logger):
        chat_id = repo_entry.chat_id
        repo_id = repo_entry.id

        orchestrator = RepoMonitorOrchestrator(
            bot=self.bot,
            chat_id=chat_id,
            repo_entry=repo_entry,
            base_check_interval=check_interval,
            max_retries=self.max_retries,
            github_token=self.github_token,
            strings=self.S,
            async_session_maker=self.async_session,
            module_config=module_config_for_orchestrator,
            parent_logger=task_logger
        )

        should_stop_permanently = False
        try:
            should_stop_permanently = await orchestrator.run()

            if should_stop_permanently:
                task_logger.info(f"Monitor requested permanent stop by orchestrator. Removing DB entry.")
                await self._remove_repo_from_db_and_task(chat_id, repo_id, task_already_stopped=True)
        except asyncio.CancelledError:
            task_logger.info(f"Monitor wrapper for repo ID {repo_id} was cancelled.")
        except Exception as e:
            task_logger.error(f"Unexpected error in monitor wrapper for repo ID {repo_id}: {e}", exc_info=True)
            should_stop_permanently = True
            await self._remove_repo_from_db_and_task(chat_id, repo_id, task_already_stopped=True)
        finally:
            if not should_stop_permanently:
                if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
                    if self.monitor_tasks[chat_id][repo_id] is asyncio.current_task():
                        del self.monitor_tasks[chat_id][repo_id]
                        if not self.monitor_tasks[chat_id]:
                            del self.monitor_tasks[chat_id]
            task_logger.info(f"Monitor wrapper for repo ID {repo_id} finished. Permanent stop: {should_stop_permanently}")

    async def _stop_monitor_task(self, chat_id: int, repo_id: int) -> bool:
        """Stops a specific monitor task. Does NOT remove from DB."""
        task_found_and_stopped = False
        if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
            task = self.monitor_tasks[chat_id].pop(repo_id)
            if not self.monitor_tasks[chat_id]:
                del self.monitor_tasks[chat_id]
            
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    self.logger.info(f"Monitor task for chat {chat_id}, repo ID {repo_id} successfully cancelled.")
                except Exception as e:
                    self.logger.error(f"Error awaiting task cancellation for repo ID {repo_id}: {e}")
            elif task:
                self.logger.info(f"Monitor task for chat {chat_id}, repo ID {repo_id} was already done.")
            task_found_and_stopped = True
        return task_found_and_stopped

    async def _remove_repo_from_db_and_task(self, chat_id: int, repo_id: int, task_already_stopped: bool = False):
        """Stops task (if not already) and removes repo from DB."""
        if not task_already_stopped:
            await self._stop_monitor_task(chat_id, repo_id)
        else:
            if chat_id in self.monitor_tasks and repo_id in self.monitor_tasks[chat_id]:
                self.monitor_tasks[chat_id].pop(repo_id, None)
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

    @allowed_for(["owner", "chat_admins"])
    @command("git_add")
    async def add_repo_cmd(self, _, message: Message):
        """Adds a GitHub repository to monitor for this chat."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["add_repo"]["usage"])
            return

        repo_url = message.command[1].strip().rstrip('/')
        branch_name: Optional[str] = None
        if len(message.command) > 2:
            branch_name = message.command[2].strip()
            if not branch_name:
                branch_name = None 
        
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
                        branch=branch_name
                    )
                
                self.logger.info(f"Added repo {owner}/{repo_name_parsed} (Branch: {branch_name or 'default'}, ID: {new_repo_entry.id}) to DB for chat {chat_id}")
                await self._start_monitor_task(new_repo_entry)

                branch_display_for_msg = branch_name if branch_name else self.S["git_settings"]["default_branch_display"]
                success_text = self.S["add_repo"]["success"].format(
                    owner=owner, 
                    repo=repo_name_parsed, 
                    branch_name_display=branch_display_for_msg
                )
                if confirmation_msg: await confirmation_msg.edit_text(success_text)
                else: await self.bot.send_message(chat_id, success_text)

        except IntegrityError:
            self.logger.warning(f"[{chat_id}] Integrity error (likely race condition) adding {repo_url}.")
            error_text = self.S["add_repo"]["already_monitoring"].format(owner=owner, repo=repo_name_parsed)
            if confirmation_msg: 
                try: await confirmation_msg.edit_text(error_text)
                except RPCError: pass
            else: await message.reply(error_text)
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error adding repo {repo_url}: {e}", exc_info=True)
            error_text = self.S["add_repo"]["error_generic"]
            if confirmation_msg:
                try: await confirmation_msg.edit_text(error_text)
                except RPCError: pass
            else: await message.reply(error_text)

    @allowed_for(["owner", "chat_admins"])
    @command("git_remove")
    async def remove_repo_cmd(self, _, message: Message):
        """Removes a specific GitHub repository from monitoring."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["remove_repo"]["usage"] + "\n" + self.S["remove_repo"]["usage_hint"])
            return

        repo_identifier = message.command[1].strip().rstrip('/')
        
        repo_to_remove_entry: Optional[MonitoredRepo] = None

        try:
            async with self.async_session() as session:
                if repo_identifier.isdigit():
                    repo_to_remove_entry = await db_ops.get_repo_by_id(session, int(repo_identifier))
                    if repo_to_remove_entry and repo_to_remove_entry.chat_id != chat_id:
                        repo_to_remove_entry = None 
                else:
                    repo_to_remove_entry = await db_ops.get_repo_by_url(session, chat_id, repo_identifier)

                if repo_to_remove_entry is None:
                    await message.reply(self.S["remove_repo"]["not_found_id_url"].format(identifier=repo_identifier))
                    return
                
                repo_id_to_remove = repo_to_remove_entry.id
                owner_of_repo = repo_to_remove_entry.owner
                name_of_repo = repo_to_remove_entry.repo

            await self._remove_repo_from_db_and_task(chat_id, repo_id_to_remove)

            await message.reply(self.S["remove_repo"]["success"].format(owner=owner_of_repo, repo=name_of_repo))

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error removing repo {repo_identifier}: {e}", exc_info=True)
            await message.reply(self.S["remove_repo"]["error"])

    @command("git_list")
    async def list_repos_cmd(self, _, message: Message):
        """Lists the repositories currently being monitored in this chat."""
        chat_id = message.chat.id
        repos_list_text_parts = []
        try:
            async with self.async_session() as session:
                monitored_repos = await db_ops.get_repos_for_chat(session, chat_id)

                if not monitored_repos:
                    await message.reply(self.S["list_repos"]["none"])
                    return

                for repo_entry in monitored_repos:
                    interval_val = repo_entry.check_interval or self.default_check_interval
                    interval_str = f"{interval_val}s"
                    commit_status = self.S["list_repos"]["status_enabled"] if repo_entry.monitor_commits else self.S["list_repos"]["status_disabled"]
                    issue_status = self.S["list_repos"]["status_enabled"] if repo_entry.monitor_issues else self.S["list_repos"]["status_disabled"]
                    tag_status = self.S["list_repos"]["status_enabled"] if repo_entry.monitor_tags else self.S["list_repos"]["status_disabled"]
                    branch_display = repo_entry.branch or self.S["git_settings"]["default_branch_display"]


                    repos_list_text_parts.append(
                        self.S["list_repos"]["repo_line_format"].format(
                            id=repo_entry.id,
                            repo_url=repo_entry.repo_url,
                            branch_name_display=branch_display,
                            interval_str=interval_str,
                            commit_status=commit_status,
                            issue_status=issue_status,
                            tag_status=tag_status
                        )
                    )

            response_text = self.S["list_repos"]["header"] + "\n" + "\n".join(repos_list_text_parts)
            await message.reply(response_text, disable_web_page_preview=True)

        except Exception as e:
            self.logger.error(f"[{chat_id}] Error listing repos: {e}", exc_info=True)
            await message.reply(self.S["list_repos"]["error"])

    @allowed_for(["owner", "chat_admins"])
    @command("git_interval")
    async def set_interval_cmd(self, _, message: Message):
        """Sets the update interval for a specific monitored repository."""
        chat_id = message.chat.id
        if len(message.command) < 3:
            await message.reply(self.S["git_interval"]["usage"] + "\n" + self.S["git_interval"]["usage_hint"])
            return

        repo_identifier = message.command[1].strip().rstrip('/')
        interval_str = message.command[2]

        try:
            seconds = int(interval_str)
            if seconds < self.min_interval:
                await message.reply(self.S["git_interval"]["min_interval"].format(min_interval=self.min_interval))
                return
        except ValueError:
            await message.reply(self.S["git_interval"]["invalid_interval"])
            return

        repo_to_update: Optional[MonitoredRepo] = None
        try:
            async with self.async_session() as session:
                async with session.begin():
                    if repo_identifier.isdigit():
                        repo_to_update = await db_ops.get_repo_by_id(session, int(repo_identifier))
                        if repo_to_update and repo_to_update.chat_id != chat_id:
                             repo_to_update = None
                    else:
                        repo_to_update = await db_ops.get_repo_by_url(session, chat_id, repo_identifier)

                    if repo_to_update is None:
                        await message.reply(self.S["git_interval"]["not_found_id_url"].format(identifier=repo_identifier))
                        return

                    updated_repo_entry = await db_ops.set_repo_interval(session, repo_to_update, seconds)

                self.logger.info(f"Interval updated for repo {updated_repo_entry.owner}/{updated_repo_entry.repo} (ID: {updated_repo_entry.id}) "
                                 f"in chat {chat_id} to {seconds}s. Restarting monitor task.")

                await self._start_monitor_task(updated_repo_entry)
                await message.reply(self.S["git_interval"]["success"].format(
                    owner=updated_repo_entry.owner, repo=updated_repo_entry.repo, seconds=seconds)
                )
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error setting interval for {repo_identifier}: {e}", exc_info=True)
            await message.reply(self.S["git_interval"]["error_generic"])

    @allowed_for(["owner", "chat_admins"])
    @command("git_settings")
    async def repo_settings_cmd(self, _, message: Message):
        """Configures monitoring options for a specific repository."""
        chat_id = message.chat.id
        
        if len(message.command) > 1:
            identifier = message.command[1].strip().rstrip('/')
            repo_entry: Optional[MonitoredRepo] = None
            async with self.async_session() as session:
                if identifier.isdigit():
                    repo_entry = await db_ops.get_repo_by_id(session, int(identifier))
                    if repo_entry and repo_entry.chat_id != chat_id:
                        repo_entry = None
                else:
                    repo_entry = await db_ops.get_repo_by_url(session, chat_id, identifier)
            
            if repo_entry:
                await send_repo_settings_panel(message, repo_entry, self.S, current_list_page=0, module_instance=self)
            else:
                await message.reply(self.S["git_settings"]["repo_not_found"].format(identifier=identifier))
        else:
            async with self.async_session() as session:
                repos = await db_ops.get_repos_for_chat(session, chat_id)
            if not repos:
                await message.reply(self.S["list_repos"]["none"])
                return
            await send_repo_selection_list(message, repos, 0, self.S, self)

    @allowed_for(["owner", "chat_admins"])
    @callback_query(filters.regex(r"^gitsettings_.*"))
    async def git_settings_callback_handler(self, _, call: CallbackQuery):
        """Handles all callbacks starting with gitsettings_"""
        if not hasattr(self.bot, 'ext_module_gitMonitorModule'):
            setattr(self.bot, 'ext_module_gitMonitorModule', self)
        await handle_settings_callback(call, self)
