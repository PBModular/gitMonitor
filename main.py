from pyrogram.types import Message
from base.module import BaseModule, command

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
    
    async def get_next_step(self, chat_id):
        return self.next_step.get(chat_id, "start")
    
    async def set_next_step(self, chat_id, step):
        self.next_step[chat_id] = step

    async def _monitor_repo(self, chat_id: int, start_message_id: int):
        repo_url_parts = self.repo_url.split('/')
        owner, repo = repo_url_parts[-2], repo_url_parts[-1]
        api_url = f"https://api.github.com/repos/{self.repo_url.split('/')[-2]}/{self.repo_url.split('/')[-1].replace('.git', '')}/commits"
        last_commit_sha = None
        while True:
            try:
                headers = {"Authorization": f"Token {self.github_token}"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(api_url) as response:
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

                    text = (
                        f"<b>New commit in:</b> {owner}/{repo}\n"
                        f"<b>Author:</b> {author}\n"
                        f"<b>Description:</b>\n {message}\n"
                        f"<b>SHA:</b> {sha}\n"
                        f"<b>URL:</b> {commit_url}"
                    )

                    await self.bot.send_message(chat_id, text, reply_to_message_id=start_message_id)

                last_commit_sha = data[0]["sha"]

            except Exception as e:
                self.logger.error(f"Error while monitoring repository {self.repo_url}: {e}")
            finally:
                await asyncio.sleep(60)


    @command("git_start")
    async def startcmd(self, _, message: Message):
        chat_id = message.chat.id

        # check if already started
        if chat_id in self.started_chats:
            await message.reply_text("The command has already been executed in this chat.")
            return

        # set next step to set repo url
        await self.set_next_step(chat_id, "set_repo_url")
        self.started_chats.add(chat_id) # add to started chats set

        # send welcome message
        welcome_text = "Welcome to gitMonitor_module! Please, use /git_src <url> to configure your repo for monitoring.\n Don't forget to past your GitHub token into main.py."
        await message.reply_text(welcome_text)

    @command("git_src")
    async def setrepocmd(self, _, message: Message):
        chat_id = message.chat.id

        # check if started with git_start
        if await self.get_next_step(chat_id) != "set_repo_url":
            await message.reply_text("First run the /git_start command.")
            return

        # set repo url and start monitoring
        repo_url = message.text.split(" ", 1)[1]
        self.repo_url = repo_url
        start_message = await message.reply_text(f"The {repo_url} repository will be monitored.")
        start_message_id = start_message.reply_to_message_id
        
        # start monitoring task
        self.monitor_task = asyncio.create_task(self._monitor_repo(chat_id, start_message_id))
        
    @command("git_reset")
    async def resetcmd(self, _, message: Message):
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            self.monitor_task = None
            self.repo_url = None
            await message.reply_text("Repository monitoring is stopped and reset.")
        else:
            await message.reply_text("Repository monitoring is not running.")