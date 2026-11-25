"""Configuracion central del bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
load_dotenv(ENV_PATH)

DEFAULT_BASE_URL = "https://senati.blackboard.com"
DEFAULT_TIMEZONE = "America/Lima"

# Proveedores SAML de SENATI (Microsoft Entra ID / Azure AD)
PROVIDER_BY_DOMAIN = {
    "senati.pe": "_5702_1",
    "senati.edu.pe": "_1502_1",
}
DEFAULT_PROVIDER = "_5702_1"


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def provider_for_email(email: str, override: str = "") -> str:
    """Elige el proveedor SAML segun el dominio del correo institucional."""
    if override.strip():
        return override.strip()
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return PROVIDER_BY_DOMAIN.get(domain, DEFAULT_PROVIDER)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_chat_id: int | None
    base_url: str
    saml_apid_override: str
    timezone: str
    headless: bool
    browser_channel: str | None
    check_interval_min: int
    data_dir: Path
    env_email: str
    env_password: str

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.enc"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "bot.log"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Falta BOT_TOKEN en .env. Crea un bot con @BotFather y pega el token."
        )

    owner_raw = os.getenv("OWNER_CHAT_ID", "").strip()
    owner_chat_id: int | None = None
    if owner_raw:
        try:
            owner_chat_id = int(owner_raw)
        except ValueError as exc:
            raise RuntimeError("OWNER_CHAT_ID debe ser un numero entero.") from exc

    base_url = os.getenv("BB_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")

    data_raw = os.getenv("BB_DATA_DIR", "").strip()
    if data_raw:
        data_dir = Path(data_raw).expanduser()
    else:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        data_dir = Path(local) / "BlackboardBot" if local else PROJECT_DIR / "data"

    channel = os.getenv("BB_BROWSER_CHANNEL", "chrome").strip().lower()
    if channel in {"", "none", "chromium", "playwright"}:
        channel = None

    interval = _int_env("BB_CHECK_INTERVAL_MIN", 30)
    if interval < 0:
        interval = 0

    settings = Settings(
        bot_token=token,
        owner_chat_id=owner_chat_id,
        base_url=base_url,
        saml_apid_override=os.getenv("BB_SAML_APID", "").strip(),
        timezone=os.getenv("BB_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
        headless=_truthy(os.getenv("BB_HEADLESS"), default=False),
        browser_channel=channel,
        check_interval_min=interval,
        data_dir=data_dir,
        env_email=os.getenv("BB_EMAIL", "").strip(),
        env_password=os.getenv("BB_PASSWORD", ""),
    )
    settings.prepare()
    return settings
