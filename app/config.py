import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COMET_API_TOKEN = os.getenv("COMET_API_TOKEN", "")
TZ = os.getenv("TZ", "Europe/Moscow")
COMET_MODEL = os.getenv("COMET_MODEL", "gpt-5.1")

# Optional hardening/tuning
ALLOWED_CHAT_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
BOT_COOLDOWN_SECONDS = int(os.getenv("BOT_COOLDOWN_SECONDS", "20"))
_HUMOR_MODE_RAW = os.getenv("HUMOR_MODE", "strict").strip().lower()
_HUMOR_MODE_ALIASES = {"hard": "strict", "insane": "brutal"}
HUMOR_MODE = _HUMOR_MODE_ALIASES.get(_HUMOR_MODE_RAW, _HUMOR_MODE_RAW)  # soft|strict|brutal
WEB_DIGEST_HOUR = int(os.getenv("WEB_DIGEST_HOUR", "12"))
WEB_DIGEST_MINUTE = int(os.getenv("WEB_DIGEST_MINUTE", "0"))
