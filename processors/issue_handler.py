import logging
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from ..api.github_api import GitHubAPIClient, APIError
from .issue_processing import identify_new_issues, format_single_issue_message, format_multiple_issues_message
from .. import db_ops

async def handle_issue_checks(
    api_client: GitHubAPIClient,
    owner: str,
    repo: str,
    repo_url: str,
    repo_db_id: int,
    current_last_issue_number: Optional[int],
    current_issue_etag: Optional[str],
    async_session_maker: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    strings: Dict[str, Any],
    chat_id: int,
    bot
) -> Tuple[Optional[int], Optional[str]]:
    """
    Handles one cycle of checking for new issues.
    Fetches issues, identifies new ones, sends notifications, and updates DB.

    Returns:
        The new last_issue_number and new_issue_etag to be used for the next cycle.
    Raises:
        APIError exceptions if GitHub API calls fail critically.
    """
    api_response = await api_client.fetch_issues(
        owner, repo, etag=current_issue_etag, per_page=30,
        sort='created', direction='desc', state='open'
    )

    next_last_issue_number = current_last_issue_number
    next_issue_etag = current_issue_etag

    if api_response.status_code == 304: # Not Modified
        etag_from_304 = api_response.etag
        logger.debug(f"Issues: No new data for {owner}/{repo} (304 Not Modified). ETag: {etag_from_304[:7] if etag_from_304 else 'None'}")
        if etag_from_304 and etag_from_304 != current_issue_etag:
            old_etag_display = current_issue_etag[:7] if current_issue_etag else "None"
            new_etag_display = etag_from_304[:7]
            logger.info(f"Issues: ETag changed on 304 for {owner}/{repo}. Old: {old_etag_display}, New: {new_etag_display}. Updating.")
            next_issue_etag = etag_from_304
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, issue_etag=next_issue_etag)
        return next_last_issue_number, next_issue_etag

    # Successful response (200 OK)
    github_issues_data = api_response.data
    new_etag_from_response = api_response.etag

    if not isinstance(github_issues_data, list):
        logger.warning(f"Issues: Invalid (non-list) issue data from GitHub API for {owner}/{repo} despite 200 OK. Skipping this check.")
        if new_etag_from_response and new_etag_from_response != current_issue_etag:
            old_etag_display = current_issue_etag[:7] if current_issue_etag else "None"
            new_etag_display = new_etag_from_response[:7]
            logger.info(f"Issues: ETag changed on (faulty non-list) 200 OK for {owner}/{repo}. Old: {old_etag_display}, New: {new_etag_display}. Updating.")
            next_issue_etag = new_etag_from_response
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, issue_etag=next_issue_etag)
        return current_last_issue_number, next_issue_etag

    if not github_issues_data:
        logger.debug(f"Issues: Received empty list for {owner}/{repo}, no new issues based on current criteria.")
        if new_etag_from_response and new_etag_from_response != current_issue_etag:
            old_etag_display = current_issue_etag[:7] if current_issue_etag else "None"
            new_etag_display = new_etag_from_response[:7]
            logger.info(f"Issues: ETag changed on 200 OK (empty list) for {owner}/{repo}. Old: {old_etag_display}, New: {new_etag_display}. Updating.")
            next_issue_etag = new_etag_from_response
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, issue_etag=next_issue_etag)
        return next_last_issue_number, next_issue_etag


    newly_found_issues, latest_issue_number_on_github, is_initial = \
        identify_new_issues(github_issues_data, current_last_issue_number)

    db_updates = {}

    if is_initial:
        logger.info(f"Issues: Initial run for {owner}/{repo}.")
        next_last_issue_number = latest_issue_number_on_github
        db_updates["last_known_issue_number"] = next_last_issue_number
    
    elif newly_found_issues:
        logger.info(f"Issues: Found {len(newly_found_issues)} new issue(s) for {owner}/{repo}. Old Number: {current_last_issue_number}, Newest Number from API: {latest_issue_number_on_github}.")
        
        # The first issue in newly_found_issues is the newest one.
        new_issue_number_to_store_in_db = newly_found_issues[0]['number']
        next_last_issue_number = new_issue_number_to_store_in_db
        db_updates["last_known_issue_number"] = next_last_issue_number
        
        # Prepare and Send Notification
        try:
            if len(newly_found_issues) == 1:
                message_text = format_single_issue_message(newly_found_issues[0], owner, repo, strings)
            else:
                message_text = format_multiple_issues_message(
                    newly_found_issues, owner, repo, strings
                )
            
            await bot.send_message(chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
            logger.info(f"Issues: Sent notification for {len(newly_found_issues)} issue(s) to chat {chat_id} for {owner}/{repo}.")
        except RPCError as rpc_e:
            logger.error(f"Issues: Failed to send Telegram message for {owner}/{repo} to {chat_id}: {rpc_e}. Monitoring continues.")
        except Exception as send_e:
            logger.error(f"Issues: Unexpected error preparing/sending issue notification for {owner}/{repo}: {send_e}", exc_info=True)

    elif latest_issue_number_on_github is not None and latest_issue_number_on_github != current_last_issue_number : 
        if latest_issue_number_on_github > (current_last_issue_number or 0):
            logger.info(f"Issues: No new issues reported, but latest API issue number {latest_issue_number_on_github} "
                    f"is greater than known {current_last_issue_number}. Updating known number.")
            next_last_issue_number = latest_issue_number_on_github
            db_updates["last_known_issue_number"] = next_last_issue_number
        else:
            logger.debug(f"Issues: Latest API issue {latest_issue_number_on_github} is not newer than known {current_last_issue_number}. No update needed.")
    else:
        logger.debug(f"Issues: No new issues for {owner}/{repo} (Number match or latest not newer: {current_last_issue_number}).")

    if new_etag_from_response and new_etag_from_response != current_issue_etag:
        old_etag_display = current_issue_etag[:7] if current_issue_etag else "None"
        new_etag_display = new_etag_from_response[:7]
        logger.info(f"Issues: ETag changed on 200 OK for {owner}/{repo}. Old: {old_etag_display}, New: {new_etag_display}. Updating.")
        next_issue_etag = new_etag_from_response
        db_updates["issue_etag"] = next_issue_etag
    
    if db_updates:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, **db_updates)
            logger.debug(f"Issues: Updated DB for {owner}/{repo} with: {db_updates}")
        except Exception as db_e:
            logger.error(f"Issues: Failed to update DB state for repo ID {repo_db_id}: {db_e}. State might be stale for next run.", exc_info=True)
            return current_last_issue_number, current_issue_etag

    return next_last_issue_number, next_issue_etag
