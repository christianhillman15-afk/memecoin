"""Thin async wrapper over the Telegram Bot HTTP API (long polling).

Centralises the base URL, JSON envelope parsing (`ok` / `result` /
`error_code` / `description` / `parameters.retry_after`) and raises a typed
:class:`TelegramAPIError` the bot can branch on (notably 429 retry_after and
409 conflict). Outbound HTTPS reuses the same trust_env + CA-bundle pattern as
the rest of the app so it works through a proxy; TLS verification is never
disabled.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("memeradar.telegram.api")


def _ca_bundle() -> Any:
    return os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True


class TelegramAPIError(Exception):
    def __init__(self, status: int, description: str, retry_after: int | None = None):
        super().__init__(f"{status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, poll_timeout: int = 25):
        self._base = f"https://api.telegram.org/bot{token}"
        # read timeout must exceed the long-poll window or every getUpdates
        # would ReadTimeout and look like an outage
        self._client = httpx.AsyncClient(
            trust_env=True, verify=_ca_bundle(),
            timeout=httpx.Timeout(connect=10.0, read=poll_timeout + 10.0,
                                  write=10.0, pool=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    read_timeout: float | None = None) -> Any:
        # ALWAYS bounded: a non-poll call (read_timeout None) gets a 20s read,
        # getUpdates passes its long-poll window. Never pass timeout=None (that
        # would DISABLE httpx timeouts and a stalled send could hang forever).
        timeout = httpx.Timeout(
            connect=10.0, read=20.0 if read_timeout is None else read_timeout,
            write=10.0, pool=10.0)
        try:
            r = await self._client.post(
                f"{self._base}/{method}", json=payload or {}, timeout=timeout)
        except httpx.HTTPError as e:
            raise TelegramAPIError(0, f"transport error: {e}") from e
        try:
            body = r.json()
        except ValueError:
            raise TelegramAPIError(r.status_code, f"non-JSON response: {r.text[:120]}")
        if body.get("ok"):
            return body.get("result")
        params = body.get("parameters") or {}
        raise TelegramAPIError(
            body.get("error_code", r.status_code),
            body.get("description", "unknown error"),
            params.get("retry_after"),
        )

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def delete_webhook(self, drop_pending: bool = False) -> Any:
        return await self._call("deleteWebhook", {"drop_pending_updates": drop_pending})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload = {"timeout": timeout, "limit": 100, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        # allow the server's long-poll to run the full window before our read trips
        return await self._call("getUpdates", payload, read_timeout=timeout + 10.0) or []

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = "HTML",
                           disable_preview: bool = True,
                           silent: bool = False) -> Any:
        payload = {"chat_id": chat_id, "text": text,
                   "disable_web_page_preview": disable_preview,
                   "disable_notification": silent}
        if parse_mode:  # omit for the plain-text fallback
            payload["parse_mode"] = parse_mode
        return await self._call("sendMessage", payload)
