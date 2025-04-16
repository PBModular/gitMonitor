import asyncio
import aiohttp
import logging
from .utils import parse_github_url
from .db import MonitoredRepo
from pyrogram.errors import RPCError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    Returns True if monitoring should stop permanently (e.g., 404, 401), False otherwise.
    """
    logger = logging.getLogger(f"gitMonitor[{chat_id}][{repo_db_id}]")
    owner, repo = parse_github_url(repo_url)

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

            try:
                async with aiohttp.ClientSession(headers=current_headers) as http_session:
                    async with http_session.get(api_url, timeout=30) as response:
                        if response.status == 304:
                            logger.debug(f"No new commits for {owner}/{repo} (ETag match).")
                            retries = 0
                            await asyncio.sleep(check_interval)
                            continue

                        if response.status == 404:
                            logger.error(f"Repository {owner}/{repo} not found (404). Stopping monitor.")
                            await bot.send_message(chat_id, strings["monitor"]["repo_not_found"].format(repo_url=repo_url))
                            return True

                        if response.status == 403:
                            rate_limit_reset = response.headers.get('X-RateLimit-Reset', '')
                            reset_time_str = f" (resets at {rate_limit_reset})" if rate_limit_reset else ""
                            logger.warning(f"Forbidden (403). Check token or rate limits{reset_time_str}. Retrying after delay.")
                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"Rate limit / Forbidden error persisted after {max_retries} retries for {repo_url}. Stopping monitor.")
                                await bot.send_message(chat_id, strings["monitor"]["rate_limit_error"].format(repo_url=repo_url))
                                return True
                            await asyncio.sleep(check_interval * (2 ** retries))
                            continue

                        if response.status == 401:
                            logger.error("Unauthorized (401). Check token. Stopping monitor.")
                            await bot.send_message(chat_id, strings["monitor"]["auth_error"].format(repo_url=repo_url))
                            return True
                        response.raise_for_status()
                        data = await response.json()
                        new_etag = response.headers.get("ETag")

                        if not data or not isinstance(data, list):
                            logger.warning(f"Invalid data received from GitHub API for {owner}/{repo}. Retrying.")
                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"Received invalid data after {max_retries} retries for {repo_url}. Stopping monitor.")
                                await bot.send_message(chat_id, strings["monitor"]["invalid_data_error"].format(repo_url=repo_url))
                                return True
                            await asyncio.sleep(check_interval * retries)
                            continue

                        latest_commit = data[0]
                        current_commit_sha = latest_commit.get("sha")
                        if not current_commit_sha:
                            logger.warning(f"No SHA found in latest commit data for {owner}/{repo}. Retrying.")
                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"Could not find commit SHA after {max_retries} retries for {repo_url}. Stopping monitor.")
                                await bot.send_message(chat_id, strings["monitor"]["invalid_data_error"].format(repo_url=repo_url))
                                return True
                            await asyncio.sleep(check_interval * retries)
                            continue

                        if last_commit_sha is None:
                            last_commit_sha = current_commit_sha
                            logger.info(f"Initialized monitor for {owner}/{repo}. Last commit: {last_commit_sha[:7]}")

                        elif current_commit_sha != last_commit_sha:
                            logger.info(f"New commit detected for {owner}/{repo}: {current_commit_sha[:7]}")
                            commit_info = latest_commit.get("commit", {})
                            commit_url = latest_commit.get("html_url", "#")
                            author_info = commit_info.get("author", {})
                            author_name = author_info.get("name", "Unknown")
                            commit_message = commit_info.get("message", "No message").split('\n')[0]
                            sha_short = current_commit_sha[:7]

                            text = strings["monitor"]["new_commit"].format(
                                owner=owner,
                                repo=repo,
                                author=author_name,
                                message=commit_message,
                                sha=sha_short,
                                commit_url=commit_url
                            )
                            try:
                                await bot.send_message(chat_id, text, disable_web_page_preview=True)
                            except RPCError as e:
                                logger.error(f"Failed to send notification message to chat {chat_id}: {e}. Stopping monitor for this repo.")
                                return True

                            last_commit_sha = current_commit_sha

                        if last_commit_sha or new_etag != etag:
                            try:
                                async with async_session_maker() as session:
                                    async with session.begin():
                                        await session.execute(
                                            update(MonitoredRepo)
                                            .where(MonitoredRepo.id == repo_db_id)
                                            .values(last_commit_sha=last_commit_sha, etag=new_etag)
                                        )
                                    logger.debug(f"Updated DB for {owner}/{repo} (ID: {repo_db_id}). SHA: {last_commit_sha[:7] if last_commit_sha else 'None'}, ETag: {new_etag}")
                            except Exception as db_e:
                                logger.error(f"Failed to update DB state for repo ID {repo_db_id}: {db_e}", exc_info=True)

                        etag = new_etag
                        retries = 0

            except aiohttp.ClientError as e:
                retries += 1
                logger.warning(f"Network error monitoring {owner}/{repo}: {e}. Retry {retries}/{max_retries}.")
                if retries >= max_retries:
                    logger.error(f"Max network retries exceeded for {repo_url}. Stopping monitor.")
                    await bot.send_message(chat_id, strings["monitor"]["network_error"].format(repo_url=repo_url))
                    return True
                await asyncio.sleep(check_interval * (2 ** (retries -1)))
                continue
            except Exception as e:
                logger.error(f"Unexpected error in monitor loop for {owner}/{repo}: {e}", exc_info=True)
                await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=repo_url))
                return True

            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) cancelled.")
        return False
    finally:
        logger.info(f"Monitor task for {owner}/{repo} (ID: {repo_db_id}) finished.")
