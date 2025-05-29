import aiohttp
import asyncio
import logging
from typing import Optional, Any, Dict, Literal

class APIError(Exception):
    """Base class for API errors."""
    def __init__(self, status_code: int, message: str, headers: Optional[Dict] = None):
        self.status_code = status_code
        self.message = message
        self.headers = headers or {}
        super().__init__(f"API Error {status_code}: {message}")

class NotFoundError(APIError): pass
class UnauthorizedError(APIError): pass
class ForbiddenError(APIError): pass
class ClientRequestError(APIError): pass
class InvalidResponseError(APIError): pass

class GitHubAPIResponse:
    def __init__(self, status_code: int, data: Optional[Any], etag: Optional[str], headers: Dict):
        self.status_code = status_code
        self.data = data
        self.etag = etag
        self.headers = headers

class GitHubAPIClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.token = token
        self._session: Optional[aiohttp.ClientSession] = None
        self._loop = loop or asyncio.get_event_loop()
        self.logger = logging.getLogger(__name__)
        
        self._base_headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            self._base_headers["Authorization"] = f"Bearer {self.token}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self.logger.debug("Creating new aiohttp.ClientSession")
            self._session = aiohttp.ClientSession(headers=self._base_headers, loop=self._loop)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            self.logger.debug("Closing aiohttp.ClientSession")
            await self._session.close()
            self._session = None

    async def _request(self, method: str, url: str, params: Optional[Dict] = None, request_specific_headers: Optional[Dict] = None) -> GitHubAPIResponse:
        session = await self._get_session()
        
        try:
            async with session.request(method, url, params=params, headers=request_specific_headers, timeout=30) as response:
                response_etag = response.headers.get("ETag")
                
                if response.status == 304: # Not Modified
                    return GitHubAPIResponse(status_code=304, data=None, etag=response_etag, headers=dict(response.headers))
                
                # Check for specific errors before trying to parse JSON
                if response.status == 404:
                    raise NotFoundError(response.status, f"Resource not found: {url}", dict(response.headers))
                if response.status == 401:
                    raise UnauthorizedError(response.status, f"Unauthorized for: {url}. Check token.", dict(response.headers))
                if response.status == 403: # Forbidden or Rate Limit
                    raise ForbiddenError(response.status, f"Forbidden or rate limited for: {url}", dict(response.headers))

                response.raise_for_status() # Raises for other 4xx/5xx errors
                
                try:
                    data = await response.json()
                except aiohttp.ContentTypeError as e: # Or json.JSONDecodeError
                    self.logger.error(f"Failed to decode JSON from {url}: {e}. Response text: {await response.text()[:200]}")
                    raise InvalidResponseError(response.status, f"Invalid JSON response from {url}", dict(response.headers))

                return GitHubAPIResponse(status_code=response.status, data=data, etag=response_etag, headers=dict(response.headers))

        except aiohttp.ClientError as e:
            self.logger.warning(f"aiohttp.ClientError during request to {url}: {e}")
            raise ClientRequestError(0, str(e)) 

    async def fetch_repo_details(
        self,
        owner: str,
        repo: str
    ) -> GitHubAPIResponse:
        """Fetches general details for a repository."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}"
        return await self._request("GET", url)

    async def fetch_branches(
        self,
        owner: str,
        repo: str,
        per_page: int = 15
    ) -> GitHubAPIResponse:
        """Fetches all branches for a repository."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/branches"
        params: Dict[str, Any] = {"per_page": per_page}
        return await self._request("GET", url, params=params)

    async def fetch_commits(
        self,
        owner: str,
        repo: str,
        etag: Optional[str] = None,
        per_page: int = 30,
        sha_or_branch: Optional[str] = None
        ) -> GitHubAPIResponse:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/commits"
        params: Dict[str, Any] = {"per_page": per_page}
        if sha_or_branch:
            params["sha"] = sha_or_branch
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        return await self._request("GET", url, params=params, request_specific_headers=headers)

    async def fetch_issues(
        self,
        owner: str,
        repo: str,
        etag: Optional[str] = None,
        per_page: int = 30,
        sort: Literal["created", "updated", "comments"] = "created",
        direction: Literal["asc", "desc"] = "desc",
        state: Literal["open", "closed", "all"] = "open",
        since: Optional[str] = None
    ) -> GitHubAPIResponse:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues"
        params: Dict[str, Any] = {"per_page": per_page, "sort": sort, "direction": direction, "state": state}
        if since:
            params["since"] = since
            
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        return await self._request("GET", url, params=params, request_specific_headers=headers)

    async def fetch_tags(
        self,
        owner: str,
        repo: str,
        etag: Optional[str] = None,
        per_page: int = 30
    ) -> GitHubAPIResponse:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/tags"
        params: Dict[str, Any] = {"per_page": per_page}
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        return await self._request("GET", url, params=params, request_specific_headers=headers)
