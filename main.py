import asyncio
from pyrogram.types import Message
from pyrogram.errors import RPCError
from sqlalchemy import select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from base.module import BaseModule, command
from .db import Base, ChatState
from .monitor import monitor_repo
from .utils import parse_github_url

class gitMonitorModule(BaseModule):
    def on_init(self):
        self.monitor_tasks: dict[int, asyncio.Task] = {}
        self.github_token = self.module_config.get("api_token")
        self.default_check_interval = self.module_config.get("default_check_interval", 60)
        self.max_retries = self.module_config.get("max_retries", 5)
        if not self.github_token:
            self.logger.warning("Valid GitHub API token not found in config. Rate limits will be lower.")
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
                    check_interval = chat_state.check_interval or self.default_check_interval

                    # Ensure required data is present before starting monitor
                    if repo_url:
                        self.logger.info(f"Restarting monitor for chat {chat_id} on repo {repo_url}")
                        if chat_id in self.monitor_tasks:
                            self.monitor_tasks[chat_id].cancel()
                        self.monitor_tasks[chat_id] = asyncio.create_task(
                            monitor_repo(self.bot, chat_id, repo_url, check_interval, self.max_retries, self.github_token, self.S)
                        )
                        count += 1
                    else:
                        self.logger.warning(f"Skipping chat {chat_id}: Missing repo_url in DB.")
                self.logger.info(f"Successfully restarted {count} monitors.")
        except Exception as e:
            self.logger.error(f"Error loading chat states: {e}", exc_info=True)

    def on_unload(self):
        self.logger.info(f"Unloading module. Cancelling {len(self.monitor_tasks)} tasks...")
        for task in self.monitor_tasks.values():
            if not task.done():
                task.cancel()
        self.monitor_tasks.clear()
        self.logger.info("All monitoring tasks cancelled.")

    @property
    def help_page(self):
        return self.S["help"]

    async def _stop_monitor(self, session: AsyncSession, chat_id: int):
        if chat_id in self.monitor_tasks:
            if not self.monitor_tasks[chat_id].done():
                self.monitor_tasks[chat_id].cancel()
            del self.monitor_tasks[chat_id]
            self.logger.info(f"Cancelled monitor task for chat {chat_id}")
        await session.execute(delete(ChatState).where(ChatState.chat_id == chat_id))
        await session.commit()
        self.logger.info(f"Removed database entry for chat {chat_id}")

    @command("git_monitor")
    async def set_repo_cmd(self, _, message: Message):
        """Sets or updates the GitHub repository to monitor for this chat."""
        chat_id = message.chat.id

        if len(message.command) < 2:
            await message.reply(self.S["set_repo"]["usage"])
            return

        repo_url = message.command[1]
        owner, repo = parse_github_url(repo_url)
        if not owner or not repo:
            await message.reply(self.S["set_repo"]["invalid_url"].format(repo_url=repo_url))
            return

        try:
            confirmation_message = await message.reply(
                self.S["set_repo"]["starting"].format(owner=owner, repo=repo)
            )
        except RPCError as e:
            self.logger.error(f"[{chat_id}] Failed to send confirmation: {e}")
            confirmation_message = None

        try:
            async with self.async_session() as session:
                if chat_id in self.monitor_tasks:
                    self.logger.info(f"[{chat_id}] Stopping existing monitor.")
                    task = self.monitor_tasks.pop(chat_id)
                    if not task.done():
                        task.cancel()
                        try:
                            await asyncio.wait_for(task, timeout=1.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

                chat_state = await session.get(ChatState, chat_id)
                if not chat_state:
                    chat_state = ChatState(chat_id=chat_id)
                    session.add(chat_state)
                chat_state.repo_url = repo_url
                await session.commit()

                check_interval = chat_state.check_interval or self.default_check_interval
                new_task = asyncio.create_task(
                    monitor_repo(self.bot, chat_id, repo_url, check_interval, self.max_retries, self.github_token, self.S)
                )
                self.monitor_tasks[chat_id] = new_task

                success_text = self.S["set_repo"]["success"].format(owner=owner, repo=repo)
                if confirmation_message:
                    await confirmation_message.edit_text(success_text)
                else:
                    await self.bot.send_message(chat_id, success_text)
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error setting repo {repo_url}: {e}", exc_info=True)
            error_text = self.S["set_repo"]["error_generic"]
            if confirmation_message:
                await confirmation_message.edit_text(error_text)
            else:
                await message.reply(error_text)

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
                    await message.reply(self.S["stop"]["not_monitoring"])
                    return

                if not is_monitoring and db_has_repo:
                    self.logger.warning(f"[{chat_id}] Found DB state but no task. Cleaning DB.")

                await self._stop_monitor(session, chat_id)
            await message.reply(self.S["stop"]["success"])
        except Exception as e:
            self.logger.error(f"[{chat_id}] Error stopping monitor: {e}", exc_info=True)
            await message.reply(self.S["stop"]["error"])

    @command("git_interval")
    async def set_interval_cmd(self, _, message: Message):
        """Sets update interval for this chat."""
        chat_id = message.chat.id
        if len(message.command) < 2:
            await message.reply(self.S["git_interval"]["usage"])
            return

        try:
            seconds = int(message.command[1])
            if seconds < 10:
                await message.reply(self.S["git_interval"]["min_interval"])
                return
        except ValueError:
            await message.reply(self.S["git_interval"]["invalid_interval"])
            return

        async with self.async_session() as session:
            chat_state = await session.get(ChatState, chat_id)
            if not chat_state or not chat_state.repo_url:
                await message.reply(self.S["git_interval"]["no_repo"])
                return
            chat_state.check_interval = seconds
            await session.commit()
            await message.reply(self.S["git_interval"]["success"])

            if chat_id in self.monitor_tasks:
                task = self.monitor_tasks.pop(chat_id)
                if not task.done():
                    task.cancel()
                repo_url = chat_state.repo_url
                new_task = asyncio.create_task(
                    monitor_repo(self.bot, chat_id, repo_url, seconds, self.max_retries, self.github_token, self.S)
                )
                self.monitor_tasks[chat_id] = new_task
