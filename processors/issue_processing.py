from html import escape
from typing import List, Optional, Tuple, Dict, Any

def identify_new_issues(
    api_data: List[Dict[str, Any]],
    issue_number: Optional[int]
) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    """
    Identifies new issues from the API data (newest created first).
    Uses 'number' field of an issue as its identifier.
    
    Args:
        api_data: List of issue data from GitHub API (newest first by creation).
        issue_number: The number of the last issue known to the monitor.

    Returns:
            A tuple containing:
        1. new_issues_list: List of new issues (newest first by creation).
        2. latest_issue_number_on_github: Number of the newest issue from API data.
        3. is_initial_run: True if issue_number was None.
    """
    if not api_data or not isinstance(api_data, list) or not api_data[0].get("number"):
        return [], None, False

    latest_issue_number_on_github = api_data[0]["number"]
    if issue_number is None:
        return [], latest_issue_number_on_github, True

    if latest_issue_number_on_github <= issue_number:
        return [], latest_issue_number_on_github, False

    new_issues_list = []
    for issue_data in api_data:
        current_issue_number = issue_data["number"]
        if current_issue_number <= issue_number:
            break
        new_issues_list.append(issue_data)
    
    return new_issues_list, latest_issue_number_on_github, False

def identify_newly_closed_issues(
    api_data_closed_issues_updated_desc: List[Dict[str, Any]], 
    known_last_closed_issue_update_ts_str: Optional[str]
) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """
    Identifies newly closed issues from API data sorted by updated_at descending.

    Args:
        api_data_closed_issues_updated_desc: List of closed issue data from GitHub API (newest updated first).
        known_last_closed_issue_update_ts_str: ISO8601 timestamp of the last known closed issue's update time.

    Returns:
            A tuple containing:
        1. newly_closed_issues_for_notif: List of issues detected as newly closed (newest updated first).
        2. latest_overall_update_ts_from_api: 'updated_at' of the newest issue in the API payload.
        3. is_initial_poll: True if known_last_closed_issue_update_ts_str was None.
    """
    is_initial_poll = known_last_closed_issue_update_ts_str is None

    if not api_data_closed_issues_updated_desc or not isinstance(api_data_closed_issues_updated_desc, list):
        return [], None, is_initial_poll

    latest_overall_update_ts_from_api = api_data_closed_issues_updated_desc[0].get("updated_at")

    if is_initial_poll:
        return [], latest_overall_update_ts_from_api, True

    issues_for_notification = []
    for issue_data in api_data_closed_issues_updated_desc:
        if issue_data.get('state') != 'closed': 
            continue 
        
        current_issue_updated_at = issue_data.get("updated_at")
        if not current_issue_updated_at: 
            continue

        if current_issue_updated_at > known_last_closed_issue_update_ts_str:
            issues_for_notification.append(issue_data)
        else:
            break 
    
    return issues_for_notification, latest_overall_update_ts_from_api, False

def format_single_issue_message(issue_data: Dict[str, Any], owner: str, repo: str, strings: Dict) -> str:
    """Formats a notification message for a single new open issue."""
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

def format_closed_issue_message(issue_data: Dict[str, Any], owner: str, repo: str, strings: Dict) -> str:
    """Formats a notification message for a single closed issue."""
    issue_title = escape(issue_data.get("title", "No Title"))
    issue_number = issue_data["number"]
    issue_url = escape(issue_data.get("html_url", "#"))
    
    closed_by_user_info = issue_data.get("closed_by", {})
    closed_by_user = "Unknown"
    if closed_by_user_info and closed_by_user_info.get("login"):
        closed_by_user = escape(closed_by_user_info.get("login"))

    state_reason = issue_data.get("state_reason")
    reason_display = ""
    if state_reason == "completed":
        reason_display = strings["monitor"].get("issue_reason_completed", " (Completed)")
    elif state_reason == "not_planned":
        reason_display = strings["monitor"].get("issue_reason_not_planned", " (Not Planned)")
    
    return strings["monitor"]["issue_closed"].format(
        owner=escape(owner),
        repo=escape(repo),
        closed_by_user=closed_by_user,
        title=issue_title,
        number=issue_number,
        issue_url=issue_url,
        reason_display=reason_display
    )

def format_multiple_issues_message(
    new_issues_data_newest_first: List[Dict[str, Any]], 
    owner: str, 
    repo: str, 
    strings: Dict,
    max_to_list: int
) -> str:
    """Formats a notification message for multiple new open issues."""
    count = len(new_issues_data_newest_first)
    issue_list_lines = []
    
    issues_to_display_in_list = new_issues_data_newest_first[:max_to_list]
    
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

    more_link = ""
    if count > max_to_list:
        repo_issues_url = f"https://github.com/{escape(owner)}/{escape(repo)}/issues"
        more_link = strings["monitor"].get("more_issues","").format(issues_url=repo_issues_url)

    text = strings["monitor"]["multiple_new_issues"].format(
        count=count,
        owner=escape(owner),
        repo=escape(repo),
        issue_list="\n".join(issue_list_lines),
        latest_issue_number=latest_issue_number_notif,
        latest_issue_url=latest_issue_url_notif
    )
    if more_link and count > max_to_list:
      text += more_link
    return text
