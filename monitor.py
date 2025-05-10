import asyncio
import logging
from html import escape
import datetime
import aiohttp

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .utils import parse_github_url
from .api.github_api import (
    GitHubAPIClient, APIError, NotFoundError,
    UnauthorizedError, ForbiddenError, ClientRequestError, InvalidResponseError
)
from .db import MonitoredRepo
from .processors.commit_handler import handle_commit_checks
from .processors.issue_handler import handle_issue_monitoring_cycle


async def monitor_repo(
    bot,
    chat_id: int,
    repo_entry: MonitoredRepo,
    check_interval: int,
    max_retries: int,
    github_token: str | None,
    strings: dict,
    async_session_maker: async_sessionmaker[AsyncSession],
    max_commits_to_list_in_notification: int,
    max_issues_to_list_in_notification: int
):
    """
    Monitors a single GitHub repository.
    Returns True if monitoring should stop permanently, False otherwise.
    """
    repo_db_id = repo_entry.id
    repo_url = repo_entry.repo_url
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
    
    # Initialize current state from repo_entry
    current_last_sha = repo_entry.last_commit_sha
    current_commit_etag = repo_entry.commit_etag
    current_last_issue_number = repo_entry.last_known_issue_number
    current_issue_etag = repo_entry.issue_etag
    current_last_closed_ts = repo_entry.last_closed_issue_update_ts
    current_closed_issue_etag = repo_entry.closed_issue_etag

    retries = 0

    logger.info(f"Starting monitor for {owner}/{repo} (ID: {repo_db_id}). Interval: {check_interval}s. "
                f"Initial SHA: {current_last_sha[:7] if current_last_sha else 'None'}, "
                f"Initial Issue No: {current_last_issue_number if current_last_issue_number else 'None'}, "
                f"Initial Closed TS: {current_last_closed_ts if current_last_closed_ts else 'None'}. "
                f"Commits: {'Enabled' if repo_entry.monitor_commits else 'Disabled'}, "
                f"Issues: {'Enabled' if repo_entry.monitor_issues else 'Disabled'}")

    try:
        while True:
            monitor_commits_enabled = repo_entry.monitor_commits
            monitor_issues_enabled = repo_entry.monitor_issues

            try:
                if monitor_commits_enabled:
                    logger.debug(f"Checking commits for {owner}/{repo}...")
                    next_sha, next_commit_etag = await handle_commit_checks(
                        api_client=api_client, owner=owner, repo=repo, repo_url=repo_url, repo_db_id=repo_db_id,
                        current_last_sha=current_last_sha, current_commit_etag=current_commit_etag,
                        async_session_maker=async_session_maker, logger=logger, strings=strings,
                        chat_id=chat_id, bot=bot,
                        max_commits_to_list_in_notification=max_commits_to_list_in_notification
                    )
                    current_last_sha = next_sha
                    current_commit_etag = next_commit_etag
                elif not monitor_commits_enabled and current_commit_etag:
                    logger.debug(f"Commit monitoring disabled for {owner}/{repo}, clearing etag if set.")
                    async with async_session_maker() as session:
                        async with session.begin():
                            from . import db_ops
                            await db_ops.update_repo_fields(session, repo_db_id, commit_etag=None)
                    current_commit_etag = None

                if monitor_issues_enabled:
                    logger.debug(f"Checking issues (open & closed) for {owner}/{repo}...")
                    next_issue_num, next_issue_etag, next_closed_ts, next_closed_etag = \
                        await handle_issue_monitoring_cycle(
                            api_client=api_client, owner=owner, repo=repo, repo_url=repo_url, repo_db_id=repo_db_id,
                            current_last_issue_number=current_last_issue_number, current_issue_etag=current_issue_etag,
                            current_last_closed_ts=current_last_closed_ts, current_closed_etag=current_closed_issue_etag,
                            async_session_maker=async_session_maker, logger=logger, strings=strings,
                            chat_id=chat_id, bot=bot,
                            max_issues_to_list_in_notification=max_issues_to_list_in_notification
                        )
                    current_last_issue_number = next_issue_num
                    current_issue_etag = next_issue_etag
                    current_last_closed_ts = next_closed_ts
                    current_closed_issue_etag = next_closed_etag
                elif not monitor_issues_enabled:
                    db_updates_for_disabled_issues = {}
                    if current_issue_etag:
                        db_updates_for_disabled_issues["issue_etag"] = None
                        current_issue_etag = None
                    if current_closed_issue_etag:
                        db_updates_for_disabled_issues["closed_issue_etag"] = None
                        current_closed_issue_etag = None
                    if db_updates_for_disabled_issues:
                        logger.debug(f"Issue monitoring disabled for {owner}/{repo}, clearing etags if set.")
                        async with async_session_maker() as session:
                            async with session.begin():
                                from . import db_ops
                                await db_ops.update_repo_fields(session, repo_db_id, **db_updates_for_disabled_issues)
                
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
                    except ValueError: pass
                
                logger.warning(f"Forbidden/Rate Limit (403) for {owner}/{repo}{reset_time_str}: {e.message}.")
                retries += 1
                if retries >= max_retries:
                    logger.error(f"Rate limit / Forbidden error persisted after {max_retries} retries for {owner}/{repo}. Stopping monitor.")
                    await bot.send_message(chat_id, strings["monitor"]["rate_limit_error"].format(repo_url=escape(repo_url)))
                    return True
                
                # Check for explicit retry-after header from GitHub
                retry_after_seconds_str = e.headers.get('Retry-After')
                wait_time = check_interval * (2 ** retries)
                if retry_after_seconds_str:
                    try:
                        retry_after_seconds = int(retry_after_seconds_str)
                        wait_time = max(wait_time, retry_after_seconds + 5) 
                        logger.info(f"Using Retry-After header: {retry_after_seconds}s. Effective wait: {wait_time}s")
                    except ValueError: pass

                logger.info(f"Waiting {wait_time}s before next check for {owner}/{repo} (Retry {retries}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            except (ClientRequestError, InvalidResponseError, aiohttp.ClientResponseError, aiohttp.ServerTimeoutError) as e:
                logger.warning(f"API request or response error for {owner}/{repo}: {type(e).__name__} - {str(e)}. Retry {retries + 1}/{max_retries}.")
                retries += 1
                if retries >= max_retries:
                    logger.error(f"Max retries ({max_retries}) exceeded for {owner}/{repo} due to {type(e).__name__}. Stopping monitor.")
                    error_key = "invalid_data_error" if isinstance(e, InvalidResponseError) else "network_error"
                    try:
                        await bot.send_message(chat_id, strings["monitor"][error_key].format(repo_url=escape(repo_url)))
                    except Exception as send_err:
                        logger.warning(f"Failed to send {error_key} notification to chat {chat_id} for {owner}/{repo}: {send_err}")
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
                logger.error(f"Unexpected error in monitor orchestrator for {owner}/{repo}: {e}", exc_info=True)
                try:
                    await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=escape(repo_url)))
                except Exception: pass
                return True

            logger.debug(f"All checks complete for {owner}/{repo}. Sleeping for {check_interval}s.")
            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) cancelled.")
        return False
    except Exception as outer_e:
        logger.critical(f"Critical unexpected error OUTSIDE main loop for {owner}/{repo} (ID: {repo_db_id}): {outer_e}", exc_info=True)
        try:
            await bot.send_message(chat_id, f"A critical internal error occurred monitoring {escape(repo_url)}. The monitor has stopped.")
        except Exception: pass
        return True
    finally:
        await api_client.close()
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) finished processing and client closed.")
