"""TelegramBot: long-polling command interface + push-alert dispatcher.

Owns two asyncio tasks — a getUpdates poll loop and an alert dispatcher fed by a
bounded queue — both started/stopped with the FastAPI lifespan. The scanner
stays Telegram-unaware: the bot subscribes via ``scanner.on_signal`` with a
cheap synchronous callback that only filters + ``queue.put_nowait`` (no network
on the scan hot path). See ``app/telegram`` spec for the full design.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from ..config import Config
from ..models import Signal
from . import alerts as alert_mod
from . import commands as cmd_mod
from .api import TelegramAPI, TelegramAPIError
from .format import h

log = logging.getLogger("memeradar.telegram")

CONTROL_COMMANDS = {"pause", "resume", "scan", "reset"}
MAX_MSG = 4096
REFUSAL_COOLDOWN = 30.0  # seconds; throttles replies to unauthorized chats


class TelegramBot:
    def __init__(self, cfg: Config, scanner, db):
        self.cfg = cfg
        self.scanner = scanner
        self.db = db
        self.api = TelegramAPI(cfg.telegram_bot_token, cfg.telegram_poll_timeout_seconds)
        self.username = ""
        self._offset: int | None = None
        self._stopped = False
        self._poll_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=256)
        self._suppressed = 0
        self._rate = alert_mod.AlertRateState(
            cfg.telegram_alert_cooldown_seconds, cfg.telegram_alert_max_per_minute)
        self._mutes: dict[str, float] = {}     # symbol -> expiry ts (inf = forever)
        self._reset_pending: dict[int, float] = {}  # chat_id -> ts (2-step reset)
        self._refused_at: dict[int, float] = {}

    # ---------------- lifecycle ---------------- #
    async def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        me = await self.api.get_me()
        self.username = me.get("username", "")
        log.info("Telegram bot @%s online", self.username)
        try:
            await self.api.delete_webhook(drop_pending=False)
        except TelegramAPIError as e:
            log.warning("deleteWebhook: %s", e)
        # resume from persisted offset; cold start drops >24h backlog (offset=-1)
        self._offset = self.db.kv_get("tg_update_offset", -1)
        self.scanner.on_signal(self._enqueue_signal)
        self._stopped = False
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        await self._broadcast("✅ <b>MemeRadar</b> bot online. /help for commands.",
                              silent=True)

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._poll_task, self._dispatch_task):
            if task:
                task.cancel()
        for task in (self._poll_task, self._dispatch_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._offset is not None:
            self.db.kv_set("tg_update_offset", self._offset)
        await self.api.close()

    # ---------------- poll loop ---------------- #
    async def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stopped:
            try:
                updates = await self.api.get_updates(
                    self._offset, self.cfg.telegram_poll_timeout_seconds)
                for upd in updates:
                    try:
                        await self._handle_update(upd)
                    except Exception:  # noqa: BLE001 — one bad update must not wedge polling
                        log.exception("error handling update")
                    self._offset = max(self._offset or 0, upd["update_id"] + 1)
                if updates:
                    self.db.kv_set("tg_update_offset", self._offset)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except TelegramAPIError as e:
                if e.status == 409:
                    log.error("Telegram 409 Conflict — another poller/webhook is "
                              "consuming updates for this token. Backing off.")
                elif e.status == 429 and e.retry_after:
                    await asyncio.sleep(e.retry_after)
                    continue
                else:
                    log.warning("getUpdates error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            except Exception as e:  # noqa: BLE001
                log.warning("poll loop error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    # ---------------- alert path ---------------- #
    def _active_mutes(self) -> set[str]:
        now = time.time()
        live = {sym for sym, exp in self._mutes.items() if exp > now}
        if len(live) != len(self._mutes):
            self._mutes = {s: self._mutes[s] for s in live}
        return live

    def _enqueue_signal(self, sig: Signal) -> None:
        """Synchronous, cheap, no I/O — safe on the scanner's hot path."""
        try:
            if not alert_mod.should_alert(sig, self.cfg, self._active_mutes()):
                return
            if not self._rate.passes_cooldown(sig):
                return
            self._queue.put_nowait(sig)
        except asyncio.QueueFull:
            self._suppressed += 1
        except Exception:  # noqa: BLE001 — never raise back into the scanner
            pass

    async def _dispatch_loop(self) -> None:
        while not self._stopped:
            try:
                first = await self._queue.get()
                batch = [first]
                deadline = time.time() + self.cfg.telegram_alert_batch_seconds
                while len(batch) < 20:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                    except asyncio.TimeoutError:
                        break
                # cap outbound rate; coalesced batch = one message
                while not self._rate.take_token():
                    await asyncio.sleep(1.0)
                suppressed, self._suppressed = self._suppressed, 0
                silent = all(s.severity == "info" for s in batch)
                await self._broadcast(alert_mod.format_batch(batch, suppressed),
                                      silent=silent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("dispatch loop error")
                await asyncio.sleep(1.0)

    # ---------------- sending ---------------- #
    async def _broadcast(self, text: str, silent: bool = False) -> None:
        for chat_id in self.cfg.telegram_chat_ids:
            await self._send(chat_id, text, silent=silent)

    async def _send(self, chat_id: int, text: str, silent: bool = False) -> None:
        for chunk in self._chunk(text):
            await self._send_chunk(chat_id, chunk, silent)
            await asyncio.sleep(0.05)  # gentle per-chat spacing

    @staticmethod
    def _chunk(text: str) -> list[str]:
        if len(text) <= MAX_MSG:
            return [text]
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > MAX_MSG:
                if cur:
                    chunks.append(cur)
                cur = line[:MAX_MSG]
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        return chunks

    async def _send_chunk(self, chat_id: int, text: str, silent: bool,
                          _retry: bool = True) -> None:
        try:
            await self.api.send_message(chat_id, text, silent=silent)
        except TelegramAPIError as e:
            if e.status == 429 and e.retry_after and _retry:
                await asyncio.sleep(e.retry_after)
                await self._send_chunk(chat_id, text, silent, _retry=False)
            elif e.status == 400 and "parse" in e.description.lower() and _retry:
                # entity parse failure — resend as plain text so nothing is lost
                try:
                    await self.api._call("sendMessage", {
                        "chat_id": chat_id, "text": text,
                        "disable_web_page_preview": True})
                except TelegramAPIError as e2:
                    log.warning("send fallback failed (%s): %s", chat_id, e2)
            else:
                log.warning("send to %s failed: %s", chat_id, e)

    # ---------------- update handling ---------------- #
    async def _handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        text = msg.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        parts = text.split()
        verb = parts[0][1:].split("@")[0].lower()  # strip leading / and @botname
        args = parts[1:]
        verb = cmd_mod.ALIASES.get(verb, verb)

        # /start is the only command answerable by anyone (onboarding)
        if verb == "start":
            await self._cmd_start(chat_id)
            return

        if chat_id not in self.cfg.telegram_chat_ids:
            await self._refuse(chat_id, msg)
            return

        is_admin = chat_id in self.cfg.telegram_admin_chat_ids
        try:
            if verb == "help":
                await self._send(chat_id, self._help_text(is_admin))
            elif verb == "alerts":
                await self._send(chat_id, self._cmd_alerts(args))
            elif verb in CONTROL_COMMANDS:
                if not is_admin:
                    await self._send(chat_id, "⛔ Admin only.")
                    log.warning("unauthorized control '%s' from chat %s", verb, chat_id)
                else:
                    await self._handle_control(chat_id, verb, args)
            elif verb in cmd_mod.READ_HANDLERS:
                reply = cmd_mod.READ_HANDLERS[verb](self.scanner, self.db, self.cfg, args)
                await self._send(chat_id, reply)
            else:
                await self._send(chat_id, "Unknown command. /help")
        except Exception:  # noqa: BLE001 — a handler bug must not kill the poll loop
            log.exception("command '%s' failed", verb)
            await self._send(chat_id, "⚠️ Command failed.")

    async def _refuse(self, chat_id: int, msg: dict[str, Any]) -> None:
        now = time.time()
        if now - self._refused_at.get(chat_id, 0.0) < REFUSAL_COOLDOWN:
            return
        self._refused_at[chat_id] = now
        user = (msg.get("from") or {}).get("username", "?")
        log.warning("unauthorized chat %s (@%s)", chat_id, user)
        await self._send(chat_id, f"Not authorized. Your chat id is <code>{chat_id}</code>.")

    async def _cmd_start(self, chat_id: int) -> None:
        if chat_id in self.cfg.telegram_chat_ids:
            role = "admin" if chat_id in self.cfg.telegram_admin_chat_ids else "reader"
            st = self.scanner.snapshot()["status"]
            await self._send(chat_id,
                f"👋 <b>MemeRadar</b> — you are recognized (<b>{role}</b>).\n"
                f"Desk {'paused' if st['paused'] else 'running'} · scan #{st['scan_count']}"
                f" · {h(st['chain'])}.\n/help for commands.")
        else:
            # reveal nothing but the id, so the operator can allowlist it
            now = time.time()
            if now - self._refused_at.get(chat_id, 0.0) >= REFUSAL_COOLDOWN:
                self._refused_at[chat_id] = now
                await self._send(chat_id,
                    f"Your chat id is <code>{chat_id}</code>.\nAsk the operator to add "
                    "it to <code>TELEGRAM_CHAT_IDS</code>.")

    def _help_text(self, is_admin: bool) -> str:
        lines = ["<b>MemeRadar commands</b>"]
        for usage, desc, control in cmd_mod.HELP:
            if control and not is_admin:
                continue
            lines.append(f"{usage} — {desc}")
        return "\n".join(lines)

    # ---------------- /alerts ---------------- #
    def _cmd_alerts(self, args: list[str]) -> str:
        if not args:
            mutes = ", ".join(sorted(self._active_mutes())) or "none"
            return ("<b>🔔 Alerts</b>\n"
                    f"master: <b>{'on' if self.cfg.telegram_alerts else 'off'}</b>\n"
                    f"kinds: {h(', '.join(self.cfg.telegram_alert_kinds) or 'none')}\n"
                    f"min severity: {h(self.cfg.telegram_alert_min_severity)}\n"
                    f"muted: {h(mutes)}\n"
                    "<i>/alerts on|off · /alerts &lt;kind&gt; on|off · "
                    "/alerts mute &lt;SYM&gt; [min] · /alerts unmute &lt;SYM&gt;</i>")
        sub = args[0].lower()
        if sub in ("on", "off"):
            self.cfg.telegram_alerts = (sub == "on")
            return f"Alerts master switch: <b>{sub}</b>."
        if sub == "mute" and len(args) >= 2:
            sym = args[1].upper()[:16]
            mins = 0
            if len(args) >= 3:
                try:
                    mins = max(0, int(args[2]))
                except ValueError:
                    mins = 0
            self._mutes[sym] = time.time() + mins * 60 if mins else float("inf")
            return f"Muted <b>{h(sym)}</b>" + (f" for {mins}m." if mins else " until unmuted.")
        if sub == "unmute" and len(args) >= 2:
            self._mutes.pop(args[1].upper()[:16], None)
            return f"Unmuted <b>{h(args[1].upper()[:16])}</b>."
        # per-kind toggle: /alerts <kind> on|off
        if len(args) >= 2 and args[1].lower() in ("on", "off"):
            kind = sub
            kinds = list(self.cfg.telegram_alert_kinds)
            if args[1].lower() == "on" and kind not in kinds:
                kinds.append(kind)
            elif args[1].lower() == "off" and kind in kinds:
                kinds.remove(kind)
            self.cfg.telegram_alert_kinds = kinds
            return f"Alerts for <b>{h(kind)}</b>: <b>{args[1].lower()}</b>."
        return "Usage: /alerts · on|off · &lt;kind&gt; on|off · mute &lt;SYM&gt; [min] · unmute &lt;SYM&gt;"

    # ---------------- control commands ---------------- #
    async def _handle_control(self, chat_id: int, verb: str, args: list[str]) -> None:
        if verb == "pause":
            self.scanner.paused = True
            log.warning("PAUSE by chat %s", chat_id)
            await self._send(chat_id, "⏸ Paused — no new entries (exits still run).")
        elif verb == "resume":
            self.scanner.paused = False
            log.warning("RESUME by chat %s", chat_id)
            await self._send(chat_id, "▶️ Resumed — entries re-enabled.")
        elif verb == "scan":
            if self.scanner.is_scanning:
                await self._send(chat_id, "A scan is already in progress.")
                return
            await self._send(chat_id, "🔄 Scanning…")
            await self.scanner.scan_once()
            await self._send(chat_id, f"✅ Done — scan #{self.scanner.scan_count}.")
        elif verb == "reset":
            await self._handle_reset(chat_id, args)

    async def _handle_reset(self, chat_id: int, args: list[str]) -> None:
        configured = self.cfg.telegram_reset_token
        if configured:
            supplied = args[0] if args else ""
            if not (supplied and secrets.compare_digest(supplied, configured)):
                log.warning("RESET denied (bad/absent token) by chat %s", chat_id)
                await self._send(chat_id, "⛔ Usage: <code>/reset &lt;reset_token&gt;</code>")
                return
        else:
            # no token configured: require an explicit two-step confirm
            now = time.time()
            if not (args and args[0].lower() == "confirm"
                    and now - self._reset_pending.get(chat_id, 0.0) < 60):
                self._reset_pending[chat_id] = now
                await self._send(chat_id, "⚠️ This wipes the paper book. Send "
                                 "<code>/reset confirm</code> within 60s to proceed.")
                return
            self._reset_pending.pop(chat_id, None)
        self.scanner.trader.reset()
        self.scanner.trader.record_equity_point()
        log.warning("RESET executed by chat %s", chat_id)
        await self._send(chat_id, "♻️ Paper portfolio reset to "
                         f"{self.cfg.starting_balance_usd:,.0f}.")
