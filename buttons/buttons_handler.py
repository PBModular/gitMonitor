import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from pyrogram import filters
from typing import List, TYPE_CHECKING, Any, Optional, Dict

from ..db import MonitoredRepo
from .. import db_ops

if TYPE_CHECKING:
    from ..main import gitMonitorModule

ITEMS_PER_PAGE = 5

async def send_repo_selection_list(
    message: Message,
    repos: List[MonitoredRepo],
    page: int,
    S: dict
):
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
        module_logger = getattr(getattr(message.chat, '_client', None), 'ext_module_gitMonitorModule', None)
        if module_logger and hasattr(module_logger, 'logger'):
            module_logger.logger.error(f"Error sending/editing repo selection list: {e}")
        else:
            print(f"Error sending/editing repo selection list (logger not found): {e}")


async def send_repo_settings_panel(
    call_or_message: Any,
    repo_entry: MonitoredRepo,
    S: dict,
    current_list_page: int = 0
):
    """Sends or edits a message with the settings panel for a specific repo."""
    text = S["git_settings"]["header"].format(
        owner=repo_entry.owner, repo=repo_entry.repo, repo_id=repo_entry.id
    )

    commit_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_commits else S["git_settings"]["status_disabled"]
    issue_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_issues else S["git_settings"]["status_disabled"]
    tag_status = S["git_settings"]["status_enabled"] if repo_entry.monitor_tags else S["git_settings"]["status_disabled"]
    
    current_branch_display = repo_entry.branch or S["git_settings"]["default_branch_display"]

    buttons = [
        [InlineKeyboardButton(
            S["git_settings"]["commits_monitoring"].format(status=commit_status),
            callback_data=f"gitsettings_toggle_commits_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["branch_btn"].format(branch_name=current_branch_display),
            callback_data=f"gitsettings_setbranch_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["issues_monitoring"].format(status=issue_status),
            callback_data=f"gitsettings_toggle_issues_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(
            S["git_settings"]["tags_monitoring"].format(status=tag_status),
            callback_data=f"gitsettings_toggle_tags_{repo_entry.id}_{current_list_page}"
        )],
        [InlineKeyboardButton(S["git_settings"]["back_to_list_btn"], callback_data=f"gitsettings_list_{current_list_page}")],
        [InlineKeyboardButton(S["git_settings"]["close_btn"], callback_data="gitsettings_close")]
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

async def handle_settings_callback(
    call: CallbackQuery,
    module_instance: 'gitMonitorModule'
):
    """Handles callbacks from the settings UI."""
    S = module_instance.S
    async_session_maker = module_instance.async_session
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    parts = call.data.split("_")
    action_type = parts[1]

    if action_type == "close":
        await call.message.delete()
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
        await send_repo_selection_list(call, repos, page, S)
        await call.answer()
        return

    repo_id: int
    current_list_page: int

    if action_type == "show":
        repo_id = int(parts[2])
        current_list_page = int(parts[3])

        async with async_session_maker() as session:
            repo_entry = await db_ops.get_repo_by_id(session, repo_id)
        if not repo_entry or repo_entry.chat_id != chat_id:
            await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
            return
        await send_repo_settings_panel(call, repo_entry, S, current_list_page)
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
                await send_repo_settings_panel(call, updated_repo_entry, S, current_list_page)
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
        original_message_id = call.message.id

        async with async_session_maker() as session:
            repo_entry_for_prompt = await db_ops.get_repo_by_id(session, repo_id)
        
        if not repo_entry_for_prompt or repo_entry_for_prompt.chat_id != chat_id:
            await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
            return

        current_branch_display = repo_entry_for_prompt.branch or S["git_settings"]["default_branch_display"]
        prompt_text = S["git_settings"]["prompt_branch_name_specific"].format(current_branch=current_branch_display)
        
        cancel_button = InlineKeyboardButton(S["git_settings"]["cancel_btn"], callback_data=f"gitsettings_show_{repo_id}_{current_list_page}")
        await call.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup([[cancel_button]]))
        await call.answer()

        try:
            response_msg: Message = await module_instance._listen(
                chat_id=chat_id,
                user_id=user_id,
                message_filters=filters.text & ~filters.regex(r"^/\S+"),
                timeout=60
            )
            
            try: await response_msg.delete()
            except Exception: pass

            new_branch_name_input = response_msg.text.strip()
            
            if not new_branch_name_input:
                await module_instance.bot.send_message(
                    chat_id, 
                    S["git_settings"]["invalid_branch_name"], 
                    reply_to_message_id=original_message_id if call.message.chat.type != "private" else None)
                await send_repo_settings_panel(call, repo_entry_for_prompt, S, current_list_page)
                return

            new_branch_name: Optional[str]
            if new_branch_name_input.lower() in ["default", "clear", "none", ""]:
                new_branch_name = None
            else:
                new_branch_name = new_branch_name_input

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
                await send_repo_settings_panel(call, updated_repo_entry, S, current_list_page)
                
                branch_confirm_display = new_branch_name or S["git_settings"]["default_branch_display"]
                await module_instance.bot.send_message(
                    chat_id, 
                    S["git_settings"]["branch_updated_ok"].format(branch_name=branch_confirm_display),
                    reply_to_message_id=original_message_id if call.message.chat.type != "private" else None
                )
            else:
                await module_instance.bot.send_message(chat_id, S["git_settings"]["error"])


        except asyncio.TimeoutError:
            async with async_session_maker() as session:
                repo_entry_after_timeout = await db_ops.get_repo_by_id(session, repo_id)
            if repo_entry_after_timeout:
                await send_repo_settings_panel(call, repo_entry_after_timeout, S, current_list_page)
            
            await module_instance.bot.send_message(
                chat_id, 
                S["git_settings"]["branch_set_timeout"],
                reply_to_message_id=original_message_id if call.message.chat.type != "private" else None
            )
        except Exception as e:
            module_instance.logger.error(f"Error in setbranch listen/update flow for repo {repo_id}: {e}", exc_info=True)
            try:
                async with async_session_maker() as session:
                    repo_entry_fallback = await db_ops.get_repo_by_id(session, repo_id)
                if repo_entry_fallback:
                    await send_repo_settings_panel(call, repo_entry_fallback, S, current_list_page)
            except Exception as restore_err:
                module_instance.logger.error(f"Failed to restore panel after setbranch error: {restore_err}")
            
            await module_instance.bot.send_message(chat_id, S["git_settings"]["error"])
        return

    await call.answer("Unknown settings action.", show_alert=True)
