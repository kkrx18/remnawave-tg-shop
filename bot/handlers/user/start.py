import logging
import re
from aiogram import Router, F, types, Bot
from aiogram.utils.text_decorations import html_decoration as hd
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from typing import Optional, Union
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

import os

from db.dal import user_dal
from db.models import User

from bot.keyboards.inline.user_keyboards import (
    get_main_menu_inline_keyboard,
    get_language_selection_keyboard,
    get_channel_subscription_keyboard,
    get_back_to_main_menu_markup,
)
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.services.referral_service import ReferralService
from bot.services.promo_code_service import PromoCodeService
from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.utils.text_sanitizer import sanitize_username, sanitize_display_name
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
    FSInputFile,
    InputMediaPhoto,
)

router = Router(name="user_start_router")

async def reply_or_edit(
    callback: types.CallbackQuery,
    text: str,
    *,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
    as_caption: bool = True,
    parse_mode: Optional[str] = None,
    allow_web_page_preview: Optional[bool] = None,
) -> Optional[types.Message]:
    """
    Helper to update an existing callback message gracefully.

    - If `as_caption` is True and the current message contains media (photo), edit the caption.
    - Otherwise try to edit the text.
    - If editing fails (message not modified or markup change not allowed), send a new message and delete the old one.

    Returns the resulting message object or None.
    """
    if not callback or not callback.message:
        # Lost message context, send a new message
        return await callback.bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)

    msg = callback.message

    # Attempt to edit caption if media and requested as_caption
    if as_caption and getattr(msg, "photo", None):
        try:
            await msg.edit_caption(caption=text, reply_markup=reply_markup)
            return msg
        except Exception as e:
            logging.debug("edit_caption failed: %s", e)

    # Attempt to edit text
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=not allow_web_page_preview)
        return msg
    except Exception as e:
        logging.debug("edit_text failed: %s", e)

    # Fallback: send new message and delete old
    try:
        new_msg = await callback.bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        try:
            await msg.delete()
        except Exception as e_del:
            logging.debug("Could not delete old message after fallback send: %s", e_del)
        return new_msg
    except Exception as e_send:
        logging.exception("Failed to send fallback message for user %s: %s", callback.from_user.id, e_send)
        return None


