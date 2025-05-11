import logging
import datetime
from html import escape
from typing import Tuple, Dict, Any, TYPE_CHECKING

from ..api.github_api import (
    APIError, NotFoundError, UnauthorizedError, ForbiddenError,
    ClientRequestError, InvalidResponseError
)

if TYPE_CHECKING:
    from pyrogram import Client as PyrogramClient

async def handle_api_error(
    error: APIError,
    owner: str,
    repo_name: str,
    repo_url: str,
    attempt_number: int,
    max_attempts: int,
    base_check_interval: float,
    logger: logging.Logger,
    bot: 'PyrogramClient',
    chat_id: int,
    strings: Dict[str, Any]
) -> Tuple[bool, float]:
    """
    Handles API errors, determines if monitoring should stop, and calculates wait time.
    Sends notifications to the user if appropriate.
    """
    if isinstance(error, NotFoundError):
        logger.error(f"Repository {owner}/{repo_name} not found (404): {error.message}. Stopping monitor.")

        try:
            await bot.send_message(chat_id, strings["monitor"]["repo_not_found"].format(repo_url=escape(repo_url)))
        except Exception as send_err:
            logger.warning(f"Failed to send 'repo_not_found' notification for {owner}/{repo_name}: {send_err}")
        return True, 0

    if isinstance(error, UnauthorizedError):
        logger.error(f"Unauthorized (401) for {owner}/{repo_name}: {error.message}. Check token. Stopping monitor.")

        try:
            await bot.send_message(chat_id, strings["monitor"]["auth_error"].format(repo_url=escape(repo_url)))
        except Exception as send_err:
            logger.warning(f"Failed to send 'auth_error' notification for {owner}/{repo_name}: {send_err}")
        return True, 0

    if isinstance(error, ForbiddenError):
        rate_limit_reset = error.headers.get('X-RateLimit-Reset', '')
        reset_time_str = ""

        if rate_limit_reset:
            try:
                reset_dt = datetime.datetime.fromtimestamp(int(rate_limit_reset), tz=datetime.timezone.utc)
                reset_time_str = f" (resets at {reset_dt.strftime('%Y-%m-%d %H:%M:%S %Z')})"
            except ValueError: pass
        
        logger.warning(f"Forbidden/Rate Limit (403) for {owner}/{repo_name}{reset_time_str}: {error.message}.")

        if attempt_number >= max_attempts:
            logger.error(f"Rate limit / Forbidden error persisted after {max_attempts} retries for {owner}/{repo_name}. Stopping monitor.")

            try:
                await bot.send_message(chat_id, strings["monitor"]["rate_limit_error"].format(repo_url=escape(repo_url)))
            except Exception as send_err:
                logger.warning(f"Failed to send 'rate_limit_error' notification for {owner}/{repo_name}: {send_err}")
            return True, 0
        
        retry_after_seconds_str = error.headers.get('Retry-After')
        wait_time = base_check_interval * (2 ** (attempt_number -1))
        if retry_after_seconds_str:
            try:
                retry_after_seconds = int(retry_after_seconds_str)
                wait_time = max(wait_time, retry_after_seconds + 5) 
                logger.info(f"Using Retry-After header: {retry_after_seconds}s. Effective wait: {wait_time:.2f}s")
            except ValueError: pass
        
        logger.info(f"Waiting {wait_time:.2f}s before next check for {owner}/{repo_name} (Retry {attempt_number}/{max_attempts})")
        return False, wait_time

    if isinstance(error, (ClientRequestError, InvalidResponseError)): 
        logger.warning(f"API request or response error for {owner}/{repo_name}: {type(error).__name__} - {str(error)}. Retry {attempt_number}/{max_attempts}.")

        if attempt_number >= max_attempts:
            logger.error(f"Max retries ({max_attempts}) exceeded for {owner}/{repo_name} due to {type(error).__name__}. Stopping monitor.")
            error_key = "invalid_data_error" if isinstance(error, InvalidResponseError) else "network_error"

            try:
                await bot.send_message(chat_id, strings["monitor"][error_key].format(repo_url=escape(repo_url)))
            except Exception as send_err:
                logger.warning(f"Failed to send '{error_key}' notification for {owner}/{repo_name}: {send_err}")
            return True, 0
        
        wait_time = base_check_interval * (2 ** (attempt_number -1))
        logger.info(f"Waiting {wait_time:.2f}s before next check for {owner}/{repo_name}")
        return False, wait_time

    logger.error(f"Unhandled APIError for {owner}/{repo_name}: {error}. Stopping monitor.", exc_info=True)

    try:
        await bot.send_message(chat_id, strings["monitor"]["internal_error"].format(repo_url=escape(repo_url)))
    except Exception as send_err:
        logger.warning(f"Failed to send 'internal_error' notification for {owner}/{repo_name}: {send_err}")
    return True, 0
