"""Настройки сервиса из переменных окружения."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))

# Выборка матчей OpenDota: средний ранг (75 = Divine 5, 80 = Immortal) и окно в днях.
MIN_RANK = int(os.getenv("MIN_RANK", "75"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "8"))
REFRESH_HOURS = float(os.getenv("REFRESH_HOURS", "6"))
OPENDOTA_API_KEY = os.getenv("OPENDOTA_API_KEY", "")

# Распознавание фото экрана через Claude (если ключа нет, работает только разбор скриншота).
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_DAILY_LIMIT = int(os.getenv("CLAUDE_DAILY_LIMIT", "200"))
CLAUDE_PER_IP_HOURLY = int(os.getenv("CLAUDE_PER_IP_HOURLY", "10"))

# Сколько прокси дописывают адрес в X-Forwarded-For (на Render один). 0 — заголовок не учитывать.
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1" if os.getenv("RENDER") else "0"))
# Разбор скриншотов грузит процессор: общий и личный лимиты.
RECOGNIZE_PER_IP_MINUTE = int(os.getenv("RECOGNIZE_PER_IP_MINUTE", "12"))
RECOGNIZE_CONCURRENCY = int(os.getenv("RECOGNIZE_CONCURRENCY", "2"))
MAX_IMAGE_PIXELS = 40_000_000  # 8K-скриншот — около 33 Мп

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