async def send_main_menu(target_event: Union[types.Message, types.CallbackQuery],
                         settings: Settings,
                         i18n_data: dict,
                         subscription_service: SubscriptionService,
                         session: AsyncSession,
                         is_edit: bool = False) -> None:
    """
    Send or edit the main menu as a photo with inline buttons.

    When editing (is_edit=True), attempt to edit the caption of the existing photo rather than sending a new message.
    Falls back to sending a new photo or text if editing fails or the photo file is missing.
    """
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    # If i18n missing, fallback to simple text
    if not i18n:
        logging.error("i18n_instance missing in send_main_menu")
        fallback = "Menu"
        if isinstance(target_event, types.CallbackQuery):
            if target_event.message:
                await target_event.message.answer(fallback)
            await target_event.answer()
        else:
            await target_event.answer(fallback)
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    user = target_event.from_user
    user_full_name = hd.quote(user.full_name)
    caption_text = _(key="main_menu_greeting", user_name=user_full_name)

    # Determine if trial button should be shown
    show_trial_button_in_menu = False
    try:
        if settings.TRIAL_ENABLED and hasattr(subscription_service, "has_had_any_subscription"):
            if not await subscription_service.has_had_any_subscription(session, user.id):
                show_trial_button_in_menu = True
    except Exception:
        logging.exception("Error checking trial eligibility for user %s", user.id)

    reply_markup = get_main_menu_inline_keyboard(current_lang, i18n, settings, show_trial_button_in_menu)

    # Path to the main menu image
    MAIN_MENU_IMAGE_PATH = "bot/static/mainmenu.png"

    # Determine the message object
    message_obj: Optional[types.Message] = None
    if isinstance(target_event, types.CallbackQuery):
        message_obj = target_event.message
    elif isinstance(target_event, types.Message):
        message_obj = target_event

    # Check file exists
    has_image = os.path.isfile(MAIN_MENU_IMAGE_PATH) and os.access(MAIN_MENU_IMAGE_PATH, os.R_OK)

    try:
        if has_image:
            fsfile = FSInputFile(MAIN_MENU_IMAGE_PATH)
            # Editing existing message
            if is_edit and message_obj:
                try:
                    # If current message has photo, edit caption
                    if getattr(message_obj, "photo", None):
                        await message_obj.edit_caption(caption=caption_text, reply_markup=reply_markup)
                    else:
                        # Replace media entirely if not currently a photo
                        media = InputMediaPhoto(media=fsfile, caption=caption_text)
                        await message_obj.edit_media(media=media, reply_markup=reply_markup)
                except Exception as e_edit:
                    logging.warning(
                        "Failed to edit existing main menu (user %s): %s. Falling back to send new photo.",
                        user.id,
                        e_edit,
                    )
                    # Send new photo
                    try:
                        if isinstance(target_event, types.CallbackQuery) and target_event.message:
                            await target_event.message.answer_photo(fsfile, caption=caption_text, reply_markup=reply_markup)
                        elif isinstance(target_event, types.Message):
                            await target_event.answer_photo(fsfile, caption=caption_text, reply_markup=reply_markup)
                        else:
                            bot_inst = getattr(target_event, "bot", None)
                            if bot_inst:
                                await bot_inst.send_photo(chat_id=user.id, photo=fsfile, caption=caption_text, reply_markup=reply_markup)
                    except Exception as e_send:
                        logging.error("Fallback send photo failed for user %s: %s", user.id, e_send)
            else:
                # New message with photo
                if isinstance(target_event, types.CallbackQuery) and target_event.message:
                    try:
                        await target_event.message.answer_photo(fsfile, caption=caption_text, reply_markup=reply_markup)
                    except Exception as e:
                        logging.warning("answer_photo in message context failed: %s. Trying direct send.", e)
                        try:
                            await target_event.bot.send_photo(chat_id=user.id, photo=fsfile, caption=caption_text, reply_markup=reply_markup)
                        except Exception as e2:
                            logging.error("Direct send_photo failed for user %s: %s", user.id, e2)
                elif isinstance(target_event, types.Message):
                    try:
                        await target_event.answer_photo(fsfile, caption=caption_text, reply_markup=reply_markup)
                    except Exception as e:
                        logging.error("answer_photo failed for user %s: %s", user.id, e)
                else:
                    bot_inst = getattr(target_event, "bot", None)
                    if bot_inst:
                        try:
                            await bot_inst.send_photo(chat_id=user.id, photo=fsfile, caption=caption_text, reply_markup=reply_markup)
                        except Exception as e:
                            logging.error("Fallback send_photo failed for user %s: %s", user.id, e)
        else:
            # If image missing, fallback to text
            logging.error("Main menu image missing or unreadable at %s", MAIN_MENU_IMAGE_PATH)
            if message_obj:
                try:
                    await message_obj.edit_text(caption_text, reply_markup=reply_markup)
                except Exception:
                    try:
                        await message_obj.answer(caption_text, reply_markup=reply_markup)
                    except Exception as e:
                        logging.error("Fallback text send failed for user %s: %s", user.id, e)
            else:
                if isinstance(target_event, types.CallbackQuery):
                    await target_event.answer(caption_text, show_alert=False)
                else:
                    await target_event.answer(caption_text, reply_markup=reply_markup)
    except Exception as e:
        logging.exception("Unhandled error in send_main_menu for user %s: %s", getattr(user, "id", "unknown"), e)
        # Final fallback — send text
        try:
            if message_obj:
                await message_obj.answer(caption_text, reply_markup=reply_markup)
            else:
                if isinstance(target_event, types.CallbackQuery):
                    await target_event.answer(caption_text, show_alert=False)
                else:
                    await target_event.answer(caption_text, reply_markup=reply_markup)
        except Exception:
            logging.exception("Also failed to send fallback main menu text.")

    # Answer callback to stop loading state
    if isinstance(target_event, types.CallbackQuery):
        try:
            await target_event.answer()
        except Exception:
            pass


