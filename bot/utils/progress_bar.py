def make_progress_bar(percent: float) -> str:
    """Рисует прогресс-бар: ██████░░░░ 60%"""
    filled = int(percent / 10)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty
    return f"Загрузка... {bar} {int(percent)}%"
