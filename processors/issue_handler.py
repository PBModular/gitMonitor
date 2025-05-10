import logging
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from ..api.github_api import GitHubAPIClient, APIError
from .issue_processing import (
    identify_new_issues, format_single_issue_message, format_multiple_issues_message,
    identify_newly_closed_issues, format_closed_issue_message
)
from .. import db_ops

async def _handle_new_open_issue_checks(
    api_client: GitHubAPIClient,
    owner: str,
    repo: str,
    repo_db_id: int,
    current_last_issue_number: Optional[int],
    current_issue_etag: Optional[str],
    async_session_maker: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    strings: Dict[str, Any],
    chat_id: int,
    bot,
    max_issues_to_list_in_notification: int
) -> Tuple[Optional[int], Optional[str]]:
    """
    Handles checking for new (open) issues.
    Fetches issues sorted by creation date.
    """
    api_response = await api_client.fetch_issues(
        owner, repo, etag=current_issue_etag, per_page=30,
        sort='created', direction='desc', state='open'
    )

    next_last_issue_number = current_last_issue_number
    next_issue_etag = current_issue_etag

    if api_response.status_code == 304: # Not Modified
        etag_from_304 = api_response.etag
        logger.debug(f"Issues (Open): No new data for {owner}/{repo} (304 Not Modified). ETag: {etag_from_304[:7] if etag_from_304 else 'None'}")
        if etag_from_304 and etag_from_304 != current_issue_etag:
            logger.info(f"Issues (Open): ETag changed on 304 for {owner}/{repo}. Updating.")
            next_issue_etag = etag_from_304
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, issue_etag=next_issue_etag)
        return next_last_issue_number, next_issue_etag

    # Successful response (200 OK)
    github_issues_data = api_response.data
    new_etag_from_response = api_response.etag
    db_updates = {}

    if not isinstance(github_issues_data, list):
        logger.warning(f"Issues (Open): Invalid (non-list) issue data from GitHub API for {owner}/{repo}. Skipping.")
        if new_etag_from_response and new_etag_from_response != current_issue_etag:
            logger.info(f"Issues (Open): ETag changed on (faulty non-list) 200 OK for {owner}/{repo}. Updating.")
            db_updates["issue_etag"] = new_etag_from_response
    elif not github_issues_data:
        logger.debug(f"Issues (Open): Received empty list for {owner}/{repo}, no new open issues based on current criteria.")
        if new_etag_from_response and new_etag_from_response != current_issue_etag:
            logger.info(f"Issues (Open): ETag changed on 200 OK (empty list) for {owner}/{repo}. Updating.")
            db_updates["issue_etag"] = new_etag_from_response
    else:
        newly_found_issues, latest_issue_number_on_github, is_initial = \
            identify_new_issues(github_issues_data, current_last_issue_number)

        if is_initial:
            logger.info(f"Issues (Open): Initial run for {owner}/{repo}. Latest issue number from API: {latest_issue_number_on_github}.")
            if latest_issue_number_on_github is not None:
                next_last_issue_number = latest_issue_number_on_github
                db_updates["last_known_issue_number"] = next_last_issue_number
        
        elif newly_found_issues:
            logger.info(f"Issues (Open): Found {len(newly_found_issues)} new open issue(s) for {owner}/{repo}.")
            new_issue_number_to_store_in_db = newly_found_issues[0]['number']
            next_last_issue_number = new_issue_number_to_store_in_db
            db_updates["last_known_issue_number"] = next_last_issue_number
            
            try:
                if len(newly_found_issues) == 1:
                    message_text = format_single_issue_message(newly_found_issues[0], owner, repo, strings)
                else:
                    message_text = format_multiple_issues_message(
                        newly_found_issues, owner, repo, strings, max_issues_to_list_in_notification
                    )
                await bot.send_message(chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                logger.info(f"Issues (Open): Sent notification for {len(newly_found_issues)} new open issue(s) to {chat_id}.")
            except RPCError as rpc_e:
                logger.error(f"Issues (Open): Failed to send Telegram message for {owner}/{repo}: {rpc_e}.")
            except Exception as send_e:
                logger.error(f"Issues (Open): Error preparing/sending notification for {owner}/{repo}: {send_e}", exc_info=True)

        elif latest_issue_number_on_github is not None and latest_issue_number_on_github > (current_last_issue_number or 0):
            logger.info(f"Issues (Open): No new issues reported by ID logic, but latest API issue number {latest_issue_number_on_github} "
                        f"is greater than known {current_last_issue_number}. Updating known number.")
            next_last_issue_number = latest_issue_number_on_github
            db_updates["last_known_issue_number"] = next_last_issue_number
        else:
            logger.debug(f"Issues (Open): No new open issues for {owner}/{repo}.")

        if new_etag_from_response and new_etag_from_response != current_issue_etag:
            logger.info(f"Issues (Open): ETag changed on 200 OK for {owner}/{repo}. Updating.")
            db_updates["issue_etag"] = new_etag_from_response

    if "issue_etag" in db_updates:
        next_issue_etag = db_updates["issue_etag"]
        
    if db_updates:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, **db_updates)
            logger.debug(f"Issues (Open): Updated DB for {owner}/{repo} with: {db_updates}")
        except Exception as db_e:
            logger.error(f"Issues (Open): Failed to update DB for {repo_db_id}: {db_e}. Returning current state.", exc_info=True)
            return current_last_issue_number, current_issue_etag 
        
    return next_last_issue_number, next_issue_etag