async def ensure_required_channel_subscription(
        event: Union[types.Message, types.CallbackQuery],
        settings: Settings,
        i18n: Optional[JsonI18n],
        current_lang: str,
        session: AsyncSession,
        db_user: Optional[User] = None) -> bool:
    """
    Verify that the user is a member of the required channel (if configured).
    Returns True when access can proceed, False when user must subscribe first.
    """
    required_channel_id = settings.REQUIRED_CHANNEL_ID
    if not required_channel_id:
        return True

    if isinstance(event, types.CallbackQuery):
        user_id = event.from_user.id
        bot_instance: Optional[Bot] = getattr(event, "bot", None)
        if bot_instance is None and event.message:
            bot_instance = event.message.bot
        message_obj: Optional[types.Message] = event.message
    else:
        user_id = event.from_user.id
        bot_instance = event.bot if hasattr(event, "bot") else None
        message_obj = event

    if bot_instance is None:
        logging.error(
            "Channel subscription check: bot instance missing for user %s.", user_id
        )
        return False

    if user_id in settings.ADMIN_IDS:
        return True

    if db_user is None:
        try:
            db_user = await user_dal.get_user_by_id(session, user_id)
        except Exception as fetch_error:
            logging.error(
                "Channel subscription check: failed to fetch user %s: %s",
                user_id,
                fetch_error,
                exc_info=True,
            )
            return False

    if not db_user:
        logging.warning(
            "Required channel check skipped because user %s is not persisted yet.",
            user_id,
        )
        return True

    if (db_user.channel_subscription_verified
            and db_user.channel_subscription_verified_for
            == required_channel_id):
        return True

    def translate(key: str, **kwargs) -> str:
        if i18n:
            return i18n.gettext(current_lang, key, **kwargs)
        return key

    now = datetime.now(timezone.utc)
    is_member = False
    status_value = None

    try:
        member = await bot_instance.get_chat_member(required_channel_id, user_id)
        status = getattr(member, "status", None)
        status_value = getattr(status, "value", status)
        allowed_statuses = {"creator", "administrator", "member", "restricted"}
        if status_value in allowed_statuses:
            is_member = True
    except TelegramBadRequest as bad_request:
        logging.info(
            "Required channel check: user %s not subscribed (details: %s)",
            user_id,
            bad_request,
        )
    except TelegramForbiddenError as forbidden_error:
        logging.error(
            "Required channel check failed due to insufficient permissions: %s",
            forbidden_error,
        )
        error_text = translate("channel_subscription_check_failed")
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(error_text, show_alert=True)
            except Exception:
                pass
            if message_obj:
                try:
                    await message_obj.answer(error_text)
                except Exception:
                    pass
        else:
            await event.answer(error_text)
        return False
    except TelegramAPIError as api_error:
        logging.error(
            "Required channel check failed for user %s: %s",
            user_id,
            api_error,
            exc_info=True,
        )
        error_text = translate("channel_subscription_check_failed")
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(error_text, show_alert=True)
            except Exception:
                pass
            if message_obj:
                try:
                    await message_obj.answer(error_text)
                except Exception:
                    pass
        else:
            await event.answer(error_text)
        return False

    update_payload = {
        "channel_subscription_checked_at": now,
        "channel_subscription_verified_for": required_channel_id,
        "channel_subscription_verified": is_member,
    }
    try:
        await user_dal.update_user(session, user_id, update_payload)
    except Exception as update_error:
        logging.error(
            "Failed to persist channel verification result for user %s: %s",
            user_id,
            update_error,
            exc_info=True,
        )

    if is_member:
        logging.info(
            "User %s confirmed as member of required channel %s (status=%s).",
            user_id,
            required_channel_id,
            status_value,
        )
        return True

    keyboard = (get_channel_subscription_keyboard(
        current_lang, i18n, settings.REQUIRED_CHANNEL_LINK
    )
               if i18n else None)

    prompt_text = translate("channel_subscription_required")

    if isinstance(event, types.CallbackQuery):
        if keyboard and event.message:
            try:
                await event.message.edit_text(prompt_text, reply_markup=keyboard)
            except Exception as edit_error:
                logging.debug(
                    "Failed to edit prompt message for user %s: %s",
                    user_id,
                    edit_error,
                )
        if keyboard is None and message_obj:
            try:
                await message_obj.answer(prompt_text)
            except Exception:
                pass
        try:
            await event.answer(prompt_text, show_alert=True)
        except Exception:
            pass
    else:
        await event.answer(prompt_text, reply_markup=keyboard)

    return False


