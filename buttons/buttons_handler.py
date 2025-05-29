import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from typing import List, TYPE_CHECKING, Any, Optional, Dict
from html import escape

from ..api.github_api import GitHubAPIClient, APIError
from ..db import MonitoredRepo
from .. import db_ops

if TYPE_CHECKING:
    from ..main import gitMonitorModule

ITEMS_PER_PAGE = 5
ITEMS_PER_PAGE_BRANCHES = 8

async def send_repo_selection_list(
    message: Message,
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
        buttons.append([
            InlineKeyboardButton(
                f"{repo_entry.owner}/{repo_entry.repo}",
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
        if message.from_user and message.from_user.is_self and not isinstance(message, CallbackQuery):
            await message.edit_text(text, reply_markup=keyboard)
        elif isinstance(message, CallbackQuery):
            await message.edit_message_text(text, reply_markup=keyboard)
        else:
            await message.reply_text(text, reply_markup=keyboard)
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

    buttons = []
    start_idx = branch_page * ITEMS_PER_PAGE_BRANCHES
    end_idx = start_idx + ITEMS_PER_PAGE_BRANCHES
    paginated_branches = all_branches[start_idx:end_idx]

    for i, branch_name in enumerate(paginated_branches):
        actual_branch_index = start_idx + i
        is_current = "✔️ " if branch_name == current_monitored_branch else "❌ "
        buttons.append([
            InlineKeyboardButton(
                f"{is_current}{escape(branch_name)}",
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
    current_branch_display = escape(current_monitored_branch) if current_monitored_branch else S["git_settings"]["default_branch_display"]
    status_text = S["git_settings"]["current_branch_indicator"].format(branch_name=current_branch_display)

    full_text = f"{header_text}\n{status_text}"

    await call.edit_message_text(full_text, reply_markup=keyboard)
    await call.answer()

async def handle_settings_callback(
    call: CallbackQuery,
    module_instance: 'gitMonitorModule'
):
    """Handles callbacks from the settings UI."""
    S = module_instance.S
    async_session_maker = module_instance.async_session
    chat_id = call.message.chat.id
    message_id = call.message.id
    user_id = call.from_user.id
    
    parts = call.data.split("_")
    action_type = parts[1]

    if action_type == "close":
        await call.message.delete()
        module_instance.active_branch.pop(message_id, None)
        await call.answer()
        return
    
    if action_type == "dummy":
        await call.answer()
        return

    if action_type == "list":
        page = int(parts[2])
        async with async_session_maker() as session:
            repos = await db_ops.get_repos_for_chat(session, chat_id)
        if not repos:
            await call.answer(S["list_repos"]["none"], show_alert=True)
            if call.message.from_user and call.message.from_user.is_self:
                try:
                    await call.message.delete()
                except Exception:
                    pass
            return
        await send_repo_selection_list(call, repos, page, S, module_instance)
        await call.answer()
        return

    repo_id: int
    current_list_page: int

    if action_type == "show":
        repo_id = int(parts[2])
        current_list_page = int(parts[3])
        module_instance.active_branch.pop(message_id, None)

        async with async_session_maker() as session:
            repo_entry = await db_ops.get_repo_by_id(session, repo_id)
        if not repo_entry or repo_entry.chat_id != chat_id:
            await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
            return
        await send_repo_settings_panel(call, repo_entry, S, current_list_page, module_instance)
        await call.answer()
        return

    if action_type == "toggle":
        toggle_target = parts[2]
        repo_id = int(parts[3])
        current_list_page = int(parts[4])

        field_to_toggle = ""
        if toggle_target == "commits":
            field_to_toggle = "monitor_commits"
        elif toggle_target == "issues":
            field_to_toggle = "monitor_issues"
        elif toggle_target == "tags":
            field_to_toggle = "monitor_tags"
        else:
            await call.answer("Unknown toggle target", show_alert=True)
            return

        updated_repo_entry = None
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    repo_entry = await db_ops.get_repo_by_id(session, repo_id)
                    if not repo_entry or repo_entry.chat_id != chat_id:
                        await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
                        return

                    current_value = getattr(repo_entry, field_to_toggle)
                    new_value = not current_value
                    
                    await db_ops.update_repo_fields(session, repo_id, **{field_to_toggle: new_value})
                updated_repo_entry = await db_ops.get_repo_by_id(session, repo_id)

            if updated_repo_entry:
                await module_instance._start_monitor_task(updated_repo_entry)
                await send_repo_settings_panel(call, updated_repo_entry, S, current_list_page, module_instance)
                await call.answer(S["git_settings"]["updated_ok"].format(owner=updated_repo_entry.owner, repo=updated_repo_entry.repo))
            else:
                raise Exception("Repo not found after update attempt")

        except Exception as e:
            module_instance.logger.error(f"Error toggling setting '{field_to_toggle}' for repo {repo_id}: {e}", exc_info=True)
            await call.answer(S["git_settings"]["error"], show_alert=True)
        return

    if action_type == "setbranch":
        repo_id = int(parts[2])
        current_list_page = int(parts[3])

        async with async_session_maker() as session:
            repo_entry_for_branches = await db_ops.get_repo_by_id(session, repo_id)
        
        if not repo_entry_for_branches or repo_entry_for_branches.chat_id != chat_id:
            await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
            return

        temp_api_client = GitHubAPIClient(token=module_instance.github_token, loop=asyncio.get_event_loop())
        branches_data = []
        try:
            await call.answer(S["git_settings"]["fetching_branches"])
            response = await temp_api_client.fetch_branches(repo_entry_for_branches.owner, repo_entry_for_branches.repo)
            if response.data and isinstance(response.data, list):
                branches_data = sorted([branch_item['name'] for branch_item in response.data if 'name' in branch_item])
        except APIError as e:
            module_instance.logger.warning(f"API Error fetching branches for {repo_entry_for_branches.owner}/{repo_entry_for_branches.repo}: {e}")
            await call.answer(S["git_settings"]["fetch_branches_error"], show_alert=True)
            await send_repo_settings_panel(call, repo_entry_for_branches, S, current_list_page, module_instance)
            return 
        finally:
            await temp_api_client.close()

        if not branches_data:
            await call.answer(S["git_settings"]["no_branches_found"], show_alert=True)
            await send_repo_settings_panel(call, repo_entry_for_branches, S, current_list_page, module_instance)
            return

        module_instance.active_branch[message_id] = {
            "repo_id": repo_id,
            "repo_owner": repo_entry_for_branches.owner,
            "repo_name_str": repo_entry_for_branches.repo,
            "branches": branches_data,
            "original_settings_list_page": current_list_page,
            "current_branch_name": repo_entry_for_branches.branch
        }
        await send_branch_selection_list(call, S, module_instance, branch_page=0)
        return

    if action_type == "branchpage":
        page_num = int(parts[2])
        await send_branch_selection_list(call, S, module_instance, branch_page=page_num)
        return

    if action_type == "pickbranch":
        branch_identifier = parts[2]
        
        cached_data = module_instance.active_branch.pop(message_id, None)
        if not cached_data:
            module_instance.logger.error(f"Cache miss for pickbranch, message_id {message_id}")
            await call.answer(S["git_settings"]["error"], show_alert=True)
            try: await call.message.delete()
            except: pass
            return

        repo_id = cached_data["repo_id"]
        all_branches: List[str] = cached_data["branches"]
        original_settings_list_page = cached_data["original_settings_list_page"]

        new_branch_name: Optional[str]
        if branch_identifier == "DEFAULT":
            new_branch_name = None
        else:
            try:
                branch_idx = int(branch_identifier)
                if 0 <= branch_idx < len(all_branches):
                    new_branch_name = all_branches[branch_idx]
                else:
                    raise ValueError("Branch index out of bounds")
            except ValueError:
                module_instance.logger.error(f"Invalid branch_idx '{branch_identifier}' for pickbranch.")
                await call.answer(S["git_settings"]["error"], show_alert=True)
                async with async_session_maker() as session:
                    repo_entry_fallback = await db_ops.get_repo_by_id(session, repo_id)
                if repo_entry_fallback:
                    await send_repo_settings_panel(call, repo_entry_fallback, S, original_settings_list_page, module_instance)
                return
        
        await call.answer()

        try:
            updated_repo_entry = None
            async with async_session_maker() as session:
                async with session.begin():
                    repo_to_update = await db_ops.get_repo_by_id(session, repo_id)
                    if not repo_to_update or repo_to_update.chat_id != chat_id:
                        await module_instance.bot.send_message(chat_id, S["git_settings"]["repo_not_found_generic"])
                        return

                    update_payload: Dict[str, Any] = {"branch": new_branch_name}
                    if repo_to_update.branch != new_branch_name:
                        module_instance.logger.info(f"Branch changing for repo {repo_id} from '{repo_to_update.branch}' to '{new_branch_name}'. Resetting commit state.")
                        update_payload["last_commit_sha"] = None
                        update_payload["commit_etag"] = None
                    
                    await db_ops.update_repo_fields(session, repo_id, **update_payload)
                
                updated_repo_entry = await db_ops.get_repo_by_id(session, repo_id)

            if updated_repo_entry:
                await module_instance._start_monitor_task(updated_repo_entry)
                await send_repo_settings_panel(call, updated_repo_entry, S, original_settings_list_page, module_instance)
                
                branch_confirm_display = new_branch_name or S["git_settings"]["default_branch_display"]
                await call.answer(S["git_settings"]["branch_updated_ok"].format(branch_name=branch_confirm_display), show_alert=False)

            else:
                await call.answer(S["git_settings"]["error"], show_alert=True)
                async with async_session_maker() as s: repo_entry_fallback = await db_ops.get_repo_by_id(s, repo_id)
                if repo_entry_fallback: await send_repo_settings_panel(call, repo_entry_fallback, S, original_settings_list_page, module_instance)
        except Exception as e:
            module_instance.logger.error(f"Error in pickbranch update flow for repo {repo_id}: {e}", exc_info=True)
            await call.answer(S["git_settings"]["error"], show_alert=True)
            async with async_session_maker() as s: repo_entry_fallback = await db_ops.get_repo_by_id(s, repo_id)
            if repo_entry_fallback: await send_repo_settings_panel(call, repo_entry_fallback, S, original_settings_list_page, module_instance)
        return

    await call.answer("Unknown settings action.", show_alert=True)
    module_instance.active_branch.pop(message_id, None)
