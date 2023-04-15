from pyrogram.types import Message
from base.module import BaseModule, command

from sqlalchemy import select


from .db import Base, ChatState
import asyncio
import aiohttp


class gitMonitorModule(BaseModule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.repo_url = None
        self.monitor_task = None
        self.github_token = "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"
        self.next_step = {}
        self.started_chats = set()

    @property
    def db_meta(self):
        return Base.metadata
    
    async def on_db_ready(self):
        async with self.db.session_maker() as session:
            # load chat states from database
            chat_states = await session.scalars(select(ChatState))
            for chat_state in chat_states:
                chat_id, repo_url, start_message_id, next_step = chat_state.chat_id, chat_state.repo_url, chat_state.start_message_id, chat_state.next_step
                self.repo_url = repo_url
                self.next_step[chat_id] = next_step
                if start_message_id is not None:
                    self.monitor_task = asyncio.create_task(self._monitor_repo(chat_id, start_message_id))
                self.started_chats.add(chat_id)
    
    async def set_next_step(self, chat_id, step):
        try:
            async with self.db.session_maker() as session:
                # save next step to database
                chat_state = await session.scalar(select(ChatState).where(ChatState.chat_id == chat_id))
                if chat_state:
                    chat_state.next_step = step
                else:
                    chat_state = ChatState(chat_id=chat_id, next_step=step)
                    session.add(chat_state)
                await session.commit()
                self.next_step[chat_id] = step
        except Exception as e:
            # handle exception
            self.logger.error(f"Error while saving next step for chat {chat_id}: {e}")

    async def get_next_step(self, chat_id):
        return self.next_step.get(chat_id, "start")

    async def _monitor_repo(self, chat_id: int, start_message_id: int):
        repo_url_parts = self.repo_url.split('/')
        owner, repo = repo_url_parts[-2], repo_url_parts[-1]
        api_url = f"https://api.github.com/repos/{self.repo_url.split('/')[-2]}/{self.repo_url.split('/')[-1].replace('.git', '')}/commits"
        last_commit_sha = None
        etag = None
        retries = 0
        
        while True:
            try:
                headers = {"Authorization": f"Token {self.github_token}"}
                if etag:
                    headers["If-None-Match"] = etag

                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(api_url) as response:
                        if response.status == 304:
                            await asyncio.sleep(60)
                            continue

                        response.raise_for_status()
                        data = await response.json()

                if not data:
                    await asyncio.sleep(60)
                    continue

                if last_commit_sha is not None and data[0]["sha"] != last_commit_sha:
                    commit = data[0]["commit"]
                    commit_url = data[0]["html_url"]
                    author = commit["author"]["name"]
                    message = commit["message"]
                    sha = data[0]["sha"][:5]

                    text = self.S["_monitor_repo"]["text"].format(owner=owner, repo=repo, author=author, 
                                                                  message=message, sha=sha, commit_url=commit_url)
                    await self.bot.send_message(chat_id, text, reply_to_message_id=start_message_id)

                last_commit_sha = data[0]["sha"]
                etag = response.headers.get("ETag")
                retries = 0

            except aiohttp.ClientError as e:
                if retries >= 5:
                    self.logger.error(f"Error while monitoring repository {self.repo_url}: {e}")
                    raise
                retries += 1
                self.logger.warning(f"Error while monitoring repository {self.repo_url}: {e}. Retrying in {60 * retries} seconds")
                
                await asyncio.sleep(60 * retries)

            except Exception as e:
                self.logger.error(f"Error while monitoring repository {self.repo_url}: {e}")
                raise
            finally:
                await asyncio.sleep(60)

    @command("git_start")
    async def startcmd(self, _, message: Message):
        chat_id = message.chat.id

        # check if already started
        if chat_id in self.started_chats:
            await message.reply_text(self.S["git_start"]["already_started"])
            return

        # set next step to set repo url
        await self.set_next_step(chat_id, "set_repo_url")
        self.started_chats.add(chat_id) # add to started chats set

        # send welcome message
        await message.reply_text(self.S["git_start"]["welcome"])

    @command("git_src")
    async def setrepocmd(self, _, message: Message):
        chat_id = message.chat.id

        # check if started with git_start
        if await self.get_next_step(chat_id) != "set_repo_url":
            await message.reply_text(self.S["git_src"]["err_start"])
            return

        # set repo url and start monitoring
        repo_url = message.text.split(" ", 1)[1]
        self.repo_url = repo_url
        start_message = await message.reply_text(self.S["git_src"]["monitoring"].format(repo_url=repo_url))
        start_message_id = start_message.reply_to_message_id
        
        async with self.db.session_maker() as session:
            # set chat state in database
            chat_state = ChatState(chat_id=chat_id, repo_url=repo_url, start_message_id=start_message_id)
            session.add(chat_state)
            await session.commit()
        
        # start monitoring task
        self.monitor_task = asyncio.create_task(self._monitor_repo(chat_id, start_message_id))
        
    @command("git_reset")
    async def resetcmd(self, _, message: Message):
        chat_id = message.chat.id
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            self.monitor_task = None
            self.repo_url = None
            
            await message.reply_text(self.S["git_reset"]["success"])
        else:
            await message.reply_text(self.S["git_reset"]["err"])
            
  