import asyncio
import aiohttp
import logging
from .utils import parse_github_url, get_merge_info
from .db import MonitoredRepo
from pyrogram.errors import RPCError
from pyrogram.enums import ParseMode
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from html import escape
import datetime

MAX_COMMITS_TO_LIST = 4

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
    Monitors a single GitHub repository for new commits. Handles multiple new commits.

    Returns True if monitoring should stop permanently (e.g., 404, 401), False otherwise.
    """
    logger = logging.getLogger(f"gitMonitor[{chat_id}][{repo_db_id}]")
    owner, repo = parse_github_url(repo_url)
    if not owner or not repo:
        logger.error(f"Invalid repo URL received in monitor: {repo_url}. Stopping.")
        try:
            await bot.send_message(chat_id, f"Internal error: Invalid repo URL {escape(repo_url)} passed to monitor. Stopping.")
        except Exception:
            pass
        return True

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    last_commit_sha = initial_last_sha
    etag = initial_etag
    retries = 0
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    logger.info(f"Starting monitor for {owner}/{repo} (ID: {repo_db_id}). Interval: {check_interval}s. Initial SHA: {last_commit_sha[:7] if last_commit_sha else 'None'}")

    try:
        while True:
            current_headers = headers.copy()
            if etag:
                current_headers["If-None-Match"] = etag

            new_commits_data = []

            try:
                async with aiohttp.ClientSession(headers=current_headers) as http_session:
                    async with http_session.get(api_url, params={"per_page": 30}, timeout=30) as response:
                        # Handle 304 Not Modified
                        if response.status == 304:
                            logger.debug(f"No new commits for {owner}/{repo} (ETag match).")
                            retries = 0
                            await asyncio.sleep(check_interval)
                            continue

                        # Handle permanent errors (404, 401)
                        if response.status == 404:
                            logger.error(f"Repository {owner}/{repo} not found (404). Stopping monitor.")
                            await bot.send_message(chat_id, strings["monitor"]["repo_not_found"].format(repo_url=escape(repo_url)))
                            return True
                        if response.status == 401:
                            logger.error(f"Unauthorized (401) for {owner}/{repo}. Check token. Stopping monitor.")
                            await bot.send_message(chat_id, strings["monitor"]["auth_error"].format(repo_url=escape(repo_url)))
                            return True

                        # Handle temporary errors (403 Rate Limit/Forbidden)
                        if response.status == 403:
                            rate_limit_reset = response.headers.get('X-RateLimit-Reset', '')
                            try:
                                reset_time_str = f" (resets at {datetime.datetime.fromtimestamp(int(rate_limit_reset)).strftime('%Y-%m-%d %H:%M:%S %Z')})" if rate_limit_reset else ""
                            except ValueError:
                                reset_time_str = ""
                            logger.warning(f"Forbidden/Rate Limit (403) for {owner}/{repo}{reset_time_str}. Retrying after delay.")
                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"Rate limit / Forbidden error persisted after {max_retries} retries for {owner}/{repo}. Stopping monitor.")
                                await bot.send_message(chat_id, strings["monitor"]["rate_limit_error"].format(repo_url=escape(repo_url)))
                                return True
                            wait_time = check_interval * (2 ** retries)
                            logger.info(f"Waiting {wait_time}s before next check for {owner}/{repo}")
                            await asyncio.sleep(wait_time)
                            continue

                        response.raise_for_status()

                        # Process successful response
                        data = await response.json()
                        new_etag = response.headers.get("ETag")

                        if not data or not isinstance(data, list) or not data[0].get("sha"):
                            logger.warning(f"Invalid/empty data received from GitHub API for {owner}/{repo}. Retrying.")
                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"Received invalid data after {max_retries} retries for {owner}/{repo}. Stopping monitor.")
                                await bot.send_message(chat_id, strings["monitor"]["invalid_data_error"].format(repo_url=escape(repo_url)))
                                return True
                            await asyncio.sleep(check_interval * (2**(retries-1)))
                            continue

                        latest_commit_sha_on_github = data[0]["sha"]

                        if last_commit_sha is None:
                            logger.info(f"Initialized monitor for {owner}/{repo}. Last commit: {latest_commit_sha_on_github[:7]}")
                            last_commit_sha = latest_commit_sha_on_github
                            etag = new_etag
                            try:
                                async with async_session_maker() as session:
                                    async with session.begin():
                                        await session.execute(
                                            update(MonitoredRepo)
                                            .where(MonitoredRepo.id == repo_db_id)
                                            .values(last_commit_sha=last_commit_sha, etag=etag)
                                        )
                                    logger.debug(f"DB initialized for {owner}/{repo} (ID: {repo_db_id}). SHA: {last_commit_sha[:7]}, ETag: {etag}")
                            except Exception as db_e:
                                logger.error(f"Failed to update DB state on initialization for repo ID {repo_db_id}: {db_e}", exc_info=True)

                        elif latest_commit_sha_on_github != last_commit_sha:
                            logger.info(f"Change detected for {owner}/{repo}. Known SHA: {last_commit_sha[:7]}, Latest SHA: {latest_commit_sha_on_github[:7]}. Fetching details...")
                            found_last_sha = False
                            for commit_data in data:
                                if commit_data["sha"] == last_commit_sha:
                                    found_last_sha = True
                                    break
                                new_commits_data.append(commit_data)

                            if not found_last_sha:
                                logger.warning(f"Previously known SHA {last_commit_sha[:7]} not found in recent commits for {owner}/{repo}. \
                                               Possible force push or very large number of commits. Reporting only the latest {len(new_commits_data)} fetched.")

                            if new_commits_data:
                                logger.info(f"Found {len(new_commits_data)} new commit(s) for {owner}/{repo}.")
                                new_last_sha = new_commits_data[0]['sha']
                                try:
                                    async with async_session_maker() as session:
                                        async with session.begin():
                                            await session.execute(
                                                update(MonitoredRepo)
                                                .where(MonitoredRepo.id == repo_db_id)
                                                .values(last_commit_sha=new_last_sha, etag=new_etag)
                                            )
                                        last_commit_sha = new_last_sha
                                        etag = new_etag
                                        logger.debug(f"Updated DB for {owner}/{repo} (ID: {repo_db_id}). New SHA: {last_commit_sha[:7]}, ETag: {etag}")
                                except Exception as db_e:
                                    logger.error(f"Failed to update DB state for repo ID {repo_db_id} after finding new commits: {db_e}", exc_info=True)
                                    await asyncio.sleep(check_interval)
                                    continue

                                # Prepare and Send Notification
                                try:
                                    if len(new_commits_data) == 1:
                                        commit = new_commits_data[0]
                                        merge_info = get_merge_info(commit)
                                        merge_indicator = ''
                                        if merge_info:
                                            if merge_info["type"] == "pr":
                                                pr_url = f"https://github.com/{owner}/{repo}/pull/{merge_info['number']}"
                                                merge_indicator = f' [<a href="{pr_url}">PR #{merge_info["number"]} merged</a>]'
                                            else:
                                                merge_indicator = ' [Merge commit]'
                                        
                                        commit_info = commit.get("commit", {})
                                        author_info = commit_info.get("author", {})
                                        author_name = escape(author_info.get("name", "Unknown"))
                                        commit_message = escape(commit_info.get("message", "No message").split('\n')[0])
                                        sha_short = commit['sha'][:7]
                                        commit_url = escape(commit.get("html_url", "#"))

                                        text = strings["monitor"]["new_commit"].format(
                                            owner=escape(owner),
                                            repo=escape(repo),
                                            author=author_name,
                                            message=commit_message,
                                            merge_indicator=merge_indicator,
                                            sha=sha_short,
                                            commit_url=commit_url
                                        )
                                    else:
                                        count = len(new_commits_data)
                                        commit_list_lines = []
                                        for commit in reversed(new_commits_data[:MAX_COMMITS_TO_LIST]):
                                            merge_info = get_merge_info(commit)
                                            merge_indicator = ''
                                            if merge_info:
                                                if merge_info["type"] == "pr":
                                                    pr_url = f"https://github.com/{owner}/{repo}/pull/{merge_info['number']}"
                                                    merge_indicator = f' [<a href="{pr_url}">PR #{merge_info["number"]}</a>]'
                                                else:
                                                    merge_indicator = ' [Merge]'
                                            
                                            commit_info = commit.get("commit", {})
                                            author_info = commit_info.get("author", {})
                                            author_name = escape(author_info.get("name", "Unknown"))
                                            commit_message = escape(commit_info.get("message", "No message").split('\n')[0])
                                            sha_short = commit['sha'][:7]
                                            commit_url = escape(commit.get("html_url", "#"))

                                            commit_list_lines.append(
                                                strings["monitor"]["commit_line"].format(
                                                    url=commit_url,
                                                    sha=sha_short,
                                                    message=commit_message,
                                                    merge_indicator=merge_indicator,
                                                    author=author_name
                                                )
                                            )

                                        latest_commit = new_commits_data[0]
                                        latest_sha_short = latest_commit['sha'][:7]
                                        latest_commit_url = escape(latest_commit.get("html_url", "#"))

                                        more_link = ""
                                        if count > MAX_COMMITS_TO_LIST:
                                            oldest_sha = new_commits_data[-1]['sha'][:7]
                                            newest_sha = new_commits_data[0]['sha']
                                            compare_url = escape(f"https://github.com/{owner}/{repo}/compare/{oldest_sha}...{newest_sha}")
                                            more_link = strings["monitor"]["more"].format(compare_url=compare_url)

                                        text = strings["monitor"]["multiple_new_commits"].format(
                                            count=count,
                                            owner=escape(owner),
                                            repo=escape(repo),
                                            commit_list="\n".join(commit_list_lines),
                                            latest_sha=latest_sha_short,
                                            latest_commit_url=latest_commit_url
                                        ) + more_link

                                    await bot.send_message(chat_id, text, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
                                    logger.info(f"Sent notification for {len(new_commits_data)} commit(s) to chat {chat_id} for {owner}/{repo}.")

                                except RPCError as e:
                                    logger.error(f"Failed to send notification message to chat {chat_id} for {owner}/{repo}: {e}. Monitoring continues, but state is updated.")
                                except Exception as send_e:
                                    logger.error(f"Unexpected error preparing/sending notification for {owner}/{repo}: {send_e}", exc_info=True)

                            else:
                                logger.warning(f"Detected change for {owner}/{repo} but found no specific new commits. Updating ETag only.")
                                etag = new_etag

                        else:
                            logger.debug(f"No new commits for {owner}/{repo} (SHA match: {last_commit_sha[:7]}).")
                            if new_etag and new_etag != etag:
                                logger.debug(f"ETag changed for {owner}/{repo} even though SHA matched. Updating ETag.")
                                etag = new_etag
                                try:
                                    async with async_session_maker() as session:
                                        async with session.begin():
                                            await session.execute(
                                                update(MonitoredRepo)
                                                .where(MonitoredRepo.id == repo_db_id)
                                                .values(etag=etag)
                                            )
                                except Exception as db_e:
                                     logger.error(f"Failed to update ETag in DB for repo ID {repo_db_id}: {db_e}", exc_info=True)

                        # Reset retries on successful check (304 or 200 OK)
                        retries = 0

            except aiohttp.ClientError as e:
                retries += 1
                logger.warning(f"Network error monitoring {owner}/{repo}: {e}. Retry {retries}/{max_retries}.")
                if retries >= max_retries:
                    logger.error(f"Max network retries ({max_retries}) exceeded for {owner}/{repo}. Stopping monitor.")
                    try:
                       await bot.send_message(chat_id, strings["monitor"]["network_error"].format(repo_url=escape(repo_url)))
                    except Exception:
                       logger.warning(f"Failed to send network error notification to chat {chat_id} for {owner}/{repo}")
                    return True
                wait_time = check_interval * (2 ** (retries -1))
                logger.info(f"Waiting {wait_time}s before next check for {owner}/{repo}")
                await asyncio.sleep(wait_time)
                continue

            except Exception as e:
                logger.error(f"Unexpected error in monitor loop for {owner}/{repo}: {e}", exc_info=True)
                try:
                   await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=escape(repo_url)))
                except Exception:
                   logger.warning(f"Failed to send internal error notification to chat {chat_id} for {owner}/{repo}")
                return True

            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) cancelled.")
        return False
    except Exception as outer_e:
         logger.critical(f"Critical unexpected error outside main loop for {owner}/{repo} (ID: {repo_db_id}): {outer_e}", exc_info=True)
         return True
    finally:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) finished.")
