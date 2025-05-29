import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from typing import List, TYPE_CHECKING, Any, Optional, Dict
from html import escape

from ..api.github_api import GitHubAPIClient, APIError
from .. import db_ops
from .processor import send_branch_selection_list, send_repo_selection_list, send_repo_settings_panel, ITEMS_PER_PAGE

if TYPE_CHECKING:
    from ..main import gitMonitorModule

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

    if action_type == "addprompt":
        await call.answer(S["git_settings"]["add_new_repo_prompt"], show_alert=True)
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

    if action_type == "confirmremove":
        repo_id_to_confirm = int(parts[2])
        current_list_page_for_confirm = int(parts[3])

        async with async_session_maker() as session:
            repo_to_confirm = await db_ops.get_repo_by_id(session, repo_id_to_confirm)

        if not repo_to_confirm or repo_to_confirm.chat_id != chat_id:
            await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
            return

        confirm_text = S["git_settings"]["confirm_remove_repo_text"].format(
            owner=escape(repo_to_confirm.owner), repo=escape(repo_to_confirm.repo)
        )
        confirm_buttons = [
            [
                InlineKeyboardButton(
                    S["git_settings"]["confirm_remove_yes_btn"],
                    callback_data=f"gitsettings_doremove_{repo_id_to_confirm}_{current_list_page_for_confirm}"
                ),
                InlineKeyboardButton(
                    S["git_settings"]["cancel_btn"], 
                    callback_data=f"gitsettings_show_{repo_id_to_confirm}_{current_list_page_for_confirm}"
                )
            ]
        ]
        await call.edit_message_text(confirm_text, reply_markup=InlineKeyboardMarkup(confirm_buttons))
        await call.answer()
        return

    if action_type == "doremove":
        repo_id_to_remove = int(parts[2])
        current_list_page_after_remove = int(parts[3])

        repo_owner_removed = "N/A"
        repo_name_removed = "N/A"

        try:
            async with async_session_maker() as session:
                repo_entry_before_delete = await db_ops.get_repo_by_id(session, repo_id_to_remove)
                if repo_entry_before_delete and repo_entry_before_delete.chat_id == chat_id:
                    repo_owner_removed = repo_entry_before_delete.owner
                    repo_name_removed = repo_entry_before_delete.repo
                else: 
                    await call.answer(S["git_settings"]["repo_not_found_generic"], show_alert=True)
                    repos_for_list = await db_ops.get_repos_for_chat(session, chat_id)
                    if not repos_for_list: await call.edit_message_text(S["list_repos"]["none"]); await call.answer(); return
                    await send_repo_selection_list(call, repos_for_list, current_list_page_after_remove, S, module_instance)
                    return

            await module_instance._remove_repo_from_db_and_task(chat_id, repo_id_to_remove)

            await call.answer(
                S["git_settings"]["repo_removed_success"].format(owner=escape(repo_owner_removed), repo=escape(repo_name_removed)),
                show_alert=False 
            )

            async with async_session_maker() as session:
                 repos_after_removal = await db_ops.get_repos_for_chat(session, chat_id)
            if not repos_after_removal:
                await call.edit_message_text(S["list_repos"]["none"])
            else:
                total_pages_after_removal = (len(repos_after_removal) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
                page_to_show = min(current_list_page_after_remove, max(0, total_pages_after_removal -1))
                await send_repo_selection_list(call, repos_after_removal, page_to_show, S, module_instance)
        except Exception as e:
            module_instance.logger.error(f"Error in doremove for repo {repo_id_to_remove}: {e}", exc_info=True)
            await call.answer(S["git_settings"]["error"], show_alert=True)
            async with async_session_maker() as session:
                repo_entry_fallback = await db_ops.get_repo_by_id(session, repo_id_to_remove)
            if repo_entry_fallback and repo_entry_fallback.chat_id == chat_id:
                await send_repo_settings_panel(call, repo_entry_fallback, S, current_list_page_after_remove, module_instance)
            else:
                async with async_session_maker() as session:
                    repos_fallback_list = await db_ops.get_repos_for_chat(session, chat_id)
                if not repos_fallback_list: await call.edit_message_text(S["list_repos"]["none"])
                else: await send_repo_selection_list(call, repos_fallback_list, current_list_page_after_remove, S, module_instance)
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
