import asyncio
import logging
from .utils import parse_github_url
from . import db_ops
from .github_api import GitHubAPIClient, APIError, NotFoundError, UnauthorizedError, ForbiddenError, ClientRequestError, InvalidResponseError
from .commit_processing import identify_new_commits, format_single_commit_message, format_multiple_commits_message

from pyrogram.errors import RPCError
from pyrogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from html import escape
import datetime
import aiohttp

MAX_COMMITS_TO_LIST_IN_NOTIFICATION = 4

async def monitor_repo(
    bot,
    chat_id: int,
    repo_db_id: int,
    repo_url: str,
    check_interval: int,
    max_retries: int,
    github_token: str | None,
    strings: dict,
    initial_last_sha: str | None,
    initial_etag: str | None,
    async_session_maker: async_sessionmaker[AsyncSession]
):
    """
    Monitors a single GitHub repository for new commits.
    Returns True if monitoring should stop permanently, False otherwise.
    """
    logger = logging.getLogger(f"gitMonitor[{chat_id}][{repo_db_id}]")
    owner, repo = parse_github_url(repo_url)
    if not owner or not repo:
        logger.error(f"Invalid repo URL: {repo_url}. Stopping.")
        try:
            await bot.send_message(chat_id, f"Internal error: Invalid repo URL {escape(repo_url)} passed to monitor. Stopping.")
        except Exception:
            pass
        return True

    api_client = GitHubAPIClient(token=github_token, loop=asyncio.get_event_loop())
    current_last_sha = initial_last_sha
    current_etag = initial_etag
    retries = 0

    logger.info(f"Starting monitor for {owner}/{repo} (ID: {repo_db_id}). Interval: {check_interval}s. Initial SHA: {current_last_sha[:7] if current_last_sha else 'None'}")

    try:
        while True:
            try:
                api_response = await api_client.fetch_commits(owner, repo, etag=current_etag, per_page=30)

                if api_response.status_code == 304: # Not Modified
                    logger.debug(f"No new commits for {owner}/{repo} (304 Not Modified). ETag: {api_response.etag}")
                    if api_response.etag and api_response.etag != current_etag:
                        logger.info(f"ETag changed on 304 for {owner}/{repo}. Old: {current_etag}, New: {api_response.etag}. Updating.")
                        current_etag = api_response.etag
                        async with async_session_maker() as session:
                            async with session.begin():
                                await db_ops.update_repo_fields(session, repo_db_id, etag=current_etag)
                    retries = 0
                    await asyncio.sleep(check_interval)
                    continue

                # Successful response (200 OK)
                github_commits_data = api_response.data
                new_etag_from_response = api_response.etag

                if not github_commits_data or not isinstance(github_commits_data, list) or not github_commits_data[0].get("sha"):
                    logger.warning(f"Invalid or empty commit data from GitHub API for {owner}/{repo} despite 200 OK. Retrying.")
                    raise ClientRequestError(200, "Received empty or malformed commit list on 200 OK")


                newly_found_commits, latest_sha_on_github, is_initial, force_pushed_or_many = \
                    identify_new_commits(github_commits_data, current_last_sha)

                if is_initial:
                    logger.info(f"Initial run for {owner}/{repo}. Setting last commit to {latest_sha_on_github[:7]}.")
                    current_last_sha = latest_sha_on_github
                    current_etag = new_etag_from_response
                    async with async_session_maker() as session:
                        async with session.begin():
                            await db_ops.update_repo_fields(
                                session, repo_db_id,
                                last_commit_sha=current_last_sha,
                                etag=current_etag
                            )
                    logger.debug(f"DB initialized for {owner}/{repo}. SHA: {current_last_sha[:7]}, ETag: {current_etag}")
                
                elif newly_found_commits:
                    logger.info(f"Found {len(newly_found_commits)} new commit(s) for {owner}/{repo}. Old SHA: {current_last_sha[:7] if current_last_sha else 'None'}, Newest SHA from API: {latest_sha_on_github[:7]}.")
                    if force_pushed_or_many:
                         logger.warning(f"Previously known SHA {current_last_sha[:7] if current_last_sha else 'None'} not found in recent commits for {owner}/{repo}. "
                                        f"Possible force push or >30 new commits. Reporting on {len(newly_found_commits)} fetched.")

                    new_sha_to_store_in_db = newly_found_commits[0]['sha']

                    previous_sha_for_notification = current_last_sha
                    current_last_sha = new_sha_to_store_in_db
                    current_etag = new_etag_from_response
                    try:
                        async with async_session_maker() as session:
                            async with session.begin():
                                await db_ops.update_repo_fields(
                                    session, repo_db_id,
                                    last_commit_sha=current_last_sha,
                                    etag=current_etag
                                )
                        logger.debug(f"Updated DB for {owner}/{repo}. New SHA: {current_last_sha[:7]}, ETag: {current_etag}")
                    except Exception as db_e:
                        logger.error(f"Failed to update DB state for repo ID {repo_db_id} after new commits: {db_e}", exc_info=True)
                        current_last_sha = previous_sha_for_notification 
                        await asyncio.sleep(check_interval)
                        continue

                    # Prepare and Send Notification
                    try:
                        if len(newly_found_commits) == 1:
                            message_text = format_single_commit_message(newly_found_commits[0], owner, repo, strings)
                        else:
                            message_text = format_multiple_commits_message(
                                newly_found_commits, owner, repo, strings,
                                previous_known_sha=previous_sha_for_notification,
                                max_commits_to_list=MAX_COMMITS_TO_LIST_IN_NOTIFICATION
                            )
                        
                        await bot.send_message(chat_id, message_text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                        logger.info(f"Sent notification for {len(newly_found_commits)} commit(s) to chat {chat_id} for {owner}/{repo}.")
                    except RPCError as rpc_e:
                        logger.error(f"Failed to send Telegram message for {owner}/{repo} to {chat_id}: {rpc_e}. Monitoring continues.")
                    except Exception as send_e:
                        logger.error(f"Unexpected error preparing/sending notification for {owner}/{repo}: {send_e}", exc_info=True)

                elif latest_sha_on_github != current_last_sha:
                    logger.warning(f"SHA mismatch for {owner}/{repo}. DB SHA: {current_last_sha[:7] if current_last_sha else 'None'}, Fetched SHA: {latest_sha_on_github[:7]}. "
                                   f"No new commits in between found (force_pushed_or_many={force_pushed_or_many}). Updating to latest SHA.")
                    current_last_sha = latest_sha_on_github
                    current_etag = new_etag_from_response
                    async with async_session_maker() as session:
                        async with session.begin():
                            await db_ops.update_repo_fields(
                                session, repo_db_id,
                                last_commit_sha=current_last_sha,
                                etag=current_etag
                            )
                
                else:
                    logger.debug(f"No new commits for {owner}/{repo} (SHA match: {current_last_sha[:7]}).")
                    if new_etag_from_response and new_etag_from_response != current_etag:
                        logger.info(f"ETag changed for {owner}/{repo} despite SHA match. Old: {current_etag}, New: {new_etag_from_response}. Updating.")
                        current_etag = new_etag_from_response
                        async with async_session_maker() as session:
                            async with session.begin():
                                await db_ops.update_repo_fields(session, repo_db_id, etag=current_etag)
                
                retries = 0

            except NotFoundError as e:
                logger.error(f"Repository {owner}/{repo} not found (404): {e.message}. Stopping monitor.")
                await bot.send_message(chat_id, strings["monitor"]["repo_not_found"].format(repo_url=escape(repo_url)))
                return True
            except UnauthorizedError as e:
                logger.error(f"Unauthorized (401) for {owner}/{repo}: {e.message}. Check token. Stopping monitor.")
                await bot.send_message(chat_id, strings["monitor"]["auth_error"].format(repo_url=escape(repo_url)))
                return True
            except ForbiddenError as e:
                rate_limit_reset = e.headers.get('X-RateLimit-Reset', '')
                reset_time_str = ""
                if rate_limit_reset:
                    try:
                        reset_dt = datetime.datetime.fromtimestamp(int(rate_limit_reset), tz=datetime.timezone.utc)
                        reset_time_str = f" (resets at {reset_dt.strftime('%Y-%m-%d %H:%M:%S %Z')})"
                    except ValueError:
                        pass
                logger.warning(f"Forbidden/Rate Limit (403) for {owner}/{repo}{reset_time_str}: {e.message}.")
                retries += 1
                if retries >= max_retries:
                    logger.error(f"Rate limit / Forbidden error persisted after {max_retries} retries for {owner}/{repo}. Stopping monitor.")
                    await bot.send_message(chat_id, strings["monitor"]["rate_limit_error"].format(repo_url=escape(repo_url)))
                    return True
                
                # Check for explicit retry-after header from GitHub
                retry_after_seconds = e.headers.get('Retry-After')
                wait_time = check_interval * (2 ** retries)
                if retry_after_seconds:
                    try:
                        wait_time = max(wait_time, int(retry_after_seconds) + 5)
                        logger.info(f"Using Retry-After header: {retry_after_seconds}s.")
                    except ValueError:
                        pass
                
                logger.info(f"Waiting {wait_time}s before next check for {owner}/{repo}")
                await asyncio.sleep(wait_time)
                continue
            except (ClientRequestError, InvalidResponseError, aiohttp.ClientResponseError) as e:
                logger.warning(f"API request error for {owner}/{repo}: {type(e).__name__} - {e.message if hasattr(e, 'message') else str(e)}. Retry {retries + 1}/{max_retries}.")
                retries += 1
                if retries >= max_retries:
                    logger.error(f"Max retries ({max_retries}) exceeded for {owner}/{repo} due to {type(e).__name__}. Stopping monitor.")
                    error_key = "invalid_data_error" if isinstance(e, InvalidResponseError) else "network_error"
                    try:
                        await bot.send_message(chat_id, strings["monitor"][error_key].format(repo_url=escape(repo_url)))
                    except Exception:
                        logger.warning(f"Failed to send {error_key} notification to chat {chat_id} for {owner}/{repo}")
                    return True
                wait_time = check_interval * (2 ** (retries -1))
                logger.info(f"Waiting {wait_time}s before next check for {owner}/{repo}")
                await asyncio.sleep(wait_time)
                continue
            except APIError as e:
                logger.error(f"Unhandled APIError for {owner}/{repo}: {e}. Stopping monitor.", exc_info=True)
                await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=escape(repo_url)))
                return True
            except Exception as e:
                logger.error(f"Unexpected error in monitor loop for {owner}/{repo}: {e}", exc_info=True)
                try:
                    await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=escape(repo_url)))
                except Exception:
                    pass
                return True

            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) cancelled.")
        return False
    except Exception as outer_e:
        logger.critical(f"Critical unexpected error outside main loop for {owner}/{repo} (ID: {repo_db_id}): {outer_e}", exc_info=True)
        try:
            await bot.send_message(chat_id, f"A critical internal error occurred monitoring {escape(repo_url)}. The monitor has stopped.")
        except Exception:
            pass
        return True
    finally:
        await api_client.close()
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) finished processing.")
