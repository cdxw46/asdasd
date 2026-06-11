"""Telegram front-end for the outbound dialer.

Menu-driven bot: launch campaigns (paste numbers), manage locutions (upload
MP3 or generate from text), pick which prompt is played to the callee and
which identification message the agent hears on transfer, and review history.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile

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

from .agentes import AgentStore
from .ami import AMIClient
from .ari import ARIClient
from .config import Config, load_config
from .dialer import Campaign, Dialer, Outcome
from .locuciones import LocutionStore
from .numbers import parse_numbers
from .provisioning import ProvisioningServer
from . import qr
from .tts import ensure_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("piensa.bot")

ROLE_LABEL = {"cliente": "Cliente (al que llamamos)", "agente": "Agente (al transferir)"}


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ari = ARIClient(cfg.ari_rest_url, cfg.ari_username, cfg.ari_password, cfg.stasis_app)
        self.ami = AMIClient(cfg.ami_host, cfg.ami_port, cfg.ami_user, cfg.ami_password)
        self.locutions = LocutionStore(cfg.sounds_dir, cfg.tts_lang)
        self.agents = AgentStore(cfg.agents_store_path, cfg.agents_include_path, self.ami)
        self.dialer = Dialer(self.ari, cfg, self.locutions, self.agents)
        self.provisioning = ProvisioningServer(self.agents, self._sip_host(), cfg.provision_port)
        self._pending: dict[int, list[str]] = {}        # numbers awaiting confirm
        self._state: dict[int, dict] = {}               # per-chat conversational state

    def _sip_host(self) -> str:
        if self.cfg.sip_public_host:
            return self.cfg.sip_public_host
        # Fall back to the host in the provisioning base URL.
        base = self.cfg.provision_base_url
        if base:
            return base.split("://")[-1].split(":")[0].split("/")[0]
        return "CAMBIA_ESTA_IP"

    def _prov_url(self, token: str) -> str:
        base = self.cfg.provision_base_url
        if not base:
            base = f"http://{self._sip_host()}:{self.cfg.provision_port}"
        return f"{base.rstrip('/')}/prov/{token}.xml"

    # ----------------------------------------------------------------- auth
    def _authorized(self, update: Update) -> bool:
        if not self.cfg.allowed_user_ids:
            return True
        user = update.effective_user
        return bool(user and user.id in self.cfg.allowed_user_ids)

    async def _guard(self, update: Update) -> bool:
        if self._authorized(update):
            return True
        if update.effective_message:
            await update.effective_message.reply_text("No estás autorizado para usar este bot.")
        return False

    # ----------------------------------------------------------------- menus
    def _main_menu(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📞 Llamar", callback_data="menu:call"),
                 InlineKeyboardButton("🎙 Locuciones", callback_data="menu:loc")],
                [InlineKeyboardButton("👥 Agentes", callback_data="menu:agents"),
                 InlineKeyboardButton("📋 Historial", callback_data="menu:hist")],
                [InlineKeyboardButton("⚙️ Configuración", callback_data="menu:cfg")],
            ]
        )

    def _agents_menu(self) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for a in self.agents.list():
            rows.append([
                InlineKeyboardButton(f"👤 {a.name} ({a.sip_user})", callback_data=f"ag:qr:{a.id}"),
                InlineKeyboardButton("🗑", callback_data=f"ag:del:{a.id}"),
            ])
        rows.append([InlineKeyboardButton("➕ Crear agente", callback_data="ag:new")])
        rows.append([InlineKeyboardButton("⬅️ Menú", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _loc_menu(self) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for role in ("cliente", "agente"):
            active = self.locutions.active(role)
            rows.append([InlineKeyboardButton(
                f"— {ROLE_LABEL[role]} —", callback_data="noop")])
            for loc in self.locutions.list(role):
                mark = "✅ " if active and active.id == loc.id else "▫️ "
                rows.append([
                    InlineKeyboardButton(f"{mark}{loc.name}", callback_data=f"loc:act:{loc.id}"),
                    InlineKeyboardButton("🗑", callback_data=f"loc:del:{loc.id}"),
                ])
        rows.append([
            InlineKeyboardButton("➕ Subir MP3", callback_data="loc:up"),
            InlineKeyboardButton("✍️ Desde texto", callback_data="loc:tts:cliente"),
        ])
        rows.append([InlineKeyboardButton("⬅️ Menú", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _main_text(self) -> str:
        cli = self.locutions.active("cliente")
        ag = self.locutions.active("agente")
        return (
            "🟢 <b>P1 Call Center</b>\n\n"
            f"🎙 Locución cliente: <b>{html.escape(cli.name) if cli else '—'}</b>\n"
            f"🔁 Locución agente: <b>{html.escape(ag.name) if ag else '—'}</b>\n"
            f"📞 Transferencia a: <code>{html.escape(self.cfg.agent_display)}</code>\n\n"
            "Pégame números para llamar, o usa los botones."
        )

    # ----------------------------------------------------------------- commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        self._state.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(
            self._main_text(), reply_markup=self._main_menu(), parse_mode=ParseMode.HTML
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        campaign = self.dialer.active
        if campaign is None:
            await update.effective_message.reply_text("No hay ninguna campaña.")
            return
        await update.effective_message.reply_text(self._render_status(campaign), parse_mode=ParseMode.HTML)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = "🛑 Cancelando la campaña…" if self.dialer.cancel() else "No hay ninguna campaña en curso."
        await update.effective_message.reply_text(msg)

    # ----------------------------------------------------------------- buttons
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            await query.edit_message_text("No estás autorizado.")
            return
        chat_id = update.effective_chat.id
        data = query.data or ""

        if data == "noop":
            return
        if data == "menu:main":
            self._state.pop(chat_id, None)
            await query.edit_message_text(self._main_text(), reply_markup=self._main_menu(), parse_mode=ParseMode.HTML)
        elif data == "menu:call":
            await query.edit_message_text(
                "📞 Pégame los números (uno por línea, o separados por comas/espacios) "
                "y te pediré confirmación antes de llamar.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu:main")]]),
            )
        elif data == "menu:loc":
            await query.edit_message_text(
                "🎙 <b>Locuciones</b>\nElige cuál suena. Pulsa una para activarla.",
                reply_markup=self._loc_menu(), parse_mode=ParseMode.HTML,
            )
        elif data == "menu:agents":
            await query.edit_message_text(
                "👥 <b>Agentes</b>\nUsuarios SIP para Zoiper/PortSIP. Pulsa uno para ver su QR.",
                reply_markup=self._agents_menu(), parse_mode=ParseMode.HTML,
            )
        elif data == "ag:new":
            self._state[chat_id] = {"await": "agent_name"}
            await query.edit_message_text(
                "👤 Escríbeme el <b>nombre</b> del nuevo agente (ej. «Juan» o «Soporte 1»).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Agentes", callback_data="menu:agents")]]),
                parse_mode=ParseMode.HTML,
            )
        elif data.startswith("ag:del:"):
            await self.agents.delete(data.split(":")[2])
            await query.edit_message_text("👥 <b>Agentes</b>\nAgente eliminado.", reply_markup=self._agents_menu(), parse_mode=ParseMode.HTML)
        elif data.startswith("ag:qr:"):
            await self._send_agent_qr(query, context, chat_id, data.split(":")[2])
        elif data == "menu:hist":
            await query.edit_message_text(self._render_history(), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Menú", callback_data="menu:main")]]), parse_mode=ParseMode.HTML)
        elif data == "menu:cfg":
            await query.edit_message_text(self._render_config(), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Menú", callback_data="menu:main")]]), parse_mode=ParseMode.HTML)

        elif data == "loc:up":
            self._state[chat_id] = {"await": "audio_role_then_file"}
            await query.edit_message_text(
                "📤 Envíame ahora el archivo de audio (MP3, OGG, M4A o nota de voz). "
                "Lo convierto y lo añado como locución.\n\n"
                "Pon el nombre en el <i>pie de foto/caption</i> si quieres.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Locuciones", callback_data="menu:loc")]]),
                parse_mode=ParseMode.HTML,
            )
        elif data.startswith("loc:tts:"):
            role = data.split(":")[2]
            self._state[chat_id] = {"await": "loc_text", "role": role}
            await query.edit_message_text(
                f"✍️ Escríbeme el texto para la locución de <b>{ROLE_LABEL[role]}</b>. "
                "Lo convierto a voz en español.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Locuciones", callback_data="menu:loc")]]),
                parse_mode=ParseMode.HTML,
            )
        elif data.startswith("loc:act:"):
            await self.locutions.set_active(data.split(":")[2])
            await query.edit_message_text("🎙 <b>Locuciones</b>\nActivada.", reply_markup=self._loc_menu(), parse_mode=ParseMode.HTML)
        elif data.startswith("loc:del:"):
            await self.locutions.delete(data.split(":")[2])
            await query.edit_message_text("🎙 <b>Locuciones</b>\nEliminada.", reply_markup=self._loc_menu(), parse_mode=ParseMode.HTML)

        elif data.startswith("uprole:"):
            await self._finish_audio_upload(query, context, chat_id, data.split(":")[1])

        elif data == "launch":
            await self._launch(query, context, chat_id)
        elif data == "discard":
            self._pending.pop(chat_id, None)
            await query.edit_message_text("Operación cancelada.")

    # ----------------------------------------------------------------- text input
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        state = self._state.get(chat_id)

        # Awaiting a new agent name.
        if state and state.get("await") == "agent_name":
            self._state.pop(chat_id, None)
            await message.reply_text("⏳ Creando agente y recargando Asterisk…")
            agent = await self.agents.create(message.text.strip())
            await self._deliver_agent(context, chat_id, agent.id, created=True)
            return

        # Awaiting locution text -> create a TTS locution.
        if state and state.get("await") == "loc_text":
            role = state.get("role", "cliente")
            text = message.text.strip()
            self._state.pop(chat_id, None)
            await message.reply_text("⏳ Generando locución…")
            loc = await self.locutions.add_from_text(text, name=text[:40], role=role)
            await self.locutions.set_active(loc.id)
            await message.reply_text(
                f"✅ Locución «{html.escape(loc.name)}» creada y activada para {ROLE_LABEL[role]}.",
                reply_markup=self._main_menu(),
            )
            return

        valid, invalid = parse_numbers(message.text or "", self.cfg.default_country_code)
        if not valid:
            await message.reply_text(
                "No he encontrado números válidos. Pégame algo como:\n"
                "<code>+34600111222\n+34600333444</code>\n\nO usa /start para el menú.",
                parse_mode=ParseMode.HTML,
            )
            return

        self._pending[chat_id] = valid
        preview = "\n".join(f"• <code>{html.escape(n)}</code>" for n in valid[:20])
        extra = f"\n…y {len(valid) - 20} más" if len(valid) > 20 else ""
        warn = f"\n\n⚠️ Ignorados {len(invalid)} valores no válidos." if invalid else ""
        cli = self.locutions.active("cliente")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📞 Llamar", callback_data="launch"),
            InlineKeyboardButton("❌ Cancelar", callback_data="discard"),
        ]])
        await message.reply_text(
            f"He detectado <b>{len(valid)}</b> número(s):\n{preview}{extra}{warn}\n\n"
            f"🎙 Locución: <b>{html.escape(cli.name) if cli else '—'}</b>\n"
            "¿Lanzo las llamadas?",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )

    # ----------------------------------------------------------------- audio upload
    async def on_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id

        file_id = name = None
        if message.audio:
            file_id, name = message.audio.file_id, message.audio.file_name
        elif message.voice:
            file_id, name = message.voice.file_id, None
        elif message.document:
            file_id, name = message.document.file_id, message.document.file_name
        if not file_id:
            return

        if message.caption:
            name = message.caption.strip()
        if not name:
            name = "Locución"
        name = os.path.splitext(name)[0]

        await message.reply_text("⬇️ Descargando audio…")
        tg_file = await context.bot.get_file(file_id)
        tmp_path = os.path.join(tempfile.gettempdir(), f"upload-{file_id[:16]}")
        await tg_file.download_to_drive(custom_path=tmp_path)

        self._state[chat_id] = {"await": "uprole", "path": tmp_path, "name": name}
        await message.reply_text(
            f"¿Para qué rol es «{html.escape(name)}»?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👤 Cliente", callback_data="uprole:cliente"),
                InlineKeyboardButton("🎧 Agente", callback_data="uprole:agente"),
            ]]),
            parse_mode=ParseMode.HTML,
        )

    async def _finish_audio_upload(self, query, context, chat_id: int, role: str) -> None:
        state = self._state.pop(chat_id, None)
        if not state or "path" not in state:
            await query.edit_message_text("No hay ningún audio pendiente. Súbelo de nuevo.")
            return
        await query.edit_message_text("⏳ Convirtiendo audio…")
        try:
            loc = await self.locutions.add_from_file(state["path"], state["name"], role)
            await self.locutions.set_active(loc.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Audio conversion failed")
            await query.edit_message_text(f"❌ No pude convertir el audio: {html.escape(str(exc))}")
            return
        finally:
            try:
                os.remove(state["path"])
            except OSError:
                pass
        await query.edit_message_text(
            f"✅ Locución «{html.escape(loc.name)}» añadida y activada para {ROLE_LABEL[role]}.",
            reply_markup=self._loc_menu(),
        )

    # ----------------------------------------------------------------- agents / QR
    async def _send_agent_qr(self, query, context, chat_id: int, agent_id: str) -> None:
        await query.answer()
        await self._deliver_agent(context, chat_id, agent_id, created=False)

    async def _deliver_agent(self, context, chat_id: int, agent_id: str, created: bool) -> None:
        agent = self.agents.get(agent_id)
        if not agent:
            await context.bot.send_message(chat_id, "Ese agente ya no existe.")
            return
        url = self._prov_url(agent.token)
        caption = (
            (f"✅ Agente «{html.escape(agent.name)}» creado.\n\n" if created else
             f"👤 Agente «{html.escape(agent.name)}»\n\n")
            + "<b>Credenciales SIP</b> (Zoiper/PortSIP):\n"
            f"• Servidor: <code>{html.escape(self._sip_host())}</code>\n"
            f"• Usuario: <code>{html.escape(agent.sip_user)}</code>\n"
            f"• Contraseña: <code>{html.escape(agent.sip_password)}</code>\n"
            "• Transporte: UDP\n\n"
            "📲 O escanea el QR en Zoiper («Iniciar sesión con QR»):"
        )
        png = qr.make_png(url)
        await context.bot.send_photo(chat_id, photo=png, caption=caption, parse_mode=ParseMode.HTML)

    # ----------------------------------------------------------------- campaign launch
    async def _launch(self, query, context, chat_id: int) -> None:
        numbers = self._pending.pop(chat_id, None)
        if not numbers:
            await query.edit_message_text("La lista ha caducado, vuelve a pegarla.")
            return
        if self.dialer.is_busy:
            await query.edit_message_text("Ya hay una campaña en curso. Usa /status.")
            return
        campaign = self.dialer.start_campaign(numbers, chat_id)
        await query.edit_message_text(
            f"🚀 Campaña <code>{campaign.id}</code> lanzada con {len(numbers)} llamadas.",
            parse_mode=ParseMode.HTML,
        )
        status_msg = await context.bot.send_message(chat_id, self._render_status(campaign), parse_mode=ParseMode.HTML)
        asyncio.create_task(self._watch_campaign(context, campaign, status_msg.message_id))

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
                    await context.bot.edit_message_text(text, chat_id=campaign.chat_id, message_id=message_id, parse_mode=ParseMode.HTML)
                except Exception:  # noqa: BLE001
                    pass
            if campaign.completed:
                break
        await context.bot.send_message(campaign.chat_id, self._render_report(campaign), parse_mode=ParseMode.HTML)

    # ----------------------------------------------------------------- renderers
    def _render_status(self, campaign: Campaign) -> str:
        s = campaign.snapshot()
        state = "✅ finalizada" if s["completed"] else ("🛑 cancelando" if s["cancelled"] else "📞 en curso")
        return (
            f"<b>Campaña {campaign.id}</b> — {state}\n"
            f"Progreso: <b>{s['done']}/{s['total']}</b>\n"
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

    def _render_history(self) -> str:
        if not self.dialer.history:
            return "📋 <b>Historial</b>\n\nAún no hay campañas finalizadas."
        lines = ["📋 <b>Historial</b> (últimas)"]
        for c in reversed(self.dialer.history[-10:]):
            s = c.snapshot()
            lines.append(f"• <code>{c.id}</code> — {s['total']} llamadas · ✅ {s['success']} pulsaron 1")
        return "\n".join(lines)

    def _render_config(self) -> str:
        c = self.cfg
        return (
            "⚙️ <b>Configuración</b>\n"
            f"• Trunk SIP: <code>{html.escape(c.sip_endpoint)}</code>\n"
            f"• Caller ID: <code>{html.escape(c.caller_id)}</code>\n"
            f"• Transferencia a: <code>{html.escape(c.agent_display)}</code>\n"
            f"• Agentes registrables: <code>{len(self.agents.list())}</code>\n"
            f"• Llamadas simultáneas: <code>{c.max_concurrent_calls}</code>\n"
            f"• Timeout llamada: <code>{c.call_timeout}s</code>"
        )

    # ----------------------------------------------------------------- wiring
    def build(self) -> Application:
        app = (
            Application.builder()
            .token(self.cfg.telegram_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("menu", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, self.on_audio))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    async def _post_init(self, app: Application) -> None:
        logger.info("Preparing default TTS prompt…")
        await ensure_prompt(self.cfg.message_text, self.cfg.tts_lang, self.cfg.sounds_dir, self.cfg.sound_name)
        await self.locutions.register_default(self.cfg.sound_name)
        if self.cfg.agent_mode == "sip":
            await self.agents.ensure_seed(self.cfg.seed_agent_user, self.cfg.seed_agent_password)
        logger.info("Starting provisioning server…")
        await self.provisioning.start()
        logger.info("Connecting to ARI at %s…", self.cfg.ari_rest_url)
        await self.ari.connect(self.dialer.handle_event)
        await self.ari.wait_until_ready()
        logger.info("Bot ready.")

    async def _post_shutdown(self, app: Application) -> None:
        await self.ari.close()
        await self.provisioning.stop()


def main() -> None:
    cfg = load_config()
    BotApp(cfg).build().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
