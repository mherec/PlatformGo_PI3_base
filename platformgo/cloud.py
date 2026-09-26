"""Secure bidirectional link to the external API.

  - WSS connection (CLOUD_WS_URL) with Authorization header, heartbeat and
    reconnect with exponential backoff + jitter
  - incoming {"action", "params", "id"} -> Actions -> reply {"id", "ok", "result"|"error"}
  - outgoing: hub events whose type starts with CLOUD_FORWARD prefixes
  - HTTPS helper request() for REST calls (CLOUD_API_URL)
  - auth: CLOUD_AUTH = none | api_key | oauth2  (OAuth2 Client Credentials, token cached
    and refreshed 60 s before expiry, invalidated on 401)
Only https:// and wss:// are accepted; certificates are always verified.
All settings come from the environment (/etc/platformgo/env), never from the repo."""
import asyncio
import json
import logging
import os
import random
import socket
import time

import aiohttp

log = logging.getLogger("cloud")


class Cloud:
    def __init__(self, hub, actions):
        e = os.environ.get
        self.hub, self.actions = hub, actions
        self.ws_url = e("CLOUD_WS_URL", "")
        self.api_url = e("CLOUD_API_URL", "").rstrip("/")
        self.auth = e("CLOUD_AUTH", "none")
        self.api_key = e("CLOUD_API_KEY", "")
        self.api_key_header = e("CLOUD_API_KEY_HEADER", "X-API-Key")
        self.token_url = e("CLOUD_TOKEN_URL", "")
        self.client_id = e("CLOUD_CLIENT_ID", "")
        self.client_secret = e("CLOUD_CLIENT_SECRET", "")
        self.scope = e("CLOUD_SCOPE", "")
        self.audience = e("CLOUD_AUDIENCE", "")
        self.device_id = e("DEVICE_ID", socket.gethostname())
        self.forward = tuple(p for p in e("CLOUD_FORWARD", "gpio.,uart.rx,ble.,audio.done").split(",") if p)
        self._token, self._token_exp = None, 0.0
        self._out: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.connected = False
        self.error = None
        self.session = None
        for url in (self.ws_url, self.api_url, self.token_url):
            if url and not url.startswith(("https://", "wss://")):
                raise ValueError(f"insecure URL refused: {url}")

    @property
    def enabled(self):
        return bool(self.ws_url)

    def status(self):
        return {"enabled": self.enabled, "connected": self.connected, "error": self.error,
                "url": self.ws_url or None, "auth": self.auth}

    # --- auth ------------------------------------------------------------------
    async def headers(self) -> dict:
        h = {"X-Device-Id": self.device_id}
        if self.auth == "api_key":
            h[self.api_key_header] = self.api_key
        elif self.auth == "oauth2":
            h["Authorization"] = f"Bearer {await self._access_token()}"
        return h

    async def _access_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        if self.audience:
            data["audience"] = self.audience
        async with self.session.post(self.token_url, data=data,
                                     auth=aiohttp.BasicAuth(self.client_id, self.client_secret)) as r:
            if r.status != 200:
                raise RuntimeError(f"token endpoint HTTP {r.status}: {(await r.text())[:200]}")
            tok = await r.json()
        self._token = tok["access_token"]
        self._token_exp = time.time() + int(tok.get("expires_in", 3600))
        log.info("OAuth2 token refreshed, expires in %ss", tok.get("expires_in"))
        return self._token

    # --- HTTPS REST ------------------------------------------------------------
    async def request(self, method, path, **kw):
        if not self.api_url:
            raise RuntimeError("CLOUD_API_URL not set")
        for attempt in (1, 2):
            async with self.session.request(method, self.api_url + path,
                                            headers=await self.headers(), **kw) as r:
                if r.status == 401 and self.auth == "oauth2" and attempt == 1:
                    self._token = None          # token revoked/expired early -> retry once
                    continue
                r.raise_for_status()
                return await r.json(content_type=None)

    # --- WebSocket link ----------------------------------------------------------
    def _on_hub_event(self, event):
        if self.connected and event["type"].startswith(self.forward):
            if self._out.full():
                self._out.get_nowait()
            self._out.put_nowait({"event": event})

    async def run(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15))
        if not self.enabled:
            log.info("cloud link disabled (CLOUD_WS_URL not set)")
            return
        self.hub.subscribe(self._on_hub_event)
        backoff = 1
        while True:
            try:
                async with self.session.ws_connect(self.ws_url, headers=await self.headers(),
                                                   heartbeat=30) as ws:
                    self.connected, self.error, backoff = True, None, 1
                    self.hub.emit("cloud.status", **self.status())
                    log.info("connected to %s", self.ws_url)
                    await ws.send_json({"hello": {"device_id": self.device_id,
                                                  "actions": self.actions.names()}})
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                if isinstance(e, aiohttp.WSServerHandshakeError) and e.status == 401:
                    self._token = None
            self.connected = False
            self.hub.emit("cloud.status", **self.status())
            delay = min(60, backoff) * (0.5 + random.random())
            log.warning("cloud link down (%s), retry in %.0fs", self.error, delay)
            await asyncio.sleep(delay)
            backoff *= 2

    async def _pump(self, ws):
        async def sender():
            while True:
                await ws.send_json(await self._out.get())
        send_task = asyncio.create_task(sender())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = None
                try:
                    m = json.loads(msg.data)
                    result = await self.actions.run(m["action"], m.get("params") or {})
                    reply = {"id": m.get("id"), "ok": True, "result": result}
                except Exception as e:
                    reply = {"id": m.get("id") if isinstance(m, dict) else None,
                             "ok": False, "error": str(e)}
                await ws.send_json(reply)
        finally:
            send_task.cancel()

    async def close(self):
        if self.session:
            await self.session.close()
