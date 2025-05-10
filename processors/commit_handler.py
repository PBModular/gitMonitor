import logging
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from ..api.github_api import GitHubAPIClient, APIError
from .commit_processing import identify_new_commits, format_single_commit_message, format_multiple_commits_message
from .. import db_ops

async def handle_commit_checks(
    api_client: GitHubAPIClient,
    owner: str,
    repo: str,
    repo_url: str,
    repo_db_id: int,
    current_last_sha: Optional[str],
    current_commit_etag: Optional[str],
    async_session_maker: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
    strings: Dict[str, Any],
    chat_id: int,
    bot
) -> Tuple[Optional[str], Optional[str]]:
    """
    Handles one cycle of checking for new commits.
    Fetches commits, identifies new ones, sends notifications, and updates DB.

    Returns:
        The new last_sha and new_commit_etag to be used for the next cycle.

    Raises:
        APIError exceptions if GitHub API calls fail critically (e.g. 404, 401, 403 after retries)
        which should be caught by the main monitor loop.
    """
    
    api_response = await api_client.fetch_commits(owner, repo, etag=current_commit_etag, per_page=30)

    next_last_sha = current_last_sha
    next_commit_etag = current_commit_etag

    if api_response.status_code == 304: # Not Modified
        logger.debug(f"Commits: No new data for {owner}/{repo} (304 Not Modified). ETag: {api_response.etag}")
        if api_response.etag and api_response.etag != current_commit_etag:
            logger.info(f"Commits: ETag changed on 304 for {owner}/{repo}. Old: {current_commit_etag[:7]}, New: {api_response.etag[:7]}. Updating.")
            next_commit_etag = api_response.etag
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, commit_etag=next_commit_etag)
        return next_last_sha, next_commit_etag

    # Successful response (200 OK)
    github_commits_data = api_response.data
    new_etag_from_response = api_response.etag

    if not github_commits_data or not isinstance(github_commits_data, list) or not github_commits_data[0].get("sha"):
        logger.warning(f"Commits: Invalid or empty commit data from GitHub API for {owner}/{repo} despite 200 OK. Skipping this check.")
        return current_last_sha, current_commit_etag # Or update etag if it changed on this faulty 200

    newly_found_commits, latest_sha_on_github, is_initial, force_pushed_or_many = \
        identify_new_commits(github_commits_data, current_last_sha)

    db_updates = {}

    if is_initial:
        logger.info(f"Commits: Initial run for {owner}/{repo}. Setting last commit to {latest_sha_on_github[:7] if latest_sha_on_github else 'None'}.")
        next_last_sha = latest_sha_on_github
        db_updates["last_commit_sha"] = next_last_sha
    
    elif newly_found_commits:
        logger.info(f"Commits: Found {len(newly_found_commits)} new commit(s) for {owner}/{repo}. Old SHA: {current_last_sha[:7] if current_last_sha else 'None'}, Newest SHA from API: {latest_sha_on_github[:7] if latest_sha_on_github else 'None'}.")
        if force_pushed_or_many:
                logger.warning(f"Commits: Previously known SHA {current_last_sha[:7] if current_last_sha else 'None'} not found in recent commits for {owner}/{repo}. "
                                f"Possible force push or >30 new commits. Reporting on {len(newly_found_commits)} fetched.")

        new_sha_to_store_in_db = newly_found_commits[0]['sha']
        next_last_sha = new_sha_to_store_in_db
        db_updates["last_commit_sha"] = next_last_sha
        
        # Prepare and Send Notification
        try:
            if len(newly_found_commits) == 1:
                message_text = format_single_commit_message(newly_found_commits[0], owner, repo, strings)
            else:
                message_text = format_multiple_commits_message(
                    newly_found_commits, owner, repo, strings,
                    previous_known_sha=current_last_sha
                )
            
            await bot.send_message(chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
            logger.info(f"Commits: Sent notification for {len(newly_found_commits)} commit(s) to chat {chat_id} for {owner}/{repo}.")
        except RPCError as rpc_e:
            logger.error(f"Commits: Failed to send Telegram message for {owner}/{repo} to {chat_id}: {rpc_e}. Monitoring continues.")
        except Exception as send_e:
            logger.error(f"Commits: Unexpected error preparing/sending notification for {owner}/{repo}: {send_e}", exc_info=True)

    elif latest_sha_on_github != current_last_sha:
        logger.warning(f"Commits: SHA mismatch for {owner}/{repo}. DB SHA: {current_last_sha[:7] if current_last_sha else 'None'}, Fetched SHA: {latest_sha_on_github[:7] if latest_sha_on_github else 'None'}. "
                        f"No new commits in between found (force_pushed_or_many={force_pushed_or_many}). Updating to latest SHA from API.")
        next_last_sha = latest_sha_on_github
        db_updates["last_commit_sha"] = next_last_sha
    
    else:
        logger.debug(f"Commits: No new commits for {owner}/{repo} (SHA match: {current_last_sha[:7] if current_last_sha else 'None'}).")

    # Handle ETag update for 200 OK responses
    if new_etag_from_response and new_etag_from_response != current_commit_etag:
        logger.info(f"Commits: ETag changed on 200 OK for {owner}/{repo}. Old: {current_commit_etag[:7]}, New: {new_etag_from_response[:7]}. Updating.")
        next_commit_etag = new_etag_from_response
        db_updates["commit_etag"] = next_commit_etag
    
    if db_updates:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    await db_ops.update_repo_fields(session, repo_db_id, **db_updates)
            logger.debug(f"Commits: Updated DB for {owner}/{repo} with: {db_updates}")
        except Exception as db_e:
            logger.error(f"Commits: Failed to update DB state for repo ID {repo_db_id}: {db_e}. State might be stale for next run.", exc_info=True)
            return current_last_sha, current_commit_etag

    return next_last_sha, next_commit_etag
