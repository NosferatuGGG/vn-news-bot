"""Настройки бота. Всё берётся из переменных окружения (файл .env)."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# Пустая переменная (так GitHub передаёт незаданные настройки) = значение по умолчанию
def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "on")


_PROVIDER = _env("LLM_PROVIDER", "gemini").lower()
_KEY_VARS = {"gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


_DEFAULT_MODELS = {
    "gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
    "openrouter": ("anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5"),
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
}


@dataclass
class Config:
    # --- Нейросеть ---
    # gemini (бесплатный тариф) | openrouter | anthropic
    llm_provider: str = _PROVIDER
    llm_api_key: str = _env("LLM_API_KEY") or _env(_KEY_VARS.get(_PROVIDER, ""))
    # пауза между запросами к нейросети, сек (бесплатный Gemini — около 10 запросов в минуту)
    llm_min_interval_sec: float = float(_env("LLM_MIN_INTERVAL_SEC", "7" if _PROVIDER == "gemini" else "0"))

    # --- Telegram ---
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN", "")
    channel_id: str = _env("TELEGRAM_CHANNEL_ID", "")          # @mychannel или -100...
    admin_chat_id: str = _env("TELEGRAM_ADMIN_CHAT_ID", "")    # куда слать черновики на модерацию

    # --- Модели ---
    # быстрая — отбор и проверка фактов, сильная — перевод и рерайт
    model_fast: str = _env("MODEL_FAST", _DEFAULT_MODELS[_PROVIDER][0])
    model_writer: str = _env("MODEL_WRITER", _DEFAULT_MODELS[_PROVIDER][1])

    # --- Режим работы ---
    # moderate — сначала черновик тебе в личку с кнопками; auto — сразу в канал
    mode: str = _env("MODE", "moderate")
    dry_run: bool = _bool("DRY_RUN", False)  # ничего не отправлять, только печатать

    # --- Отбор ---
    min_score: int = _int("MIN_SCORE", 6)              # порог интересности 0–10
    max_age_hours: int = _int("MAX_AGE_HOURS", 36)     # старше — не берём
    max_llm_items_per_scan: int = _int("MAX_LLM_ITEMS_PER_SCAN", 25)
    run_budget_min: int = _int("RUN_BUDGET_MIN", 9)    # сколько минут один запуск может разбирать новости

    # --- Расписание публикаций ---
    scan_interval_min: int = _int("SCAN_INTERVAL_MIN", 20)
    max_posts_per_day: int = _int("MAX_POSTS_PER_DAY", 10)
    min_gap_min: int = _int("MIN_GAP_MIN", 45)          # минимум минут между постами
    quiet_from: int = _int("QUIET_FROM", 23)            # тихие часы по времени Вьетнама
    quiet_to: int = _int("QUIET_TO", 7)
    timezone: str = _env("TZ_NAME", "Asia/Ho_Chi_Minh")

    # --- Подпись канала ---
    channel_signature: str = _env("CHANNEL_SIGNATURE", "")  # например "@nhatrang_news"

    # Курс для пересчёта сумм в постах, текстом. Пусто — суммы останутся в донгах.
    rates: str = _env("RATES", "")

    db_path: str = _env("DB_PATH", "news.db")
    keep_days: int = _int("KEEP_DAYS", 14)   # сколько дней помнить уже увиденные новости
    extra: dict = field(default_factory=dict)


CFG = Config()
