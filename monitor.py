import asyncio
import aiohttp
import logging
from .utils import parse_github_url
from pyrogram.errors import RPCError

async def monitor_repo(bot, chat_id, repo_url, check_interval, max_retries, github_token, strings):
    logger = logging.getLogger(f"gitMonitor[{chat_id}]")
    owner, repo = parse_github_url(repo_url)
    if not owner or not repo:
        logger.error(f"Invalid repo URL: {repo_url}")
        await bot.send_message(chat_id, strings["set_repo"]["invalid_url"].format(repo_url=repo_url))
        return

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    last_commit_sha = None
    etag = None
    retries = 0
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    logger.info(f"Starting monitor for {owner}/{repo}")

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
                            logger.error(f"Repository {owner}/{repo} not found (404).")
                            await bot.send_message(chat_id, strings["monitor"]["repo_not_found"].format(repo_url=repo_url))
                            return
                        if response.status == 403:
                            rate_limit_reset = response.headers.get('X-RateLimit-Reset', '')
                            reset_time_str = f" (resets at {rate_limit_reset})" if rate_limit_reset else ""
                            logger.warning(f"replyForbidden (403). Check token or rate limits{reset_time_str}.")
                            await asyncio.sleep(check_interval * (retries + 2))
                            continue
                        if response.status == 401:
                            logger.error("Unauthorized (401). Check token.")
                            await bot.send_message(chat_id, strings["monitor"]["auth_error"].format(repo_url=repo_url))
                            return

                        response.raise_for_status()
                        data = await response.json()
                        new_etag = response.headers.get("ETag")

                        if not data or not isinstance(data, list):
                            logger.warning("Invalid data from GitHub API.")
                            await asyncio.sleep(check_interval)
                            continue

                        latest_commit = data[0]
                        current_commit_sha = latest_commit.get("sha")
                        if not current_commit_sha:
                            logger.warning("No SHA in commit data.")
                            await asyncio.sleep(check_interval)
                            continue

                        if last_commit_sha is None:
                            last_commit_sha = current_commit_sha
                            logger.info(f"Initialized monitor. Last commit: {last_commit_sha[:7]}")
                        elif current_commit_sha != last_commit_sha:
                            logger.info(f"New commit: {current_commit_sha[:7]}")
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
                                logger.error(f"Failed to send notification: {e}")
                                return
                            last_commit_sha = current_commit_sha

                        etag = new_etag
                        retries = 0

            except aiohttp.ClientError as e:
                retries += 1
                logger.warning(f"Network error: {e}. Retry {retries}/{max_retries}.")
                if retries >= max_retries:
                    logger.error(f"Max retries exceeded for {repo_url}.")
                    await bot.send_message(chat_id, strings["monitor"]["network_error"].format(repo_url=repo_url))
                    return
                await asyncio.sleep(check_interval * retries)
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=repo_url))
                return

            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        logger.info(f"Monitor task for {owner}/{repo} cancelled.")
    finally:
        logger.info(f"Monitor task for {owner}/{repo} finished.")
