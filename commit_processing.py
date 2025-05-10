from html import escape
from typing import List, Optional, Tuple, Dict, Any
from .utils import get_merge_info

def identify_new_commits(
    api_data: List[Dict[str, Any]],
    known_last_sha: Optional[str]
) -> Tuple[List[Dict[str, Any]], Optional[str], bool, bool]:
    """
    Identifies new commits from the API data (expected newest first).
    
    Args:
        api_data: List of commit data from GitHub API (newest first).
        known_last_sha: The SHA of the last commit known to the monitor.

    Returns:
            A tuple containing:
        1. new_commits_list: List of new commits (newest first).
        2. latest_commit_sha_on_github: SHA of the newest commit from API data.
        3. is_initial_run: True if known_last_sha was None.
        4. force_pushed_or_many_new: True if known_last_sha was not found in api_data (and not initial run).
    """
    if not api_data or not isinstance(api_data, list) or not api_data[0].get("sha"):
        return [], None, False, False 

    latest_commit_sha_on_github = api_data[0]["sha"]
    
    if known_last_sha is None:
        return [], latest_commit_sha_on_github, True, False

    if latest_commit_sha_on_github == known_last_sha:
        return [], latest_commit_sha_on_github, False, False

    # Collect commits until known_last_sha is found or list exhausted
    new_commits_list = []
    found_last_sha_in_payload = False
    for commit_data in api_data:
        if commit_data["sha"] == known_last_sha:
            found_last_sha_in_payload = True
            break
        new_commits_list.append(commit_data)
    
    force_pushed_or_many_new = not found_last_sha_in_payload
    
    return new_commits_list, latest_commit_sha_on_github, False, force_pushed_or_many_new


def format_single_commit_message(commit_data: Dict[str, Any], owner: str, repo: str, strings: Dict) -> str:
    """Formats a notification message for a single commit."""
    merge_info = get_merge_info(commit_data)
    merge_indicator = ''
    if merge_info:
        if merge_info["type"] == "pr":
            pr_url = f"https://github.com/{owner}/{repo}/pull/{merge_info['number']}"
            merge_indicator = f' [<a href="{pr_url}">PR #{merge_info["number"]} merged</a>]'
        else:
            merge_indicator = ' [Merge commit]'
    
    commit_info = commit_data.get("commit", {})
    author_info = commit_info.get("author", {})
    author_name = escape(author_info.get("name", "Unknown"))
    commit_message = escape(commit_info.get("message", "No message").split('\n')[0])
    sha_short = commit_data['sha'][:7]
    commit_url = escape(commit_data.get("html_url", "#"))

    return strings["monitor"]["new_commit"].format(
        owner=escape(owner),
        repo=escape(repo),
        author=author_name,
        message=commit_message,
        merge_indicator=merge_indicator,
        sha=sha_short,
        commit_url=commit_url
    )

def format_multiple_commits_message(
    new_commits_data_newest_first: List[Dict[str, Any]], 
    owner: str, 
    repo: str, 
    strings: Dict,
    previous_known_sha: Optional[str],
    max_commits_to_list: int
) -> str:
    """Formats a notification message for multiple new commits."""
    count = len(new_commits_data_newest_first)
    commit_list_lines = []
    
    commits_to_display_in_list = new_commits_data_newest_first[:max_commits_to_list]
    
    for commit in reversed(commits_to_display_in_list):
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

    # latest_commit_overall = new_commits_data_newest_first[0]
    # latest_sha_short_notif = latest_commit_overall['sha'][:7]
    # latest_commit_url_notif = escape(latest_commit_overall.get("html_url", "#"))

    more_link = ""
    if count > max_commits_to_list:
        compare_url_base = previous_known_sha
        compare_url_head = new_commits_data_newest_first[0]['sha']

        if not compare_url_base and len(new_commits_data_newest_first) > 1:
            compare_url_base = new_commits_data_newest_first[-1]['sha']


        if compare_url_base and compare_url_base != compare_url_head:
            compare_url = escape(f"https://github.com/{owner}/{repo}/compare/{compare_url_base}...{compare_url_head}")
            more_link = strings["monitor"]["more_commits"].format(compare_url=compare_url)

    text = strings["monitor"]["multiple_new_commits"].format(
        count=count,
        owner=escape(owner),
        repo=escape(repo),
        commit_list="\n".join(commit_list_lines),
        latest_sha=latest_sha_short_notif,
        latest_commit_url=latest_commit_url_notif
    ) + more_link
    return text
