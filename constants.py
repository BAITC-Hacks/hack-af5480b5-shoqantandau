"""Все настройки проекта. Секретов здесь нет.

Ключи API кладите в local_constants.py (он в .gitignore) —
см. local_constants.example.py. Без ключей сервис работает полностью,
AI-формулировки обоснований просто отключаются.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Django ---
SECRET_KEY = "dev-only-hackalem-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

# Показывать демо-логины на странице входа (для проверки на хакатоне; в работе — False)
SHOW_DEMO_USERS = True

# --- Данные ---
DATA_DIR = BASE_DIR / "data" / "demo"

# Справочник поставщиков: папка с выгрузками и срок поставки, дн.
# lead_time_days = None — срок выводится из файла «товар в пути» (даты заказа и поступления).
SUPPLIERS = {
    "iek": {"name": "IEK", "dir": DATA_DIR / "iek", "lead_time_days": None},
    "se": {"name": "Systeme Electric", "dir": DATA_DIR / "se", "lead_time_days": 40},
}
DATA_FILES = {
    "sales_transactions": "sales_transactions.xlsx",  # построчные расходные накладные
    "sales_monthly": "sales_monthly.xlsx",            # продажи SKU x месяц
    "stock_monthly": "stock_monthly.xlsx",            # остатки SKU x месяц
    "in_transit": "in_transit.xlsx",                  # товар в пути
    "moq": "moq.xlsx",                                # минимальная партия / кратность
    "seasonality": "seasonality.xlsx",                # продажи в деньгах по месяцам
}

# --- Параметры расчёта (значения по умолчанию, меняются в интерфейсе) ---
DEFAULT_LEAD_TIME_DAYS = 40        # срок поставки, если не удалось вывести из "в пути"
REVIEW_PERIOD_DAYS = 30            # как часто делается заказ
SERVICE_LEVEL_Z = 1.65             # ~95% уровень сервиса для страхового запаса
HISTORY_MONTHS = 12                # окно для базового спроса
OUTLIER_MAD_K = 5.0                # порог выброса: медиана + k * MAD по строкам накладных
OUTLIER_MONTH_SHARE = 0.4          # и строка даёт больше этой доли продаж месяца
MONTH_SPIKE_K = 5.0                # всплеск месяца без накладной: медиана + k * MAD ненулевых месяцев
MONTH_SPIKE_RATIO = 4.0            # и больше этого числа медиан ненулевых месяцев
LONG_STOCKOUT_MONTHS = 6           # товара нет столько месяцев подряд — спрос не восстанавливаем
DISCONTINUED_MARK = "!!!"          # пометка в названии 1С, похожая на «выводится из ассортимента»

# --- LLM (опционально) ---
LLM_PROVIDER = "openai"            # "openai" или "nvidia"
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-5-mini"  # любая доступная вашему ключу chat-модель
NVIDIA_API_KEY = ""
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "meta/llama-3.1-70b-instruct"

try:  # локальные переопределения и ключи, не попадают в git
    from local_constants import *  # noqa: F401,F403
except ImportError:
    pass