@router.message(CommandStart())
@router.message(CommandStart(magic=F.args.regexp(r"^ref_((?:[uU][A-Za-z0-9]{9})|(?:[A-Za-z0-9]{9})|\d+)$").as_("ref_match")))
@router.message(CommandStart(magic=F.args.regexp(r"^promo_(\w+)$").as_("promo_match")))
@router.message(CommandStart(magic=F.args.regexp(r"^(?!ref_|promo_)([A-Za-z0-9_\-]{2,64})$").as_("ad_param_match")))
async def start_command_handler(message: types.Message,
                                state: FSMContext,
                                settings: Settings,
                                i18n_data: dict,
                                subscription_service: SubscriptionService,
                                session: AsyncSession,
                                ref_match: Optional[re.Match] = None,
                                promo_match: Optional[re.Match] = None,
                                ad_param_match: Optional[re.Match] = None):
    await state.clear()
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs
                                           ) if i18n else key

    user = message.from_user
    user_id = user.id

    referred_by_user_id: Optional[int] = None
    promo_code_to_apply: Optional[str] = None
    ad_start_param: Optional[str] = None

    if ref_match:
        raw_ref_value = ref_match.group(1)
        if raw_ref_value.isdigit():
            if settings.LEGACY_REFS:
                potential_referrer_id = int(raw_ref_value)
                if potential_referrer_id != user_id and await user_dal.get_user_by_id(
                        session, potential_referrer_id):
                    referred_by_user_id = potential_referrer_id
        else:
            normalized_code = raw_ref_value.strip()
            if normalized_code and normalized_code[0].lower() == "u":
                normalized_code = normalized_code[1:]
            ref_user = None
            if normalized_code:
                ref_user = await user_dal.get_user_by_referral_code(
                    session, normalized_code)
            if ref_user and ref_user.user_id != user_id:
                referred_by_user_id = ref_user.user_id
    elif promo_match:
        promo_code_to_apply = promo_match.group(1)
        logging.info(f"User {user_id} started with promo code: {promo_code_to_apply}")
    elif ad_param_match:
        ad_start_param = ad_param_match.group(1)
        logging.info(f"User {user_id} started with ad start param: {ad_start_param}")

    sanitized_username = sanitize_username(user.username)
    sanitized_first_name = sanitize_display_name(user.first_name)
    sanitized_last_name = sanitize_display_name(user.last_name)

    db_user = await user_dal.get_user_by_id(session, user_id)
    if not db_user:
        user_data_to_create = {
            "user_id": user_id,
            "username": sanitized_username,
            "first_name": sanitized_first_name,
            "last_name": sanitized_last_name,
            "language_code": current_lang,
            "referred_by_id": referred_by_user_id,
            "registration_date": datetime.now(timezone.utc)
        }
        try:
            db_user, created = await user_dal.create_user(session, user_data_to_create)

            if created:
                try:
                    await session.commit()
                except Exception as commit_error:
                    await session.rollback()
                    logging.error(
                        f"Failed to commit new user {user_id}: {commit_error}",
                        exc_info=True,
                    )
                    await message.answer(_("error_occurred_processing_request"))
                    return

                logging.info(
                    f"New user {user_id} added to session. Referred by: {referred_by_user_id or 'N/A'}."
                )

                # Send notification about new user registration
                try:
                    from bot.services.notification_service import NotificationService
                    notification_service = NotificationService(message.bot, settings, i18n)
                    await notification_service.notify_new_user_registration(
                        user_id=user_id,
                        username=sanitized_username,
                        first_name=sanitized_first_name,
                        referred_by_id=referred_by_user_id
                    )
                except Exception as e:
                    logging.error(f"Failed to send new user notification: {e}")
        except Exception as e_create:

            logging.error(
                f"Failed to add new user {user_id} to session: {e_create}",
                exc_info=True)
            await message.answer(_("error_occurred_processing_request"))
            return
    else:
        update_payload = {}
        if db_user.language_code != current_lang:
            update_payload["language_code"] = current_lang
        # Set referral only if not already set AND user is not currently active.
        # This allows previously subscribed but currently inactive users to be attributed.
        if referred_by_user_id and db_user.referred_by_id is None:
            try:
                is_active_now = await subscription_service.has_active_subscription(session, user_id)
            except Exception:
                is_active_now = False
            if not is_active_now:
                update_payload["referred_by_id"] = referred_by_user_id
        if sanitized_username != db_user.username:
            update_payload["username"] = sanitized_username
        if sanitized_first_name != db_user.first_name:
            update_payload["first_name"] = sanitized_first_name
        if sanitized_last_name != db_user.last_name:
            update_payload["last_name"] = sanitized_last_name

        if update_payload:
            try:
                await user_dal.update_user(session, user_id, update_payload)

                logging.info(
                    f"Updated existing user {user_id} in session: {update_payload}"
                )
            except Exception as e_update:

                logging.error(
                    f"Failed to update existing user {user_id} in session: {e_update}",
                    exc_info=True)

    # Attribute user to ad campaign if start param provided
    if ad_start_param:
        try:
            from db.dal import ad_dal as _ad_dal
            campaign = await _ad_dal.get_campaign_by_start_param(session, ad_start_param)
            if campaign and campaign.is_active:
                await _ad_dal.ensure_attribution(session, user_id=user_id, campaign_id=campaign.ad_campaign_id)
                await session.commit()
        except Exception as e_attr:
            logging.error(f"Failed to attribute user {user_id} to ad '{ad_start_param}': {e_attr}")
            try:
                await session.rollback()
            except Exception:
                pass

    if not await ensure_required_channel_subscription(message, settings, i18n,
                                                      current_lang, session,
                                                      db_user):
        return

    # Send welcome message if not disabled
    if not settings.DISABLE_WELCOME_MESSAGE:
        await message.answer(_(key="welcome", user_name=hd.quote(user.full_name)))

    # Auto-apply promo code if provided via start parameter
    if promo_code_to_apply:
        try:
            from bot.services.promo_code_service import PromoCodeService
            promo_code_service = PromoCodeService(settings, subscription_service, message.bot, i18n)

            success, result = await promo_code_service.apply_promo_code(
                session, user_id, promo_code_to_apply, current_lang
            )

            if success:
                await session.commit()
                logging.info(f"Auto-applied promo code '{promo_code_to_apply}' for user {user_id}")

                # Get updated subscription details
                active = await subscription_service.get_active_subscription_details(session, user_id)
                config_link = active.get("config_link") if active else None
                config_link = config_link or _("config_link_not_available")

                new_end_date = result if isinstance(result, datetime) else None

                promo_success_text = _(
                    "promo_code_applied_success_full",
                    end_date=(new_end_date.strftime("%d.%m.%Y %H:%M:%S") if new_end_date else "N/A"),
                    config_link=config_link,
                )

                from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
                await message.answer(
                    promo_success_text,
                    reply_markup=get_connect_and_main_keyboard(current_lang, i18n, settings, config_link),
                    parse_mode="HTML"
                )

                # Don't show main menu if promo was successfully applied
                return
            else:
                await session.rollback()
                logging.warning(f"Failed to auto-apply promo code '{promo_code_to_apply}' for user {user_id}: {result}")
                # Continue to show main menu if promo failed

        except Exception as e:
            logging.error(f"Error auto-applying promo code '{promo_code_to_apply}' for user {user_id}: {e}")
            await session.rollback()

    await send_main_menu(message,
                         settings,
                         i18n_data,
                         subscription_service,
                         session,
                         is_edit=False)


