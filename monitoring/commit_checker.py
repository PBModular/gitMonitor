from typing import Optional

from .base_checker import BaseChecker
from ..processors.commit_processing import identify_new_commits, format_single_commit_message, format_multiple_commits_message
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError


class CommitChecker(BaseChecker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_last_sha: Optional[str] = None
        self.current_commit_etag: Optional[str] = None
        self.max_commits_to_list = self.config.get("max_commits_to_list_in_notification", 4)

    async def load_initial_state(self) -> None:
        self.current_last_sha = self.repo_entry.last_commit_sha
        self.current_commit_etag = self.repo_entry.commit_etag

    async def clear_state_on_disable(self) -> None:
        self.logger.info(f"Disabling for {self.owner}/{self.repo_name}. Clearing ETag if set.")
        if self.current_commit_etag:
            await self._update_db({"commit_etag": None})
        self.current_commit_etag = None

    async def check(self) -> None:
        api_response = await self.api_client.fetch_commits(
            self.owner, self.repo_name, etag=self.current_commit_etag, per_page=30
        )

        db_updates = {}

        if api_response.status_code == 304:
            etag_from_304 = api_response.etag
            if etag_from_304 and etag_from_304 != self.current_commit_etag:
                self.current_commit_etag = etag_from_304
                db_updates["commit_etag"] = self.current_commit_etag
        
        elif api_response.status_code == 200:
            github_commits_data = api_response.data
            new_etag_from_response = api_response.etag

            if not github_commits_data or not isinstance(github_commits_data, list) or not github_commits_data[0].get("sha"):
                self.logger.warning(f"Invalid or empty commit data despite 200 OK. Skipping.")
            else:
                newly_found_commits, latest_sha_on_github, is_initial, force_pushed_or_many = \
                    identify_new_commits(github_commits_data, self.current_last_sha)

                if is_initial:
                    self.logger.info(f"Initial run. Latest SHA: {latest_sha_on_github[:7] if latest_sha_on_github else 'None'}")
                    if latest_sha_on_github:
                        self.current_last_sha = latest_sha_on_github
                        db_updates["last_commit_sha"] = self.current_last_sha
                
                elif newly_found_commits:
                    self.logger.info(f"Found {len(newly_found_commits)} new commit(s).")
                    if force_pushed_or_many:
                        self.logger.warning(f"Previously known SHA {self.current_last_sha[:7] if self.current_last_sha \
                                            else 'None'} not found. Possible force push or >30 new commits.")

                    new_sha_to_store = newly_found_commits[0]['sha']
                    prev_sha_for_msg = self.current_last_sha
                    self.current_last_sha = new_sha_to_store
                    db_updates["last_commit_sha"] = self.current_last_sha
                    
                    try:
                        if len(newly_found_commits) == 1:
                            message_text = format_single_commit_message(newly_found_commits[0], self.owner, self.repo_name, self.strings)
                        else:
                            message_text = format_multiple_commits_message(
                                newly_found_commits, self.owner, self.repo_name, self.strings,
                                previous_known_sha=prev_sha_for_msg,
                                max_to_list=self.max_commits_to_list
                            )
                        await self.bot.send_message(self.chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                    except RPCError as rpc_e:
                        self.logger.error(f"Failed to send Telegram message: {rpc_e}.")
                    except Exception as send_e:
                        self.logger.error(f"Error preparing/sending notification: {send_e}", exc_info=True)

                elif latest_sha_on_github and latest_sha_on_github != self.current_last_sha:
                    self.current_last_sha = latest_sha_on_github
                    db_updates["last_commit_sha"] = self.current_last_sha

            if new_etag_from_response and new_etag_from_response != self.current_commit_etag:
                self.logger.info(f"ETag changed on 200 OK. Updating.")
                self.current_commit_etag = new_etag_from_response
                db_updates["commit_etag"] = self.current_commit_etag

        if db_updates:
            await self._update_db(db_updates)
