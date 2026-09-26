"""aiohttp web layer: REST + WebSocket + static panel.
Handlers only read cached state or enqueue actions - no hardware I/O here."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import WSMsgType, web

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"
START = time.time()


def system_info():
    info = {"uptime_app": int(time.time() - START)}
    try:
        info["cpu_temp"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        info["load"] = os.getloadavg()
        mem = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())
        kb = lambda k: int(mem[k].split()[0]) // 1024
        info["mem_mb"] = {"total": kb("MemTotal"), "available": kb("MemAvailable")}
        info["uptime_sys"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, AttributeError, KeyError, ValueError):
        pass
    return info


def make_app(ctx) -> web.Application:
    """ctx: object with .config .actions .workers .cloud"""
    token = os.environ.get("APP_TOKEN")    # optional; empty = open panel in LAN

    @web.middleware
    async def auth_mw(request, handler):
        if token and request.path.startswith(("/api", "/ws")):
            given = request.headers.get("Authorization", "").removeprefix("Bearer ") \
                or request.query.get("token")
            if given != token:
                raise web.HTTPUnauthorized(text="bad token")
        return await handler(request)

    @web.middleware
    async def error_mw(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (KeyError, ValueError, TypeError, RuntimeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    def full_status():
        s = {name: w.status() for name, w in ctx.workers.items()}
        s["cloud"] = ctx.cloud.status()
        s["system"] = system_info()
        return s

    async def status(request):
        return web.json_response(full_status())

    async def get_config(request):
        return web.json_response(ctx.config.all())

    async def patch_config(request):
        return web.json_response(ctx.config.update(await request.json()))

    async def action(request):
        params = await request.json() if request.can_read_body else {}
        result = await ctx.actions.run(request.match_info["name"], params)
        return web.json_response({"ok": True, "result": result})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=200)

        def on_event(event):
            q.put_nowait(event)             # QueueFull -> hub drops this slow client

        async def sender():
            while True:
                await ws.send_json(await q.get())

        await ws.send_json({"type": "hello", "status": full_status(), "actions": ctx.actions.names()})
        ctx.hub.subscribe(on_event)
        send_task = asyncio.create_task(sender())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = {}
                try:
                    m = json.loads(msg.data)
                    result = await ctx.actions.run(m["action"], m.get("params") or {})
                    reply = {"type": "reply", "id": m.get("id"), "ok": True, "result": result}
                except Exception as e:
                    reply = {"type": "reply", "id": m.get("id") if isinstance(m, dict) else None,
                             "ok": False, "error": str(e)}
                await ws.send_json(reply)
        finally:
            ctx.hub.unsubscribe(on_event)
            send_task.cancel()
        return ws

    app = web.Application(middlewares=[error_mw, auth_mw])
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", status),
        web.get("/api/config", get_config),
        web.patch("/api/config", patch_config),
        web.post("/api/action/{name}", action),
        web.get("/ws", ws_handler),
        web.static("/static", STATIC),
    ])
    return app