@router.callback_query(F.data == "channel_subscription:verify")
async def verify_channel_subscription_callback(
        callback: types.CallbackQuery,
        settings: Settings,
        i18n_data: dict,
        subscription_service: SubscriptionService,
        session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    db_user = await user_dal.get_user_by_id(session, callback.from_user.id)

    verified = await ensure_required_channel_subscription(
        callback, settings, i18n, current_lang, session, db_user)
    if not verified:
        return

    if db_user and db_user.language_code:
        current_lang = db_user.language_code
        i18n_data["current_language"] = current_lang

    if i18n:
        _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    else:
        _ = lambda key, **kwargs: key

    if not settings.DISABLE_WELCOME_MESSAGE:
        welcome_text = _(key="welcome",
                         user_name=hd.quote(callback.from_user.full_name))
        if callback.message:
            await callback.message.answer(welcome_text)
        else:
            fallback_bot: Optional[Bot] = getattr(callback, "bot", None)
            if fallback_bot:
                await fallback_bot.send_message(callback.from_user.id,
                                                welcome_text)

    try:
        await callback.answer(_(key="channel_subscription_verified_success"),
                              show_alert=True)
    except Exception:
        pass

    await send_main_menu(callback,
                         settings,
                         i18n_data,
                         subscription_service,
                         session,
                         is_edit=bool(callback.message))


@router.callback_query(F.data == "main_action:about_us")
async def about_us_callback_handler(callback: types.CallbackQuery, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # Текст с гиперссылками
    text = _(
        "about_us_text",
        default="""
            С использованием нашего VPN-сервиса вы соглашаетесь с нашими 
            <a href="https://cond.kaivpn.ru/agreement.html">Пользовательским соглашением</a>, 
            <a href="https://cond.kaivpn.ru/privacy-policy.html">Политикой конфиденциальности</a> 
            и <a href="https://cond.kaivpn.ru/use-policy.html">Политикой использования</a>.
        """
    )

    # Кнопка "Назад"
    back_button = InlineKeyboardButton(
        text=_("back_to_main_menu_button"),  # Кнопка "Назад"
        callback_data="main_action:back_to_main"
    )
    back_markup = InlineKeyboardMarkup(inline_keyboard=[[back_button]])

    # Try to edit caption if the current message contains a photo; otherwise edit text
    try:
        msg = callback.message
        if getattr(msg, "photo", None):
            await msg.edit_caption(text, reply_markup=back_markup, parse_mode="HTML")
        else:
            await msg.edit_text(text, reply_markup=back_markup, parse_mode="HTML")
    except Exception:
        # Fallback: send a new message
        await callback.message.answer(text, reply_markup=back_markup, parse_mode="HTML")
    # Answer callback to stop spinner
    try:
        await callback.answer()
    except Exception:
        pass



@router.message(Command("language"))
@router.callback_query(F.data == "main_action:language")
async def language_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs
                                           ) if i18n else key

    text_to_send = _(key="choose_language")
    reply_markup = get_language_selection_keyboard(i18n, current_lang)

    target_message_obj = event.message if isinstance(
        event, types.CallbackQuery) else event
    if not target_message_obj:
        if isinstance(event, types.CallbackQuery):
            await event.answer(_("error_occurred_try_again"), show_alert=True)
        return

    if isinstance(event, types.CallbackQuery):
        if event.message:
            try:
                msg = event.message
                # Edit caption if message has photo, else edit text
                if getattr(msg, "photo", None):
                    await msg.edit_caption(text_to_send, reply_markup=reply_markup)
                else:
                    await msg.edit_text(text_to_send, reply_markup=reply_markup)
            except Exception:
                await target_message_obj.answer(text_to_send, reply_markup=reply_markup)
        try:
            await event.answer()
        except Exception:
            pass
    else:
        await target_message_obj.answer(text_to_send, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("set_lang_"))
async def select_language_callback_handler(
        callback: types.CallbackQuery, i18n_data: dict, settings: Settings,
        subscription_service: SubscriptionService, session: AsyncSession):
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Service error or message context lost.",
                              show_alert=True)
        return

    try:
        lang_code = callback.data.split("_")[2]
    except IndexError:
        await callback.answer("Error processing language selection.",
                              show_alert=True)
        return

    user_id = callback.from_user.id
    try:
        updated = await user_dal.update_user_language(session, user_id,
                                                      lang_code)
        if updated:

            i18n_data["current_language"] = lang_code
            _ = lambda key, **kwargs: i18n.gettext(lang_code, key, **kwargs)
            await callback.answer(_(key="language_set_alert"))
            logging.info(
                f"User {user_id} language updated to {lang_code} in session.")
        else:
            await callback.answer("Could not set language.", show_alert=True)
            return
    except Exception as e_lang_update:

        logging.error(
            f"Error updating lang for user {user_id}: {e_lang_update}",
            exc_info=True)
        await callback.answer("Error setting language.", show_alert=True)
        return
    await send_main_menu(callback,
                         settings,
                         i18n_data,
                         subscription_service,
                         session,
                         is_edit=True)
    
    


