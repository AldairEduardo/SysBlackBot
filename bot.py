"""Bot privado de Telegram para ver tareas y eventos de Blackboard (SENATI)."""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import crypto_store
from bb_client import (
    BlackboardError,
    BlackboardSession,
    CalendarItem,
    Course,
    LoginError,
    MFARequired,
    SessionExpired,
    iso_z,
    login_with_microsoft,
)
from config import Settings, get_settings, provider_for_email
from formatting import (
    esc,
    format_dt_short,
    local_day_bounds,
    relative_due,
    render_courses,
    render_event_list,
    render_task_list,
)
from storage import Storage

log = logging.getLogger("blackboard_bot")

EMAIL, PASSWORD, SAVE = range(3)

HELP_TEXT = """📋 <b>Comandos</b>

<b>Tareas</b>
/tareas — próximas 14 días
/tareas 30 — próximos 30 días (máx. 112)
/hoy — lo que vence hoy
/semana — próximos 7 días
/mes — próximos 31 días
/eventos — clases y eventos del calendario

<b>Cuenta</b>
/login — conectar o renovar tu cuenta
/estado — sesión, credenciales y revisión automática
/refrescar — forzar actualización
/logout — borrar credenciales y sesión
/cursos — cursos en los que estás inscrito
/ayuda — este menú

<b>Seguridad</b>
• La contraseña solo se usa para entrar a Microsoft/Blackboard.
• Si eliges guardarla, queda cifrada y atada a tu usuario de Windows.
• /logout elimina la contraseña y la sesión guardadas."""


