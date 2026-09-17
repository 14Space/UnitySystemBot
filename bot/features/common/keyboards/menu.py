"""
Постоянная Reply-клавиатура с командами (плитки-кнопки снизу экрана).

Это НЕ синяя кнопка «Меню» (та задаётся через set_my_commands). Здесь —
отдельные кнопки, прикреплённые к чату: нажатие отправляет текст команды боту.
is_persistent=True держит её открытой, resize_keyboard ужимает под число кнопок.
"""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from bot.config import ADMIN_ID


def main_menu_keyboard(user_id: int = 0, placeholder: str | None = None) -> ReplyKeyboardMarkup:
    """
    Плитки-кнопки команд снизу. Обычным пользователям — только /help; админу — ещё
    /statistics, /test и /cleancache. (Синее «Меню» шире: всем /start, /help, /premium.)

    is_persistent=True держит клавиатуру доступной по квадратной кнопке у поля ввода,
    даже когда пользователь её свернул. Прикрепляем её ТОЛЬКО к /start и больше нигде
    не переотправляем — иначе она будет раскрываться сама. Так плитки по умолчанию
    скрыты и появляются лишь по нажатию на квадратик.

    placeholder — подсказка в поле ввода, своя под роль бота и язык пользователя
    (передаёт вызывающий; см. cmd_start).
    """
    rows = [
        [KeyboardButton(text="/help")],
    ]
    if ADMIN_ID and user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="/statistics"), KeyboardButton(text="/test"),
                     KeyboardButton(text="/cleancache")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )
