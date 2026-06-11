"""Telegram front-end for the outbound dialer.

Paste a list of numbers -> confirm -> the bot launches the campaign and
keeps a live status message updated, then posts a final report.
"""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .ari import ARIClient
from .config import Config, load_config
from .dialer import Campaign, Dialer, Outcome
from .numbers import parse_numbers
from .tts import ensure_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("piensa.bot")


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ari = ARIClient(cfg.ari_rest_url, cfg.ari_username, cfg.ari_password, cfg.stasis_app)
        self.dialer = Dialer(self.ari, cfg)
        # number list staged per chat, awaiting confirmation
        self._pending: dict[int, list[str]] = {}

    # ----------------------------------------------------------------- auth
    def _authorized(self, update: Update) -> bool:
        if not self.cfg.allowed_user_ids:
            return True  # open mode (set TELEGRAM_ALLOWED_USERS to lock down)
        user = update.effective_user
        return bool(user and user.id in self.cfg.allowed_user_ids)

    async def _guard(self, update: Update) -> bool:
        if self._authorized(update):
            return True
        if update.effective_message:
            await update.effective_message.reply_text("No estás autorizado para usar este bot.")
        return False

    # ----------------------------------------------------------------- commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        uid = update.effective_user.id if update.effective_user else "?"
        await update.effective_message.reply_text(
            "👋 <b>Piensa Dialer</b>\n\n"
            "Pégame una lista de números (uno por línea, o separados por comas "
            "o espacios) y lanzo las llamadas. Cuando el destinatario pulse <b>1</b> "
            "se transfiere al agente.\n\n"
            "Comandos:\n"
            "/status — estado de la campaña actual\n"
            "/stop — cancelar la campaña en curso\n"
            "/config — ver la configuración activa\n\n"
            f"Tu Telegram ID es <code>{uid}</code>.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        c = self.cfg
        await update.effective_message.reply_text(
            "<b>Configuración</b>\n"
            f"• Trunk SIP: <code>{html.escape(c.sip_endpoint)}</code>\n"
            f"• Caller ID: <code>{html.escape(c.caller_id)}</code>\n"
            f"• Transferencia a: <code>{html.escape(c.agent_number)}</code>\n"
            f"• Llamadas simultáneas: <code>{c.max_concurrent_calls}</code>\n"
            f"• Timeout llamada: <code>{c.call_timeout}s</code>\n"
            f"• Mensaje: <i>{html.escape(c.message_text)}</i>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        campaign = self.dialer.active
        if campaign is None:
            await update.effective_message.reply_text("No hay ninguna campaña.")
            return
        await update.effective_message.reply_text(
            self._render_status(campaign), parse_mode=ParseMode.HTML
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if self.dialer.cancel():
            await update.effective_message.reply_text("🛑 Cancelando la campaña…")
        else:
            await update.effective_message.reply_text("No hay ninguna campaña en curso.")

    # ----------------------------------------------------------------- number input
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        valid, invalid = parse_numbers(message.text or "", self.cfg.default_country_code)

        if not valid:
            await message.reply_text(
                "No he encontrado números válidos. Pégame algo como:\n"
                "<code>+34600111222\n+34600333444</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        self._pending[chat_id] = valid
        preview = "\n".join(f"• <code>{html.escape(n)}</code>" for n in valid[:20])
        extra = f"\n…y {len(valid) - 20} más" if len(valid) > 20 else ""
        warn = (
            f"\n\n⚠️ Ignorados {len(invalid)} valores no válidos." if invalid else ""
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📞 Llamar", callback_data="launch"),
                    InlineKeyboardButton("❌ Cancelar", callback_data="discard"),
                ]
            ]
        )
        await message.reply_text(
            f"He detectado <b>{len(valid)}</b> número(s):\n{preview}{extra}{warn}\n\n"
            "¿Lanzo las llamadas?",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            await query.edit_message_text("No estás autorizado.")
            return
        chat_id = update.effective_chat.id

        if query.data == "discard":
            self._pending.pop(chat_id, None)
            await query.edit_message_text("Operación cancelada.")
            return

        if query.data == "launch":
            numbers = self._pending.pop(chat_id, None)
            if not numbers:
                await query.edit_message_text("La lista ha caducado, vuelve a pegarla.")
                return
            if self.dialer.is_busy:
                await query.edit_message_text("Ya hay una campaña en curso. Usa /status.")
                return
            campaign = self.dialer.start_campaign(numbers, chat_id)
            await query.edit_message_text(
                f"🚀 Campaña <code>{campaign.id}</code> lanzada con "
                f"{len(numbers)} llamadas.",
                parse_mode=ParseMode.HTML,
            )
            status_msg = await context.bot.send_message(
                chat_id, self._render_status(campaign), parse_mode=ParseMode.HTML
            )
            asyncio.create_task(self._watch_campaign(context, campaign, status_msg.message_id))

    # ----------------------------------------------------------------- reporting
    async def _watch_campaign(self, context: ContextTypes.DEFAULT_TYPE, campaign: Campaign, message_id: int) -> None:
        last_text = ""
        while True:
            try:
                await asyncio.wait_for(campaign.progress_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            campaign.progress_event.clear()

            text = self._render_status(campaign)
            if text != last_text:
                last_text = text
                try:
                    await context.bot.edit_message_text(
                        text, chat_id=campaign.chat_id, message_id=message_id,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:  # noqa: BLE001 - "message is not modified" etc.
                    pass

            if campaign.completed:
                break

        await context.bot.send_message(
            campaign.chat_id, self._render_report(campaign), parse_mode=ParseMode.HTML
        )

    def _render_status(self, campaign: Campaign) -> str:
        s = campaign.snapshot()
        bar_done = s["done"]
        total = s["total"]
        state = "✅ finalizada" if s["completed"] else ("🛑 cancelando" if s["cancelled"] else "📞 en curso")
        return (
            f"<b>Campaña {campaign.id}</b> — {state}\n"
            f"Progreso: <b>{bar_done}/{total}</b>\n"
            f"En curso: {s['in_progress']} · En cola: {s['queued']}\n"
            f"✅ Pulsaron 1: <b>{s['success']}</b>"
        )

    def _render_report(self, campaign: Campaign) -> str:
        s = campaign.snapshot()
        lines = [f"<b>📋 Informe campaña {campaign.id}</b>", f"Total: {s['total']}", ""]
        for outcome in Outcome:
            n = s["counts"].get(outcome, 0)
            if n:
                lines.append(f"• {outcome.value}: <b>{n}</b>")
        lines.append("")
        lines.append(f"✅ <b>{s['success']}</b> pidieron hablar con un agente.")

        detail = [
            f"{r.number}: {r.outcome.value if r.outcome else '-'}"
            for r in campaign.records
            if r.outcome in {Outcome.PRESSED_1, Outcome.TRANSFERRED, Outcome.TRANSFER_FAILED}
        ]
        if detail:
            lines.append("")
            lines.append("<b>Pulsaron 1:</b>")
            lines.extend(f"• <code>{html.escape(d)}</code>" for d in detail)
        return "\n".join(lines)

    # ----------------------------------------------------------------- wiring
    def build(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("config", self.cmd_config))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    async def _post_init(self, app: Application) -> None:
        logger.info("Preparing TTS prompt…")
        await ensure_prompt(self.cfg.message_text, self.cfg.tts_lang, self.cfg.sounds_dir, self.cfg.sound_name)
        logger.info("Connecting to ARI at %s…", self.cfg.ari_rest_url)
        await self.ari.connect(self.dialer.handle_event)
        await self.ari.wait_until_ready()
        logger.info("Bot ready.")

    async def _post_shutdown(self, app: Application) -> None:
        await self.ari.close()


def main() -> None:
    cfg = load_config()
    BotApp(cfg).build().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