# --------------------------------------------------------------------------
#  Utilidades
# --------------------------------------------------------------------------


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _store(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.bot_data["store"]


def _bb(context: ContextTypes.DEFAULT_TYPE) -> BlackboardSession:
    return context.bot_data["bb"]


def _owner(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    return _settings(context).owner_chat_id or _store(context).owner_id()


def _redact(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def mask_email(email: str | None) -> str:
    if not email:
        return "—"
    local, _, domain = email.partition("@")
    if not domain:
        return local[:3] + "***"
    return f"{local[:3]}***@{domain}"


async def _edit_safe(message, text: str, **kwargs) -> bool:
    try:
        await message.edit_text(text, **kwargs)
        return True
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return True
        log.debug("No se pudo editar el mensaje: %s", exc)
        return False
    except TelegramError as exc:
        log.debug("No se pudo editar el mensaje: %s", exc)
        return False


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Devuelve True si el update debe ignorarse."""
    chat = update.effective_chat
    if chat is None:
        return True
    if chat.type != "private":
        await update.effective_message.reply_text(
            "🔒 Este bot solo funciona en un chat privado."
        )
        return True
    owner = _owner(context)
    if owner is None:
        await update.effective_message.reply_text(
            "Primero usa /start para activar el bot."
        )
        return True
    if chat.id != owner:
        await update.effective_message.reply_text("🔒 Este bot es privado.")
        return True
    return False


async def _send_chunks(context, chat_id: int, chunks: list[str], status=None) -> None:
    if not chunks:
        return
    kwargs = {"parse_mode": ParseMode.HTML, "disable_web_page_preview": True}
    if status is not None:
        if not await _edit_safe(status, chunks[0], **kwargs):
            await context.bot.send_message(chat_id, chunks[0], **kwargs)
    else:
        await context.bot.send_message(chat_id, chunks[0], **kwargs)
    for chunk in chunks[1:]:
        await context.bot.send_message(chat_id, chunk, **kwargs)


def _progress_for(message):
    async def progress(text: str) -> None:
        await _edit_safe(message, text, parse_mode=ParseMode.HTML)

    return progress


async def _get_courses(
    context: ContextTypes.DEFAULT_TYPE, force: bool = False
) -> list[Course]:
    cache = context.bot_data.get("courses_cache")
    now = time.monotonic()
    if not force and cache and now - cache["at"] < 600:
        return cache["items"]
    courses = await _bb(context).fetch_courses()
    context.bot_data["courses_cache"] = {"at": now, "items": courses}
    return courses


async def _ensure_session(context, chat_id: int, progress=None) -> tuple[bool, str]:
    """Comprueba la sesion y, si expiro, intenta renovarla en silencio."""
    settings = _settings(context)
    store = _store(context)
    bb = _bb(context)

    try:
        courses = await bb.fetch_courses()
        context.bot_data["courses_cache"] = {
            "at": time.monotonic(),
            "items": courses,
        }
        store.mark_session(chat_id, True)
        return True, ""
    except SessionExpired:
        pass
    except BlackboardError as exc:
        return False, f"❌ Error consultando Blackboard:\n<code>{esc(str(exc)[:300])}</code>"

    user = store.get_user(chat_id)
    email = user["email"] if user else None
    provider = user["provider_apid"] if user else None
    password = context.bot_data["volatile"].get(chat_id)

    if user and user["credentials"]:
        try:
            data = crypto_store.decrypt_json(settings.key_path, user["credentials"])
            email = data.get("email") or email
            password = data.get("password") or password
            provider = data.get("provider") or provider
        except Exception:
            log.exception("No pude descifrar las credenciales guardadas")

    # Alternativa sin pasar la contrasena por Telegram:
    # BB_EMAIL / BB_PASSWORD en .env.
    if not email and settings.env_email:
        email = settings.env_email
    if not password and settings.env_password:
        password = settings.env_password

    if not email or not password:
        store.mark_session(chat_id, False)
        return False, (
            "🔐 Tu sesión de Blackboard expiró y no tengo tu contraseña.\n\n"
            "Usa /login para volver a conectar."
        )

    provider = provider or provider_for_email(email, settings.saml_apid_override)
    if progress:
        await progress("🔁 La sesión expiró. Renovando el acceso a Blackboard...")

    try:
        result = await login_with_microsoft(settings, email, password, provider, progress)
        bb.save_cookies(result.cookies)
        provider = result.provider_apid
        courses = await bb.fetch_courses()
        context.bot_data["courses_cache"] = {
            "at": time.monotonic(),
            "items": courses,
        }
        store.save_user(chat_id, email, provider)
        store.mark_login(chat_id)
        return True, ""
    except MFARequired as exc:
        store.mark_session(chat_id, False)
        return False, _mfa_message(exc)
    except LoginError as exc:
        store.mark_session(chat_id, False)
        detail = _redact(str(exc), password)[:400]
        extra = f"\n\nCaptura: <code>{esc(str(exc.screenshot))}</code>" if exc.screenshot else ""
        return False, f"❌ No pude renovar la sesión:\n<code>{esc(detail)}</code>{extra}"
    except Exception as exc:
        log.exception("Error renovando la sesion")
        store.mark_session(chat_id, False)
        return False, (
            "❌ Error renovando la sesión:\n"
            f"<code>{esc(_redact(str(exc), password)[:200])}</code>"
        )


def _mfa_message(exc: MFARequired) -> str:
    extra = f"\n\nCaptura: <code>{esc(str(exc.screenshot))}</code>" if exc.screenshot else ""
    return (
        "⚠️ <b>Microsoft pidió verificación en dos pasos (MFA)</b>\n\n"
        "No puedo completar ese paso automáticamente.\n"
        "Si tu institución lo permite, desactiva MFA para este equipo o "
        "usa una cuenta sin segundo factor."
        + extra
    )


async def _perform_login(
    context, status_message, email: str, password: str, save: bool
) -> None:
    settings = _settings(context)
    store = _store(context)
    bb = _bb(context)
    chat_id = status_message.chat_id
    provider = provider_for_email(email, settings.saml_apid_override)
    progress = _progress_for(status_message)

    busy = context.bot_data.setdefault("login_busy", set())
    if chat_id in busy:
        await _edit_safe(status_message, "⏳ Ya estoy conectando. Espera un momento.")
        return
    busy.add(chat_id)

    try:
        result = await login_with_microsoft(settings, email, password, provider, progress)
        provider = result.provider_apid
        bb.save_cookies(result.cookies)

        await progress("📚 Login correcto. Leyendo tus cursos...")
        courses = await bb.fetch_courses()

        store.save_user(chat_id, email, provider)
        if save:
            blob = crypto_store.encrypt_json(
                settings.key_path,
                {"email": email, "password": password, "provider": provider},
            )
            store.set_credentials(chat_id, blob)
            context.bot_data["volatile"].pop(chat_id, None)
            credentials_note = "Contraseña guardada cifrada en este equipo."
        else:
            store.clear_credentials(chat_id)
            context.bot_data["volatile"][chat_id] = password
            credentials_note = (
                "Contraseña solo en memoria. Si reinicias el bot tendrás que "
                "volver a usar /login cuando expire la sesión."
            )
        store.mark_login(chat_id)
        context.bot_data["courses_cache"] = {
            "at": time.monotonic(),
            "items": courses,
        }

        await _edit_safe(
            status_message,
            "✅ <b>Conectado a Blackboard</b>\n\n"
            f"👤 <code>{esc(email)}</code>\n"
            f"📚 {len(courses)} cursos\n"
            f"🔐 {credentials_note}",
            parse_mode=ParseMode.HTML,
        )

        # Vista previa de tareas (si falla, el login sigue siendo valido)
        try:
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(timezone.utc)
            start = local_day_bounds(now, tz)[0]
            items = await bb.fetch_calendar_items(
                start, now + timedelta(days=14), courses
            )
            tasks = [item for item in items if item.is_task]
            store.mark_items_seen(chat_id, tasks)
            chunks = render_task_list(
                tasks,
                tz=tz,
                now=now,
                range_label="14 días",
                range_phrase="los próximos 14 días",
            )
            await _send_chunks(context, chat_id, chunks)
        except Exception:
            log.exception("No pude generar la vista previa de tareas")
            await context.bot.send_message(
                chat_id, "Usa /tareas para ver tus próximas entregas."
            )

    except MFARequired as exc:
        await _edit_safe(status_message, _mfa_message(exc), parse_mode=ParseMode.HTML)
    except LoginError as exc:
        detail = _redact(str(exc), password)[:400]
        extra = f"\n\nCaptura: <code>{esc(str(exc.screenshot))}</code>" if exc.screenshot else ""
        await _edit_safe(
            status_message,
            "❌ <b>No pude iniciar sesión</b>\n\n"
            f"<code>{esc(detail)}</code>{extra}\n\n"
            "Revisa tu correo y contraseña e inténtalo con /login.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.exception("Error inesperado en el login")
        await _edit_safe(
            status_message,
            "❌ Error inesperado durante el login:\n"
            f"<code>{esc(_redact(str(exc), password)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        busy.discard(chat_id)
        context.user_data.pop("password", None)


# --------------------------------------------------------------------------
#  Comandos basicos
# --------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "🔒 Este bot solo funciona en un chat privado."
        )
        return

    settings = _settings(context)
    store = _store(context)
    if settings.owner_chat_id:
        owner = settings.owner_chat_id
    else:
        owner = store.set_owner(chat.id)
    if chat.id != owner:
        await update.effective_message.reply_text("🔒 Este bot es privado.")
        return

    await update.effective_message.reply_text(
        "👋 <b>Tu asistente privado de Blackboard SENATI</b>\n\n"
        "Puedo mostrarte las tareas próximas, fechas de entrega y eventos "
        "de tus cursos, directo desde tu cuenta.\n\n"
        "🔐 <b>Cómo manejo tus datos</b>\n"
        "• Tu contraseña se usa solo en tu PC para el login de Microsoft.\n"
        "• Nunca se envía a Telegram ni a servidores de terceros.\n"
        "• Si aceptas guardarla, queda cifrada y ligada a tu usuario de Windows.\n\n"
        "Empieza con /login y luego prueba /tareas.\n\n"
        + HELP_TEXT,
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
#  Login conversacional
# --------------------------------------------------------------------------


async def login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _guard(update, context):
        return ConversationHandler.END
    context.user_data.pop("password", None)
    context.user_data.pop("email", None)
    user = _store(context).get_user(update.effective_chat.id)
    current = (
        f"\n\nCuenta actual: <code>{esc(user['email'])}</code>"
        if user and user["email"]
        else ""
    )
    await update.effective_message.reply_text(
        "🔐 <b>Conectar Blackboard</b>\n\n"
        "Paso 1/3 · Escribe tu correo o usuario institucional.\n"
        "Ejemplo: <code>nombre.apellido@senati.pe</code>"
        + current
        + "\n\nEscribe /cancelar para salir.",
        parse_mode=ParseMode.HTML,
    )
    return EMAIL


async def login_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"[^\s]{3,100}", text):
        await update.effective_message.reply_text(
            "No parece un correo válido. Inténtalo otra vez o usa /cancelar."
        )
        return EMAIL
    context.user_data["email"] = text.lower()
    await update.effective_message.reply_text(
        "Paso 2/3 · Escribe tu contraseña de Microsoft.\n\n"
        "⚠️ La borraré del chat en cuanto la reciba (si Telegram lo permite). "
        "Aun así, evita reenviar este mensaje.\n\n"
        "Escribe /cancelar para salir."
    )
    return PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.effective_message.text or ""
    try:
        await update.effective_message.delete()
    except TelegramError:
        log.debug("No pude borrar el mensaje con la contraseña")

    if not password:
        await update.effective_chat.send_message(
            "No recibí la contraseña. Inténtalo de nuevo o usa /cancelar."
        )
        return PASSWORD

    context.user_data["password"] = password
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔐 Guardar cifrada", callback_data="login_save_yes"),
                InlineKeyboardButton("⏳ Solo esta sesión", callback_data="login_save_no"),
            ]
        ]
    )
    await update.effective_chat.send_message(
        "Paso 3/3 · ¿Quieres que guarde la contraseña?\n\n"
        "🔐 <b>Guardar cifrada</b>: podré renovar la sesión sola cuando expire. "
        "Queda cifrada y ligada a tu usuario de Windows.\n"
        "⏳ <b>Solo esta sesión</b>: no se guarda en disco; al reiniciar el bot "
        "tendrás que volver a escribirla cuando la sesión caduque.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return SAVE


async def login_save_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    save = query.data == "login_save_yes"
    await query.edit_message_text("⏳ Conectando con Blackboard...")
    email = context.user_data.get("email")
    password = context.user_data.pop("password", "")
    if not email or not password:
        await _edit_safe(
            query.message, "Se perdió el estado del login. Usa /login otra vez."
        )
        return ConversationHandler.END
    await _perform_login(context, query.message, email, password, save)
    return ConversationHandler.END


async def login_save_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip().lower()
    if text in {"si", "sí", "guardar", "guardala", "guárdala", "yes"}:
        save = True
    elif text in {"no", "solo sesion", "solo sesión", "temporal"}:
        save = False
    else:
        await update.effective_message.reply_text(
            "Usa los botones del mensaje anterior: «Guardar cifrada» o «Solo esta sesión»."
        )
        return SAVE
    email = context.user_data.get("email")
    password = context.user_data.pop("password", "")
    if not email or not password:
        await update.effective_message.reply_text(
            "Se perdió el estado del login. Usa /login otra vez."
        )
        return ConversationHandler.END
    status = await update.effective_message.reply_text("⏳ Conectando con Blackboard...")
    await _perform_login(context, status, email, password, save)
    return ConversationHandler.END


async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("password", None)
    context.user_data.pop("email", None)
    await update.effective_message.reply_text(
        "Login cancelado. No guardé ningún dato nuevo."
    )
    return ConversationHandler.END


async def stale_login_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Botonera vieja de /login pulsada cuando la conversacion ya termino."""
    query = update.callback_query
    await query.answer("Ese inicio de sesión ya terminó.")
    await _edit_safe(
        query.message,
        "⌛ Ese inicio de sesión ya terminó.\nUsa /login para empezar de nuevo.",
    )


# --------------------------------------------------------------------------
#  Tareas y eventos
# --------------------------------------------------------------------------


def _parse_days(args: list[str], default: int = 14) -> int | None:
    if not args:
        return default
    try:
        value = int(args[0])
    except ValueError:
        return None
    return max(1, min(value, 112))


async def _run_task_window(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    start: datetime | None,
    end: datetime | None,
    days: int | None,
    range_label: str,
    range_phrase: str,
) -> None:
    chat_id = update.effective_chat.id
    status = await update.effective_message.reply_text("⏳ Consultando Blackboard...")

    ok, error = await _ensure_session(context, chat_id, _progress_for(status))
    if not ok:
        await _edit_safe(status, error, parse_mode=ParseMode.HTML)
        return

    settings = _settings(context)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(timezone.utc)
    if start is None:
        start = local_day_bounds(now, tz)[0]
    if end is None:
        end = now + timedelta(days=days or 14)

    try:
        courses = await _get_courses(context)
        items = await _bb(context).fetch_calendar_items(start, end, courses)
        tasks = [
            item
            for item in items
            if item.is_task and start <= item.due <= end
        ]
        _store(context).mark_items_seen(chat_id, tasks)
        chunks = render_task_list(
            tasks,
            tz=tz,
            now=now,
            range_label=range_label,
            range_phrase=range_phrase,
        )
    except SessionExpired:
        _store(context).mark_session(chat_id, False)
        await _edit_safe(status, "🔐 La sesión expiró. Usa /login.")
        return
    except BlackboardError as exc:
        await _edit_safe(
            status,
            f"❌ No pude leer tus tareas:\n<code>{esc(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await _send_chunks(context, chat_id, chunks, status=status)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    days = _parse_days(context.args, default=14)
    if days is None:
        await update.effective_message.reply_text("Uso: /tareas [días], por ejemplo /tareas 30")
        return
    await _run_task_window(
        update,
        context,
        start=None,
        end=None,
        days=days,
        range_label=f"{days} días",
        range_phrase=f"los próximos {days} días",
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    tz = ZoneInfo(_settings(context).timezone)
    now = datetime.now(timezone.utc)
    start, end = local_day_bounds(now, tz)
    await _run_task_window(
        update,
        context,
        start=start,
        end=end,
        days=1,
        range_label="hoy",
        range_phrase="hoy",
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    await _run_task_window(
        update,
        context,
        start=None,
        end=None,
        days=7,
        range_label="7 días",
        range_phrase="los próximos 7 días",
    )


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    await _run_task_window(
        update,
        context,
        start=None,
        end=None,
        days=31,
        range_label="31 días",
        range_phrase="los próximos 31 días",
    )


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    days = _parse_days(context.args, default=14)
    if days is None:
        await update.effective_message.reply_text("Uso: /eventos [días]")
        return

    chat_id = update.effective_chat.id
    status = await update.effective_message.reply_text("⏳ Consultando el calendario...")
    ok, error = await _ensure_session(context, chat_id, _progress_for(status))
    if not ok:
        await _edit_safe(status, error, parse_mode=ParseMode.HTML)
        return

    settings = _settings(context)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(timezone.utc)
    start = local_day_bounds(now, tz)[0]
    end = now + timedelta(days=days)

    try:
        courses = await _get_courses(context)
        items = await _bb(context).fetch_calendar_items(start, end, courses)
        events = [
            item
            for item in items
            if item.is_event and start <= item.start <= end
        ]
        chunks = render_event_list(
            events,
            tz=tz,
            now=now,
            range_label=f"{days} días",
            range_phrase=f"los próximos {days} días",
        )
    except SessionExpired:
        _store(context).mark_session(chat_id, False)
        await _edit_safe(status, "🔐 La sesión expiró. Usa /login.")
        return
    except BlackboardError as exc:
        await _edit_safe(
            status,
            f"❌ No pude leer el calendario:\n<code>{esc(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await _send_chunks(context, chat_id, chunks, status=status)


async def cmd_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    chat_id = update.effective_chat.id
    status = await update.effective_message.reply_text("⏳ Leyendo tus cursos...")
    ok, error = await _ensure_session(context, chat_id, _progress_for(status))
    if not ok:
        await _edit_safe(status, error, parse_mode=ParseMode.HTML)
        return
    try:
        courses = await _get_courses(context, force=True)
    except SessionExpired:
        _store(context).mark_session(chat_id, False)
        await _edit_safe(status, "🔐 La sesión expiró. Usa /login.")
        return
    except BlackboardError as exc:
        await _edit_safe(
            status,
            f"❌ No pude leer tus cursos:\n<code>{esc(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _send_chunks(context, chat_id, render_courses(courses), status=status)


# --------------------------------------------------------------------------
#  Estado, refresco y logout
# --------------------------------------------------------------------------


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    settings = _settings(context)
    store = _store(context)
    chat_id = update.effective_chat.id
    user = store.get_user(chat_id)
    saved_creds = bool(user and user["credentials"])
    env_creds = bool(settings.env_email and settings.env_password)
    in_memory = chat_id in context.bot_data["volatile"]

    try:
        courses = await _bb(context).fetch_courses()
        session_text = f"activa · {len(courses)} cursos"
    except SessionExpired:
        session_text = (
            "expirada · se renovará al consultar"
            if (saved_creds or env_creds or in_memory)
            else "expirada · usa /login"
        )
    except BlackboardError as exc:
        session_text = f"con errores · {esc(str(exc)[:80])}"

    if saved_creds:
        credentials = "guardada cifrada en este equipo"
    elif in_memory:
        credentials = "solo en memoria (se pierde al reiniciar)"
    elif env_creds:
        credentials = "desde .env (texto plano)"
    else:
        credentials = "no disponible · usa /login"

    tz = ZoneInfo(settings.timezone)
    interval = settings.check_interval_min
    auto = f"cada {interval} min" if interval else "desactivada"

    lines = [
        "📊 <b>Estado</b>",
        "",
        f"👤 Cuenta: <code>{esc(mask_email(user['email'] if user else None))}</code>",
        f"🔐 Contraseña: {credentials}",
        f"📡 Sesión Blackboard: {session_text}",
        f"🕒 Último login: {format_dt_short(user['last_login_at'] if user else None, tz)}",
        f"🔄 Última revisión: {format_dt_short(user['last_check_at'] if user else None, tz)}",
        f"⏱️ Auto-revisión: {auto}",
        "",
        f"📁 Datos: <code>{esc(str(settings.data_dir))}</code>",
    ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    chat_id = update.effective_chat.id
    status = await update.effective_message.reply_text("⏳ Refrescando la sesión...")
    context.bot_data.pop("courses_cache", None)
    ok, error = await _ensure_session(context, chat_id, _progress_for(status))
    if not ok:
        await _edit_safe(status, error, parse_mode=ParseMode.HTML)
        return
    try:
        courses = await _get_courses(context, force=True)
    except BlackboardError as exc:
        await _edit_safe(
            status,
            f"⚠️ Sesión activa, pero no pude leer los cursos:\n"
            f"<code>{esc(str(exc)[:200])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _edit_safe(
        status,
        f"✅ Sesión activa · {len(courses)} cursos.\nUsa /tareas para ver las entregas.",
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Sí, desconectar", callback_data="logout_yes"),
                InlineKeyboardButton("Cancelar", callback_data="logout_no"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        "¿Borrar la contraseña guardada y la sesión de Blackboard de este equipo?",
        reply_markup=keyboard,
    )


async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await _guard(update, context):
        return
    if query.data != "logout_yes":
        await _edit_safe(query.message, "Cancelado. No borré nada.")
        return

    chat_id = update.effective_chat.id
    store = _store(context)
    store.clear_credentials(chat_id)
    store.mark_session(chat_id, False)
    store.reset_reminders(chat_id)
    context.bot_data["volatile"].pop(chat_id, None)
    context.bot_data.pop("courses_cache", None)
    try:
        await _bb(context).reset()
    except Exception:
        log.exception("No pude borrar la sesión de Blackboard")
    await _edit_safe(
        query.message,
        "✅ Cuenta desconectada.\n\n"
        "Borré la contraseña guardada, la sesión y los recordatorios. "
        "Puedes volver a conectar con /login.",
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        return
    settings = _settings(context)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=3)
    until = now + timedelta(days=14)
    checks = [
        (
            "/learn/api/v1/users/me/memberships",
            {"limit": 1, "offset": 0},
        ),
        (
            "/learn/api/v1/calendars",
            {"limit": 10000},
        ),
        (
            "/learn/api/v1/calendars/calendarItems",
            {"since": iso_z(since), "until": iso_z(until)},
        ),
        (
            "/learn/api/v1/calendars/calendarEventsAvailable",
            None,
        ),
    ]
    lines = [
        "🛠 <b>Diagnóstico</b>",
        "",
        f"🌐 Base: <code>{esc(settings.base_url)}</code>",
        f"🧭 Proveedor: <code>{esc(settings.saml_apid_override or 'automático')}</code>",
        f"💾 Datos: <code>{esc(str(settings.data_dir))}</code>",
        f"🍪 Cookies: {len(_bb(context)._cookies)}",
        "",
    ]
    for path, params in checks:
        status, detail = await _bb(context).probe(path, params)
        lines.append(f"• <code>{esc(path)}</code>")
        lines.append(f"  → <b>{esc(status)}</b> · {esc(detail)}")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------------------
#  Revision automatica
# --------------------------------------------------------------------------


def _new_items_message(items: list[CalendarItem], tz) -> str:
    heading = (
        "🆕 <b>Nueva tarea detectada</b>"
        if len(items) == 1
        else "🆕 <b>Nuevas tareas detectadas</b>"
    )
    lines = [heading, ""]
    for item in items[:10]:
        due_local = item.due.astimezone(tz)
        lines.append(f"• <b>{esc(item.title[:120])}</b>")
        lines.append(
            f"   {esc(item.course_name[:80])} · "
            f"{due_local:%d/%m %H:%M} ({relative_due(item.due, datetime.now(timezone.utc))})"
        )
    if len(items) > 10:
        lines.append(f"… y {len(items) - 10} más. Usa /tareas.")
    return "\n".join(lines)


def _reminder_message(reminders: list[tuple[CalendarItem, str]], tz) -> str:
    lines = ["⏰ <b>Recordatorio de entrega</b>", ""]
    for item, kind in reminders[:10]:
        due_local = item.due.astimezone(tz)
        label = relative_due(item.due, datetime.now(timezone.utc))
        lines.append(f"• <b>{esc(item.title[:120])}</b>")
        lines.append(
            f"   {esc(item.course_name[:80])} · hoy {due_local:%H:%M} ({label})"
            if due_local.date() == datetime.now(tz).date()
            else f"   {esc(item.course_name[:80])} · {due_local:%d/%m %H:%M} ({label})"
        )
    return "\n".join(lines)


async def job_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    store = _store(context)
    bb = _bb(context)
    owner = _owner(context)
    if owner is None:
        return
    if not (
        store.has_credentials(owner)
        or owner in context.bot_data["volatile"]
        or bb.has_saved_session()
        or (settings.env_email and settings.env_password)
    ):
        return

    ok, error = await _ensure_session(context, owner)
    if not ok:
        log.info("Revision automatica omitida: %s", error)
        return

    tz = ZoneInfo(settings.timezone)
    now = datetime.now(timezone.utc)
    start = local_day_bounds(now, tz)[0]
    end = now + timedelta(days=21)

    try:
        courses = await _get_courses(context)
        items = await bb.fetch_calendar_items(start, end, courses)
    except Exception as exc:
        log.warning("Revision automatica fallo: %s", exc)
        return

    tasks = [item for item in items if item.is_task and item.due >= start]

    # Primera revision correcta: registrar sin avisar de todo el historial.
    # Se usa una bandera persistente (no seen_count) para que una cuenta sin
    # tareas al inicio no se trague la primera tarea que aparezca despues.
    first_key = f"first_check_done:{owner}"
    if not store.get_meta(first_key):
        store.mark_items_seen(owner, tasks)
        store.set_meta(first_key, "1")
        store.touch_check(owner)
        return

    new_rows = store.diff_new_items(owner, tasks)
    new_ids = {row["id"] for row in new_rows}
    new_items = [item for item in tasks if item.id in new_ids]

    reminders: list[tuple[CalendarItem, str]] = []
    for item in tasks:
        remaining = item.due - now
        if remaining.total_seconds() <= 0:
            continue
        if remaining <= timedelta(hours=2):
            if store.claim_reminder(owner, item.id, "2h"):
                reminders.append((item, "2h"))
        elif remaining <= timedelta(hours=24):
            if store.claim_reminder(owner, item.id, "24h"):
                reminders.append((item, "24h"))

    try:
        if new_items:
            await context.bot.send_message(
                owner,
                _new_items_message(new_items, tz),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        if reminders:
            await context.bot.send_message(
                owner,
                _reminder_message(reminders, tz),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except TelegramError as exc:
        log.warning("No pude enviar la notificacion: %s", exc)

    store.touch_check(owner)
    store.cleanup(owner)


# --------------------------------------------------------------------------
#  Arranque
# --------------------------------------------------------------------------


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("tareas", "Próximas entregas (14 días)"),
        BotCommand("hoy", "Lo que vence hoy"),
        BotCommand("semana", "Próximos 7 días"),
        BotCommand("mes", "Próximos 31 días"),
        BotCommand("eventos", "Clases y eventos"),
        BotCommand("cursos", "Tus cursos"),
        BotCommand("login", "Conectar o renovar la cuenta"),
        BotCommand("estado", "Estado de la sesión"),
        BotCommand("refrescar", "Forzar actualización"),
        BotCommand("logout", "Borrar credenciales y sesión"),
        BotCommand("ayuda", "Ayuda"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except TelegramError:
        log.warning("No pude registrar el menú de comandos")


async def post_shutdown(app: Application) -> None:
    try:
        await app.bot_data["bb"].close()
    finally:
        app.bot_data["store"].close()


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error no controlado", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Ocurrió un error inesperado. Revisa la consola del bot."
            )
    except TelegramError:
        pass


def _setup_logging(settings: Settings) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        settings.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    settings = get_settings()
    _setup_logging(settings)
    log.info("Datos del bot: %s", settings.data_dir)

    store = Storage(settings.db_path)
    bb = BlackboardSession(settings.base_url, settings.session_path, settings.key_path)

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data.update(
        {
            "settings": settings,
            "store": store,
            "bb": bb,
            "volatile": {},
            "login_busy": set(),
        }
    )

    login_conversation = ConversationHandler(
        entry_points=[CommandHandler("login", login_entry)],
        states={
            EMAIL: [
                CommandHandler("cancelar", login_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email),
            ],
            PASSWORD: [
                CommandHandler("cancelar", login_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password),
            ],
            SAVE: [
                CommandHandler("cancelar", login_cancel),
                CallbackQueryHandler(login_save_choice, pattern=r"^login_save_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_save_text),
            ],
        },
        fallbacks=[CommandHandler("cancelar", login_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(login_conversation)
    app.add_handler(
        CallbackQueryHandler(stale_login_callback, pattern=r"^login_save_")
    )
    app.add_handler(CommandHandler("tareas", cmd_tasks))
    app.add_handler(CommandHandler("hoy", cmd_today))
    app.add_handler(CommandHandler("semana", cmd_week))
    app.add_handler(CommandHandler("mes", cmd_month))
    app.add_handler(CommandHandler("eventos", cmd_events))
    app.add_handler(CommandHandler("cursos", cmd_courses))
    app.add_handler(CommandHandler("estado", cmd_status))
    app.add_handler(CommandHandler("refrescar", cmd_refresh))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(logout_callback, pattern=r"^logout_"))
    app.add_error_handler(on_error)

    if settings.check_interval_min > 0 and app.job_queue is not None:
        app.job_queue.run_repeating(
            job_check,
            interval=timedelta(minutes=settings.check_interval_min),
            first=timedelta(minutes=2),
            name="revision-tareas",
        )
        log.info(
            "Revisión automática activada cada %s minutos", settings.check_interval_min
        )
    else:
        log.info("Revisión automática desactivada")

    log.info("Bot iniciado. Presiona Ctrl+C para detenerlo.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
