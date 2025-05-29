from typing import Optional
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from .base_checker import BaseChecker
from ..processors.tag_processing import identify_new_tags, format_new_tag_message, format_multiple_tags_message


class TagChecker(BaseChecker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_last_tag_name: Optional[str] = None
        self.current_tag_etag: Optional[str] = None
        self.max_tags_to_list = self.config.get("max_tags", 3)

    async def load_initial_state(self) -> None:
        self.current_last_tag_name = self.repo_entry.last_known_tag_name
        self.current_tag_etag = self.repo_entry.tag_etag

    async def clear_state_on_disable(self) -> None:
        self.logger.info(f"Disabling for {self.owner}/{self.repo_name}. Clearing ETag if set.")
        if self.current_tag_etag:
            await self._update_db({"tag_etag": None})
        self.current_tag_etag = None

    async def check(self) -> None:
        api_response = await self.api_client.fetch_tags(
            self.owner, self.repo_name, etag=self.current_tag_etag, per_page=30
        )
        db_updates = {}

        if api_response.status_code == 304:
            etag_from_304 = api_response.etag
            if etag_from_304 and etag_from_304 != self.current_tag_etag:
                self.current_tag_etag = etag_from_304
                db_updates["tag_etag"] = self.current_tag_etag

        elif api_response.status_code == 200:
            github_tags_data = api_response.data
            new_etag_from_response = api_response.etag
            if not github_tags_data or not isinstance(github_tags_data, list):
                self.logger.warning(f"Invalid or empty tag data from GitHub API despite 200 OK. Skipping this check.")
            else:
                newly_found, latest_name, is_initial, known_tag_not_found = \
                    identify_new_tags(github_tags_data, self.current_last_tag_name)
                if is_initial:
                    self.logger.info(f"Initial run. Latest tag: {latest_name or 'None'}")
                    if latest_name:
                        self.current_last_tag_name = latest_name
                        db_updates["last_known_tag_name"] = self.current_last_tag_name
                elif newly_found:
                    self.logger.info(f"Found {len(newly_found)} new tag(s) for {self.owner}/{self.repo_name}.")
                    new_tag_name_to_store_in_db = newly_found[0]['name']
                    self.current_last_tag_name = new_tag_name_to_store_in_db
                    db_updates["last_known_tag_name"] = self.current_last_tag_name
                    try:
                        if len(newly_found) == 1:
                            message_text = format_new_tag_message(newly_found[0], self.owner, self.repo_name, self.strings)
                        else:
                            message_text = format_multiple_tags_message(
                                newly_found, self.owner, self.repo_name, self.strings,
                                max_to_list=self.max_tags_to_list
                            )

                        await self.bot.send_message(self.chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                    except RPCError as rpc_e:
                        self.logger.error(f"Failed to send Telegram message for new tags: {rpc_e}.")
                    except Exception as send_e:
                        self.logger.error(f"Unexpected error preparing/sending tag notification: {send_e}", exc_info=True)

                elif latest_name and latest_name != self.current_last_tag_name:
                    self.current_last_tag_name = latest_name
                    db_updates["last_known_tag_name"] = self.current_last_tag_name

            if new_etag_from_response and new_etag_from_response != self.current_tag_etag:
                self.current_tag_etag = new_etag_from_response
                db_updates["tag_etag"] = self.current_tag_etag
        if db_updates: await self._update_db(db_updates)