async def _handle_newly_closed_issue_checks(
    api_client: GitHubAPIClient,
    owner: str,
    repo: str,
    repo_db_id: int,
    current_last_closed_ts: Optional[str],
    current_closed_etag: Optional[str],
    async_session_maker: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    strings: Dict[str, Any],
    chat_id: int,
    bot
) -> Tuple[Optional[str], Optional[str]]:
    """
    Handles checking for newly closed issues.
    Fetches issues sorted by update date.
    """
    api_response = await api_client.fetch_issues(
        owner, repo, etag=current_closed_etag, per_page=30,
        sort='updated', direction='desc', state='closed',
        since=current_last_closed_ts
    )

    next_last_closed_ts = current_last_closed_ts
    next_closed_etag = current_closed_etag

    if api_response.status_code == 304: # Not Modified
        etag_from_304 = api_response.etag
        logger.debug(f"Issues (Closed): No new data for {owner}/{repo} (304 Not Modified). ETag: {etag_from_304[:7] if etag_from_304 else 'None'}")
        if etag_from_304 and etag_from_304 != current_closed_etag:
            logger.info(f"Issues (Closed): ETag changed on 304 for {owner}/{repo}. Updating.")
            next_closed_etag = etag_from_304
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, closed_issue_etag=next_closed_etag)
        return next_last_closed_ts, next_closed_etag

    # Successful response (200 OK)
    github_closed_issues_data = api_response.data
    new_etag_from_response = api_response.etag
    db_updates = {}

    if not isinstance(github_closed_issues_data, list):
        logger.warning(f"Issues (Closed): Invalid (non-list) issue data from GitHub API for {owner}/{repo}. Skipping.")
        if new_etag_from_response and new_etag_from_response != current_closed_etag:
            logger.info(f"Issues (Closed): ETag changed on (faulty non-list) 200 OK for {owner}/{repo}. Updating.")
            db_updates["closed_issue_etag"] = new_etag_from_response
    else: 
        newly_found_closed_issues, latest_overall_update_ts_from_api, is_initial_poll = \
            identify_newly_closed_issues(github_closed_issues_data, current_last_closed_ts)

        if is_initial_poll:
            logger.info(f"Issues (Closed): Initial poll for {owner}/{repo}. Baseline updated_at: {latest_overall_update_ts_from_api or 'None'}. No notifications sent.")
        
        elif newly_found_closed_issues:
            logger.info(f"Issues (Closed): Found {len(newly_found_closed_issues)} newly closed issue(s) for {owner}/{repo}.")
            for closed_issue in reversed(newly_found_closed_issues):
                try:
                    message_text = format_closed_issue_message(closed_issue, owner, repo, strings)
                    await bot.send_message(chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                    logger.info(f"Issues (Closed): Sent notification for closed issue #{closed_issue['number']} to {chat_id}.")
                except RPCError as rpc_e:
                    logger.error(f"Issues (Closed): Failed to send Telegram message for closed issue #{closed_issue.get('number')} of {owner}/{repo}: {rpc_e}.")
                except Exception as send_e:
                    logger.error(f"Issues (Closed): Error preparing/sending notification for closed issue #{closed_issue.get('number')} of {owner}/{repo}: {send_e}", exc_info=True)
        else:
            logger.debug(f"Issues (Closed): No issues found strictly newer than {current_last_closed_ts or 'beginning'}.")

        if latest_overall_update_ts_from_api and latest_overall_update_ts_from_api != current_last_closed_ts:
            next_last_closed_ts = latest_overall_update_ts_from_api
            db_updates["last_closed_issue_update_ts"] = next_last_closed_ts
        
        if new_etag_from_response and new_etag_from_response != current_closed_etag:
            logger.info(f"Issues (Closed): ETag changed on 200 OK for {owner}/{repo}. Updating.")
            db_updates["closed_issue_etag"] = new_etag_from_response

    if "closed_issue_etag" in db_updates:
        next_closed_etag = db_updates["closed_issue_etag"]

    if db_updates:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, **db_updates)
            logger.debug(f"Issues (Closed): Updated DB for {owner}/{repo} with: {db_updates}")
        except Exception as db_e:
            logger.error(f"Issues (Closed): Failed to update DB for {repo_db_id}: {db_e}. Returning current state.", exc_info=True)
            return current_last_closed_ts, current_closed_etag 
    
    return next_last_closed_ts, next_closed_etag


async def handle_issue_monitoring_cycle(
    api_client: GitHubAPIClient,
    owner: str,
    repo: str,
    repo_url: str,
    repo_db_id: int,
    current_last_issue_number: Optional[int],
    current_issue_etag: Optional[str],
    current_last_closed_ts: Optional[str],
    current_closed_etag: Optional[str],
    async_session_maker: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    strings: Dict[str, Any],
    chat_id: int,
    bot,
    max_issues_to_list_in_notification: int
) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """
    Handles one full cycle of checking for new open issues and newly closed issues.
    Returns:
        Tuple of (next_last_issue_number, next_issue_etag, 
                  next_last_closed_ts, next_closed_etag)
    """
    logger.debug(f"Issues Cycle: Checking for new open issues for {owner}/{repo}...")
    next_last_issue_number, next_issue_etag = await _handle_new_open_issue_checks(
        api_client, owner, repo, repo_db_id,
        current_last_issue_number, current_issue_etag,
        async_session_maker, logger, strings, chat_id, bot, max_issues_to_list_in_notification
    )

    logger.debug(f"Issues Cycle: Checking for newly closed issues for {owner}/{repo}...")
    next_last_closed_ts, next_closed_etag = await _handle_newly_closed_issue_checks(
        api_client, owner, repo, repo_db_id,
        current_last_closed_ts, current_closed_etag,
        async_session_maker, logger, strings, chat_id, bot
    )

    return next_last_issue_number, next_issue_etag, next_last_closed_ts, next_closed_etag
