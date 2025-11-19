from aiogram.types import CallbackQuery, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest

async def edit_photo_menu(
    callback: CallbackQuery,
    caption: str,
    keyboard,
    photo=None
):
    bot = callback.message.bot
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id

    # Если картинка остаётся прежней — меняем только caption
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            reply_markup=keyboard
        )
        await callback.answer()
        return
    except TelegramBadRequest:
        pass

    # Если нужно заменить фото (опционально)
    if photo:
        try:
            media = InputMediaPhoto(media=photo, caption=caption)
            await bot.edit_message_media(
                media=media,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard
            )
            await callback.answer()
            return
        except TelegramBadRequest:
            pass

    # Полный fallback
    await callback.message.edit_text(
        caption,
        reply_markup=keyboard
    )
    await callback.answer()