@router.callback_query(F.data == "main_action:back_to_main")
async def back_to_main_handler(callback: types.CallbackQuery, i18n_data: dict, settings: Settings):
    # Логика для возврата в основное меню
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # Создаем клавиатуру для основного меню
    reply_markup = get_main_menu_inline_keyboard(current_lang, i18n, settings)
    # Текст приветствия
    greeting_text = _(key="main_menu_greeting", user_name=callback.from_user.full_name)
    try:
        msg = callback.message
        # Если текущее сообщение содержит фото, редактируем подпись, иначе текст
        if getattr(msg, "photo", None):
            await msg.edit_caption(greeting_text, reply_markup=reply_markup)
        else:
            await msg.edit_text(greeting_text, reply_markup=reply_markup)
    except Exception:
        # Фоллбек: отправить новый текст
        await callback.message.answer(greeting_text, reply_markup=reply_markup)
    # Ответ на запрос
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("main_action:"))
async def main_action_callback_handler(
        callback: types.CallbackQuery, state: FSMContext, settings: Settings,
        i18n_data: dict, bot: Bot, subscription_service: SubscriptionService,
        referral_service: ReferralService, panel_service: PanelApiService,
        promo_code_service: PromoCodeService, session: AsyncSession):
    action = callback.data.split(":")[1]
    user_id = callback.from_user.id

    from . import subscription as user_subscription_handlers
    from . import referral as user_referral_handlers
    from . import promo_user as user_promo_handlers
    from . import trial_handler as user_trial_handlers

    if not callback.message:
        await callback.answer("Error: message context lost.", show_alert=True)
        return

    if action == "subscribe":
        await user_subscription_handlers.display_subscription_options(
            callback, i18n_data, settings, session)
    elif action == "my_subscription":
        await user_subscription_handlers.my_subscription_command_handler(
            callback, i18n_data, settings, panel_service, subscription_service,
            session, bot)
    elif action == "my_devices":
        await user_subscription_handlers.my_devices_command_handler(
            callback, i18n_data, settings, panel_service, subscription_service,
            session, bot)
    elif action == "referral":
        await user_referral_handlers.referral_command_handler(
            callback, settings, i18n_data, referral_service, bot, session)
    elif action == "apply_promo":
        await user_promo_handlers.prompt_promo_code_input(
            callback, state, i18n_data, settings, session)
    elif action == "request_trial":
        await user_trial_handlers.request_trial_confirmation_handler(
            callback, settings, i18n_data, subscription_service, session)
    elif action == "language":

        await language_command_handler(callback, i18n_data, settings)
    elif action == "back_to_main":
        await send_main_menu(callback,
                             settings,
                             i18n_data,
                             subscription_service,
                             session,
                             is_edit=True)
    elif action == "back_to_main_keep":
        await send_main_menu(callback,
                             settings,
                             i18n_data,
                             subscription_service,
                             session,
                             is_edit=False)
    else:
        i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
        _ = lambda key, **kwargs: i18n.gettext(
            i18n_data.get("current_language"), key, **kw) if i18n else key
        await callback.answer(_("main_menu_unknown_action"), show_alert=True)
