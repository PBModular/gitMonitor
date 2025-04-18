from urllib.parse import urlparse

def parse_github_url(url: str) -> tuple[str | None, str | None]:
    """
    Parses a GitHub URL to extract owner and repo.
    Handles common formats like https://github.com/owner/repo and https://github.com/owner/repo.git
    """
    if not url:
        return None, None
    try:
        parsed = urlparse(url)
        if parsed.netloc.lower() != 'github.com':
            return None, None

        path_parts = [part for part in parsed.path.strip('/').split('/') if part]

        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1].replace('.git', '')
            if owner and repo:
                return owner, repo
    except Exception:
        pass
    return None, None

def get_merge_info(commit):
    """
    Determines if a commit is a merge commit and its type (pull request or regular merge).
    Returns None if not a merge, or a dict with 'type' and optionally 'number' for PRs.
    """
    parents = commit.get("parents", [])
    if len(parents) <= 1:
        return None
    message = commit.get("commit", {}).get("message", "")
    if message.startswith("Merge pull request #"):
        parts = message.split()
        if len(parts) >= 4 and parts[3].startswith("#"):
            pr_number = parts[3][1:]
            if pr_number.isdigit():
                return {"type": "pr", "number": pr_number}
    return {"type": "merge"}
