"""P1 - Bot de Telegram para lanzar campanas de llamadas (IVR 'pulse 1')."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ami import Dialer
from campaign import Campaign
from config import Config
from phone_numbers import parse_numbers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("p1.bot")

cfg = Config.from_env()

HELP = (
    "<b>P1 · Bot de llamadas</b>\n\n"
    "1️⃣ Pégame la lista de números (uno por línea, o separados por comas; "
    "también puedes subir un .txt/.csv).\n"
    "2️⃣ Te diré cuántos he detectado.\n"
    "3️⃣ Escribe /llamar para lanzar la campaña.\n\n"
    "El bot llama a cada número, reproduce el aviso y, si pulsan <b>1</b>, "
    "transfiere la llamada a vuestro número.\n\n"
    "<b>Comandos</b>\n"
    "/llamar – lanza la campaña con los números cargados\n"
    "/estado – progreso de la campaña en curso\n"
    "/stop – cancela la campaña en curso\n"
    "/ayuda – muestra esta ayuda"
)


def authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if not cfg.allowed_user_ids:
        return False
    return user.id in cfg.allowed_user_ids


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        f"⛔ No autorizado. Tu ID de Telegram es: {uid}\n"
        "Pide que te añadan a ALLOWED_USER_IDS."
    )


# ---------------------------------------------------------------- comandos
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def on_numbers_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    nums = parse_numbers(update.message.text or "", cfg.default_country_code)
    await _store_and_confirm(update, ctx, nums)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    doc = update.message.document
    if doc.file_size and doc.file_size > 1_000_000:
        return await update.message.reply_text("El fichero es demasiado grande (máx 1 MB).")
    f = await doc.get_file()
    content = await f.download_as_bytearray()
    try:
        text = bytes(content).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return await update.message.reply_text("No pude leer el fichero.")
    nums = parse_numbers(text, cfg.default_country_code)
    await _store_and_confirm(update, ctx, nums)


async def _store_and_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, nums: list[str]) -> None:
    if not nums:
        return await update.message.reply_text(
            "No he detectado ningún número válido. Inténtalo de nuevo."
        )
    ctx.user_data["pending_numbers"] = nums
    preview = "\n".join(nums[:10])
    extra = f"\n… y {len(nums) - 10} más" if len(nums) > 10 else ""
    await update.message.reply_text(
        f"✅ Detectados <b>{len(nums)}</b> número(s):\n<code>{preview}</code>{extra}\n\n"
        "Escribe /llamar para lanzar la campaña.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_llamar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)

    dialer: Dialer | None = ctx.application.bot_data.get("dialer")
    if dialer is None or not ctx.application.bot_data.get("ami_ok"):
        return await update.message.reply_text(
            "⚠️ No hay conexión con Asterisk (AMI). Revisa que el contenedor "
            "asterisk esté arrancado y la configuración del trunk."
        )

    if ctx.application.bot_data.get("active_campaign") is not None:
        return await update.message.reply_text(
            "⏳ Ya hay una campaña en curso. Usa /estado o /stop."
        )

    nums = ctx.user_data.get("pending_numbers")
    if not nums:
        return await update.message.reply_text(
            "Primero pégame la lista de números a llamar."
        )

    chat_id = update.effective_chat.id

    async def notify(text: str) -> None:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    per_call_timeout = cfg.dial_timeout + cfg.dtmf_timeout + cfg.dial_timeout + 30
    campaign = Campaign(
        numbers=list(nums),
        max_concurrent=cfg.max_concurrent,
        per_call_timeout=per_call_timeout,
        notify=notify,
    )
    ctx.application.bot_data["active_campaign"] = campaign
    ctx.user_data["pending_numbers"] = []

    async def runner() -> None:
        try:
            await campaign.run(dialer)
        except Exception:  # noqa: BLE001
            log.exception("Fallo en la campaña")
            await notify("❌ La campaña terminó con un error inesperado.")
        finally:
            ctx.application.bot_data["active_campaign"] = None

    ctx.application.create_task(runner())


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    camp: Campaign | None = ctx.application.bot_data.get("active_campaign")
    if camp is None:
        return await update.message.reply_text("No hay ninguna campaña en curso.")
    await update.message.reply_text(
        f"📊 Campaña <code>{camp.id}</code>: {camp.done_count}/{len(camp.numbers)} completadas.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    camp: Campaign | None = ctx.application.bot_data.get("active_campaign")
    if camp is None:
        return await update.message.reply_text("No hay ninguna campaña en curso.")
    camp.cancel()
    await update.message.reply_text(
        "🛑 Cancelando: no se iniciarán más llamadas (las en curso terminarán)."
    )


# ---------------------------------------------------------------- ciclo de vida
async def post_init(app: Application) -> None:
    def on_result(target: str, result: str, extra: dict) -> None:
        camp: Campaign | None = app.bot_data.get("active_campaign")
        if camp is not None:
            camp.handle_result(target, result, extra)

    dialer = Dialer(
        host=cfg.ami_host,
        port=cfg.ami_port,
        username=cfg.ami_user,
        secret=cfg.ami_secret,
        trunk_endpoint=cfg.trunk_endpoint,
        ivr_context=cfg.ivr_context,
        dial_timeout=cfg.dial_timeout,
        on_result=on_result,
    )
    app.bot_data["dialer"] = dialer
    app.bot_data["active_campaign"] = None
    try:
        await dialer.connect()
        app.bot_data["ami_ok"] = True
    except Exception:  # noqa: BLE001
        log.exception("No se pudo conectar al AMI; el bot arranca igualmente")
        app.bot_data["ami_ok"] = False


async def post_shutdown(app: Application) -> None:
    dialer: Dialer | None = app.bot_data.get("dialer")
    if dialer is not None:
        dialer.close()


def main() -> None:
    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "ayuda", "help"], cmd_start))
    app.add_handler(CommandHandler("llamar", cmd_llamar))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler(["stop", "parar"], cmd_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_numbers_text))

    log.info("Bot P1 arrancando (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
