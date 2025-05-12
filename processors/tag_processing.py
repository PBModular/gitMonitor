from html import escape
from typing import List, Optional, Tuple, Dict, Any

def identify_new_tags(
    api_data: List[Dict[str, Any]],
    known_last_tag_name: Optional[str]
) -> Tuple[List[Dict[str, Any]], Optional[str], bool, bool]:
    """
    Identifies new tags from the API data (expected newest first).
    
    Args:
        api_data: List of tag data from GitHub API (newest first).
        known_last_tag_name: The name of the last tag known to the monitor.

    Returns:
            A tuple containing:
        1. new_tags_list: List of new tags (newest first).
        2. latest_tag_name_on_github: Name of the newest tag from API data.
        3. is_initial_run: True if known_last_tag_name was None.
        4. known_tag_not_found: True if known_last_tag_name was not found (and not initial run).
    """
    if not api_data or not isinstance(api_data, list) or not api_data[0].get("name"):
        return [], None, False, False

    latest_tag_name_on_github = api_data[0]["name"]
    
    if known_last_tag_name is None:
        return [], latest_tag_name_on_github, True, False

    if latest_tag_name_on_github == known_last_tag_name:
        return [], latest_tag_name_on_github, False, False

    new_tags_list = []
    found_last_tag_in_payload = False
    for tag_data in api_data:
        if tag_data["name"] == known_last_tag_name:
            found_last_tag_in_payload = True
            break
        new_tags_list.append(tag_data)
    
    known_tag_not_found = not found_last_tag_in_payload
    
    return new_tags_list, latest_tag_name_on_github, False, known_tag_not_found

def format_new_tag_message(tag_data: Dict[str, Any], owner: str, repo: str, strings: Dict) -> str:
    """Formats a notification message for a single new tag."""
    tag_name = escape(tag_data["name"])
    commit_sha = tag_data.get("commit", {}).get("sha", "")
    sha_short = commit_sha[:7] if commit_sha else "N/A"
    tag_url = escape(f"https://github.com/{owner}/{repo}/releases/tag/{tag_name}")

    return strings["monitor"]["new_tag"].format(
        owner=escape(owner),
        repo=escape(repo),
        tag_name=tag_name,
        sha_short=sha_short,
        tag_url=tag_url
    )

def format_multiple_tags_message(
    new_tags_data_newest_first: List[Dict[str, Any]], 
    owner: str, 
    repo: str, 
    strings: Dict,
    max_to_list: int
) -> str:
    """Formats a notification message for multiple new tags."""
    count = len(new_tags_data_newest_first)
    tag_list_lines = []
    
    tags_to_display_in_list = new_tags_data_newest_first[:max_to_list]
    
    for tag_data in reversed(tags_to_display_in_list):
        tag_name = escape(tag_data["name"])
        commit_sha = tag_data.get("commit", {}).get("sha", "")
        sha_short = commit_sha[:7] if commit_sha else "N/A"
        tag_url = escape(f"https://github.com/{owner}/{repo}/releases/tag/{tag_name}")

        tag_list_lines.append(
            strings["monitor"]["tag_line"].format(
                url=tag_url,
                name=tag_name,
                sha_short=sha_short
            )
        )

    latest_tag_overall = new_tags_data_newest_first[0]
    latest_tag_name_notif = escape(latest_tag_overall["name"])
    latest_tag_url_notif = escape(f"https://github.com/{owner}/{repo}/releases/tag/{latest_tag_overall['name']}")

    more_tags_link = ""
    if count > max_to_list:
        tags_page_url = escape(f"https://github.com/{owner}/{repo}/tags")
        more_tags_link = strings["monitor"]["more_tags"].format(tags_page_url=tags_page_url)

    text = strings["monitor"]["multiple_new_tags"].format(
        count=count,
        owner=escape(owner),
        repo=escape(repo),
        tag_list="\n".join(tag_list_lines),
        latest_tag_name=latest_tag_name_notif,
        latest_tag_url=latest_tag_url_notif
    )
    if more_tags_link:
        text += more_tags_link
    return text
