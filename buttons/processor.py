from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from typing import List, TYPE_CHECKING, Any, Optional
from html import escape

from ..db import MonitoredRepo

if TYPE_CHECKING:
    from ..main import gitMonitorModule


ITEMS_PER_PAGE_BRANCHES = 8
ITEMS_PER_PAGE = 5

async def send_repo_selection_list(
    message_or_call: Any,
    repos: List[MonitoredRepo],
    page: int,
    S: dict,
    module_instance: 'gitMonitorModule'
) -> None:
    """Sends or edits a message with a paginated list of repos to select for settings."""
    buttons = []
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    paginated_repos = repos[start_idx:end_idx]

    for repo_entry in paginated_repos:
        branch_display_name = escape(repo_entry.branch) if repo_entry.branch else S["git_settings"]["default_branch_display"]
        commit_char = S["list_repos"]["status_enabled"] if repo_entry.monitor_commits else S["list_repos"]["status_disabled"]
        issue_char = S["list_repos"]["status_enabled"] if repo_entry.monitor_issues else S["list_repos"]["status_disabled"]
        tag_char = S["list_repos"]["status_enabled"] if repo_entry.monitor_tags else S["list_repos"]["status_disabled"]

        status_str = S["git_settings"].get("repo_list_status_format", "({branch}, C{c_char} I{i_char} T{t_char})").format(
            branch=branch_display_name, c_char=commit_char, i_char=issue_char, t_char=tag_char
        )
        button_text = f"{escape(repo_entry.owner)}/{escape(repo_entry.repo)} {status_str}"

        buttons.append([
            InlineKeyboardButton(
                button_text,
                callback_data=f"gitsettings_show_{repo_entry.id}_{page}"
            )
        ])

    nav_buttons = []
    total_pages = (len(repos) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(S["git_settings"]["prev_btn"], callback_data=f"gitsettings_list_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="gitsettings_dummy"))
    if end_idx < len(repos):
        nav_buttons.append(InlineKeyboardButton(S["git_settings"]["next_btn"], callback_data=f"gitsettings_list_{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(S["git_settings"]["close_btn"], callback_data="gitsettings_close")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = S["git_settings"]["select_repo_header"]
    
    try:
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.edit_message_text(text, reply_markup=keyboard)
        elif isinstance(message_or_call, Message):
            if message_or_call.from_user and message_or_call.from_user.is_self:
                try:
                    await message_or_call.edit_text(text, reply_markup=keyboard)
                except Exception: 
                    await message_or_call.reply_text(text, reply_markup=keyboard)
            else:
                await message_or_call.reply_text(text, reply_markup=keyboard)
    except Exception as e:
        module_instance.logger.error(f"Error sending/editing repo selection list: {e}")


async def send_repo_settings_panel(
    call_or_message: Any,
    repo_entry: MonitoredRepo,
    S: dict,
    current_list_page: int = 0,
    module_instance: Optional['gitMonitorModule'] = None
) -> None:
    """Sends or edits a message with the settings panel for a specific repo."""
    text = S["git_settings"]["header"].format(
        owner=escape(repo_entry.owner), repo=escape(repo_entry.repo), repo_id=repo_entry.id
    )

    commit_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_commits else S["git_settings"]["status_disabled"]
    issue_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_issues else S["git_settings"]["status_disabled"]
    tag_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_tags else S["git_settings"]["status_disabled"]
    
    current_branch_display = repo_entry.branch or S["git_settings"]["default_branch_display"]

    buttons = [
        [InlineKeyboardButton(
            S["git_settings"]["branch_btn"].format(branch_name=current_branch_display),
            callback_data=f"gitsettings_setbranch_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["commits_monitoring"].format(status=commit_status),
            callback_data=f"gitsettings_toggle_commits_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["issues_monitoring"].format(status=issue_status),
            callback_data=f"gitsettings_toggle_issues_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["tags_monitoring"].format(status=tag_status),
            callback_data=f"gitsettings_toggle_tags_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(S["git_settings"]["back_to_list_btn"], callback_data=f"gitsettings_list_{current_list_page}"),
         InlineKeyboardButton(S["git_settings"]["close_btn"], callback_data="gitsettings_close")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    if isinstance(call_or_message, CallbackQuery):
        await call_or_message.edit_message_text(text, reply_markup=keyboard)
    elif isinstance(call_or_message, Message):
        if call_or_message.from_user and call_or_message.from_user.is_self:
            try:
                await call_or_message.edit_text(text, reply_markup=keyboard)
                return
            except Exception:
                pass
        await call_or_message.reply_text(text, reply_markup=keyboard)

async def send_branch_selection_list(
    call: CallbackQuery,
    S: dict,
    module_instance: 'gitMonitorModule',
    branch_page: int
) -> None:
    """Sends or edits a message with a paginated list of branches to select."""
    message_id = call.message.id
    cached_data = module_instance.active_branch.get(message_id)

    if not cached_data:
        module_instance.logger.error(f"No cached data found for branch selection message_id {message_id}")
        await call.answer(S["git_settings"]["error"], show_alert=True)
        try:
            await call.message.delete()
        except Exception:
            pass
        return

    repo_id = cached_data["repo_id"]
    repo_owner = cached_data["repo_owner"]
    repo_name_str = cached_data["repo_name_str"]
    all_branches = cached_data["branches"]
    original_settings_list_page = cached_data["original_settings_list_page"]
    current_monitored_branch = cached_data["current_branch_name"]
    github_default_branch_name = cached_data.get("github_default_branch")

    buttons = []
    start_idx = branch_page * ITEMS_PER_PAGE_BRANCHES
    end_idx = start_idx + ITEMS_PER_PAGE_BRANCHES
    paginated_branches = all_branches[start_idx:end_idx]

    for i, branch_name in enumerate(paginated_branches):
        actual_branch_index = start_idx + i
        final_tag = "❌"
        is_actually_monitored = (current_monitored_branch == branch_name) or \
                                (current_monitored_branch is None and branch_name == github_default_branch_name)
        is_github_default = (branch_name == github_default_branch_name)

        if is_actually_monitored and is_github_default:
            final_tag = "✔️" + S["git_settings"]["github_default_tag"]
        elif is_actually_monitored:
            final_tag = "✔️"
        elif is_github_default:
            final_tag = "❌ " + S["git_settings"]["github_default_tag"]

        button_text = f"{escape(branch_name)} {final_tag}".strip()

        buttons.append([
            InlineKeyboardButton(
                button_text,
                callback_data=f"gitsettings_pickbranch_{actual_branch_index}"
            )
        ])

    total_pages = (len(all_branches) + ITEMS_PER_PAGE_BRANCHES - 1) // ITEMS_PER_PAGE_BRANCHES
    branch_pagination_row = []
    if branch_page > 0:
        branch_pagination_row.append(InlineKeyboardButton(
            S["git_settings"]["prev_btn"], callback_data=f"gitsettings_branchpage_{branch_page-1}"
        ))
    if total_pages > 1:
        branch_pagination_row.append(InlineKeyboardButton(
            S["git_settings"]["branch_page_indicator"].format(current_page=branch_page + 1, total_pages=total_pages),
            callback_data="gitsettings_dummy"
        ))
    if end_idx < len(all_branches):
        branch_pagination_row.append(InlineKeyboardButton(
            S["git_settings"]["next_btn"], callback_data=f"gitsettings_branchpage_{branch_page+1}"
        ))
    
    if branch_pagination_row:
        buttons.append(branch_pagination_row)

    action_row = []
    action_row.append(InlineKeyboardButton(S["git_settings"]["back_to_settings_btn"], callback_data=f"gitsettings_show_{repo_id}_{original_settings_list_page}"))
    if branch_page == 0:
        action_row.append(InlineKeyboardButton(S["git_settings"]["monitor_default_branch_btn"], callback_data=f"gitsettings_pickbranch_DEFAULT"))
    
    if action_row:
        buttons.append(action_row)

    keyboard = InlineKeyboardMarkup(buttons)

    header_text = S["git_settings"]["select_branch_header"].format(owner=escape(repo_owner), repo=escape(repo_name_str))
    effective_monitored_branch_name = current_monitored_branch
    if current_monitored_branch is None:
        effective_monitored_branch_name = github_default_branch_name 

    current_branch_display = escape(effective_monitored_branch_name) if effective_monitored_branch_name else S["git_settings"]["default_branch_display"]
    status_text = S["git_settings"]["current_branch_indicator"].format(branch_name=current_branch_display)

    full_text = f"{header_text}\n{status_text}"

    await call.edit_message_text(full_text, reply_markup=keyboard)
    await call.answer()