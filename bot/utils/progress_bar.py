import time

from bot.config import PROGRESS_MIN_INTERVAL


def make_progress_bar(percent: float, lang: str = "ru") -> str:
    """Рисует полоску загрузки: ██████░░░░ 60%.

    Подпись — через словарь надписей: слово «Загрузка…» было зашито по-русски, и
    англоязычный человек получал единственную русскую строку во всём общении с ботом.
    """
    from bot.utils.i18n import t

    filled = int(percent / 10)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty
    return f"{t('progress', lang)} {bar} {int(percent)}%"


# Как часто разрешаем ПРАВИТЬ сообщение с полоской, секунд.
MIN_INTERVAL = PROGRESS_MIN_INTERVAL


class ProgressThrottle:
    """Ограничитель частоты обновлений полоски загрузки.

    Зачем. yt-dlp сообщает о прогрессе постоянно, и раньше мы правили сообщение на
    КАЖДЫЙ новый процент — до сотни правок за одну загрузку. Telegram такую частоту
    не приветствует: он отвечает «подожди столько-то секунд», и бот теперь честно
    ждёт (см. middlewares/retry.py) — то есть тратит время на полоску вместо дела.

    Человек разницы не замечает: полоска обновляется раз в пару секунд, как у всех
    нормальных загрузчиков. Ноль и сто процентов пропускаем всегда — это начало и
    конец, их видно должно быть сразу.
    """

    def __init__(self, min_interval: float = MIN_INTERVAL):
        self._min_interval = min_interval
        self._last_percent = -1
        self._last_at = 0.0

    def should_send(self, percent: int) -> bool:
        """Пора ли показывать это значение."""
        percent = int(percent)
        if percent == self._last_percent:
            return False            # то же самое число — правка ничего не изменит
        now = time.monotonic()
        if percent < 100 and self._last_percent >= 0 \
                and now - self._last_at < self._min_interval:
            return False
        self._last_percent = percent
        self._last_at = now
        return True
