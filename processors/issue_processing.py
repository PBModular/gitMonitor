from html import escape
from typing import List, Optional, Tuple, Dict, Any

MAX_ISSUES_TO_LIST_IN_NOTIFICATION = 4

def identify_new_issues(
    api_data_newest_first: List[Dict[str, Any]],
    known_last_issue_number: Optional[int]
) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    """
    Identifies new issues from the API data.
    Uses 'number' field of an issue as its identifier.
    
    Args:
        api_data_newest_first: List of issue data from GitHub API (newest first).
        known_last_issue_number: The number of the last issue known to the monitor.

    Returns:
            A tuple containing:
        1. new_issues_list: List of new issues (newest first).
        2. latest_issue_number_on_github: Number of the newest issue from API data.
        3. is_initial_run: True if known_last_issue_number was None.
    """
    if not api_data_newest_first or not isinstance(api_data_newest_first, list) or not api_data_newest_first[0].get("number"):
        return [], None, False 

    latest_issue_number_on_github = api_data_newest_first[0]["number"]
    
    if known_last_issue_number is None:
        return [], latest_issue_number_on_github, True

    if latest_issue_number_on_github == known_last_issue_number:
        return [], latest_issue_number_on_github, False

    new_issues_list = []
    for issue_data in api_data_newest_first:
        current_issue_number = issue_data["number"]
        if current_issue_number == known_last_issue_number:
            break
        if current_issue_number < known_last_issue_number:
            break 
        new_issues_list.append(issue_data)
    
    return new_issues_list, latest_issue_number_on_github, False


def format_single_issue_message(issue_data: Dict[str, Any], owner: str, repo: str, strings: Dict) -> str:
    """Formats a notification message for a single new issue."""
    issue_title = escape(issue_data.get("title", "No Title"))
    issue_number = issue_data["number"]
    issue_url = escape(issue_data.get("html_url", "#"))
    user_info = issue_data.get("user", {})
    author_name = escape(user_info.get("login", "Unknown"))

    return strings["monitor"]["new_issue"].format(
        owner=escape(owner),
        repo=escape(repo),
        author=author_name,
        title=issue_title,
        number=issue_number,
        issue_url=issue_url
    )

def format_multiple_issues_message(
    new_issues_data_newest_first: List[Dict[str, Any]], 
    owner: str, 
    repo: str, 
    strings: Dict
) -> str:
    """Formats a notification message for multiple new issues."""
    count = len(new_issues_data_newest_first)
    issue_list_lines = []
    
    issues_to_display_in_list = new_issues_data_newest_first[:MAX_ISSUES_TO_LIST_IN_NOTIFICATION]
    
    # Display issues oldest first in the summary, so reverse the sub-list
    for issue in reversed(issues_to_display_in_list):
        issue_title = escape(issue.get("title", "No Title").split('\n')[0])
        issue_number = issue["number"]
        issue_url = escape(issue.get("html_url", "#"))
        user_info = issue.get("user", {})
        author_name = escape(user_info.get("login", "Unknown"))

        issue_list_lines.append(
            strings["monitor"]["issue_line"].format(
                url=issue_url,
                number=issue_number,
                title=issue_title,
                author=author_name
            )
        )
    
    latest_issue_overall = new_issues_data_newest_first[0]
    latest_issue_number_notif = latest_issue_overall['number']
    latest_issue_url_notif = escape(latest_issue_overall.get("html_url", "#"))


    text = strings["monitor"]["multiple_new_issues"].format(
        count=count,
        owner=escape(owner),
        repo=escape(repo),
        issue_list="\n".join(issue_list_lines),
        latest_issue_number=latest_issue_number_notif,
        latest_issue_url=latest_issue_url_notif
    )
    return text
