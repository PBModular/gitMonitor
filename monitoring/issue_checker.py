from typing import Optional

from .base_checker import BaseChecker
from ..processors.issue_processing import (
    identify_new_issues, format_single_issue_message, format_multiple_issues_message,
    identify_newly_closed_issues, format_closed_issue_message
)
from pyrogram.enums import ParseMode


class IssueChecker(BaseChecker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_last_issue_number: Optional[int] = None
        self.current_issue_etag: Optional[str] = None
        self.current_last_closed_ts: Optional[str] = None
        self.current_closed_issue_etag: Optional[str] = None
        self.max_issues_to_list = self.config.get("max_issues_to_list_in_notification", 4)

    async def load_initial_state(self) -> None:
        self.current_last_issue_number = self.repo_entry.last_known_issue_number
        self.current_issue_etag = self.repo_entry.issue_etag
        self.current_last_closed_ts = self.repo_entry.last_closed_issue_update_ts
        self.current_closed_issue_etag = self.repo_entry.closed_issue_etag

    async def clear_state_on_disable(self) -> None:
        self.logger.info(f"Disabling for {self.owner}/{self.repo_name}. Clearing ETags if set.")
        db_updates = {}
        if self.current_issue_etag:
            db_updates["issue_etag"] = None
            self.current_issue_etag = None
        if self.current_closed_issue_etag:
            db_updates["closed_issue_etag"] = None
            self.current_closed_issue_etag = None
        if db_updates:
            await self._update_db(db_updates)

    async def check(self) -> None:
        await self._check_new_open_issues()
        await self._check_newly_closed_issues()

    async def _check_new_open_issues(self) -> None:
        api_response = await self.api_client.fetch_issues(
            self.owner, self.repo_name, etag=self.current_issue_etag, per_page=30,
            sort='created', direction='desc', state='open'
        )
        db_updates = {}

        if api_response.status_code == 304:
            etag_from_304 = api_response.etag
            if etag_from_304 and etag_from_304 != self.current_issue_etag:
                self.current_issue_etag = etag_from_304
                db_updates["issue_etag"] = self.current_issue_etag
        elif api_response.status_code == 200:
            github_issues_data = api_response.data
            new_etag_from_response = api_response.etag
            if not isinstance(github_issues_data, list):
                self.logger.warning(f"Open: Invalid (non-list) data. Skipping.")
            else:
                newly_found, latest_num, is_initial = identify_new_issues(github_issues_data, self.current_last_issue_number)
                if is_initial:
                    self.logger.info(f"Open: Initial run. Latest issue #: {latest_num}.")
                    if latest_num is not None:
                        self.current_last_issue_number = latest_num
                        db_updates["last_known_issue_number"] = self.current_last_issue_number
                elif newly_found:
                    self.logger.info(f"Open: Found {len(newly_found)} new issue(s).")
                    self.current_last_issue_number = newly_found[0]['number']
                    db_updates["last_known_issue_number"] = self.current_last_issue_number
                    try:
                        msg = format_single_issue_message(newly_found[0], self.owner, self.repo_name, self.strings) if len(newly_found) == 1 \
                            else format_multiple_issues_message(newly_found, self.owner, self.repo_name, self.strings, self.max_issues_to_list)
                        await self.bot.send_message(self.chat_id, msg, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                    except Exception as e: self.logger.error(f"Open: Error sending notification: {e}", exc_info=True)
                elif latest_num is not None and latest_num > (self.current_last_issue_number or 0):
                    self.current_last_issue_number = latest_num
                    db_updates["last_known_issue_number"] = self.current_last_issue_number

            if new_etag_from_response and new_etag_from_response != self.current_issue_etag:
                self.current_issue_etag = new_etag_from_response
                db_updates["issue_etag"] = self.current_issue_etag
        if db_updates: await self._update_db(db_updates)

    async def _check_newly_closed_issues(self) -> None:
        api_response = await self.api_client.fetch_issues(
            self.owner, self.repo_name, etag=self.current_closed_issue_etag, per_page=30,
            sort='updated', direction='desc', state='closed', since=self.current_last_closed_ts
        )
        db_updates = {}

        if api_response.status_code == 304:
            etag_from_304 = api_response.etag
            if etag_from_304 and etag_from_304 != self.current_closed_issue_etag:
                self.current_closed_issue_etag = etag_from_304
                db_updates["closed_issue_etag"] = self.current_closed_issue_etag
        elif api_response.status_code == 200:
            github_data = api_response.data
            new_etag = api_response.etag
            if not isinstance(github_data, list):
                self.logger.warning(f"Closed: Invalid (non-list) data. Skipping.")
            else:
                newly_closed, latest_ts, is_initial = identify_newly_closed_issues(github_data, self.current_last_closed_ts)
                if is_initial:
                    self.logger.info(f"Closed: Initial poll. Baseline updated_at: {latest_ts or 'None'}.")
                elif newly_closed:
                    self.logger.info(f"Closed: Found {len(newly_closed)} newly closed issue(s).")
                    for issue_data in reversed(newly_closed):
                        try:
                            msg = format_closed_issue_message(issue_data, self.owner, self.repo_name, self.strings)
                            await self.bot.send_message(self.chat_id, msg, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                        except Exception as e: self.logger.error(f"Closed: Error sending notification for #{issue_data.get('number')}: {e}", exc_info=True)

                if latest_ts and latest_ts != self.current_last_closed_ts:
                    self.current_last_closed_ts = latest_ts
                    db_updates["last_closed_issue_update_ts"] = self.current_last_closed_ts

            if new_etag and new_etag != self.current_closed_issue_etag:
                self.current_closed_issue_etag = new_etag
                db_updates["closed_issue_etag"] = self.current_closed_issue_etag
        if db_updates: await self._update_db(db_updates)
