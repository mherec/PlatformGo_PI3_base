"""BLE peripheral: the Pi advertises a GATT service, devices connect to it.

Runs in its own thread with its own asyncio loop (BlueZ over D-Bus via `bless`),
so BLE traffic never touches the web server loop.

GATT service PlatformGo:
  RX   (write)        - client -> Pi, JSON command: {"action": "leds.set", "params": {...}, "id": 1, "key": "..."}
  TX   (notify, read) - Pi -> client, JSON events (filtered by config ble.notify_prefixes) and command replies
  INFO (read)         - JSON with device name / version

Access control ("groups"): config ble.groups = [{"name", "addresses": [...], "key"}]
  - allow_unknown=False -> centrals whose MAC is not in any group are disconnected
  - if any group defines "key", every RX command must carry a matching "key"
Note: phones use random (private) MACs unless bonded - use "key" for them."""
import asyncio
import json
import logging
import threading

SERVICE_UUID = "7e5a0001-5047-4f00-9c3a-9d1e2f3a4b5c"
RX_UUID = "7e5a0002-5047-4f00-9c3a-9d1e2f3a4b5c"
TX_UUID = "7e5a0003-5047-4f00-9c3a-9d1e2f3a4b5c"
INFO_UUID = "7e5a0004-5047-4f00-9c3a-9d1e2f3a4b5c"
POLL_S = 2.0

log = logging.getLogger("worker.ble")


class BleWorker(threading.Thread):
    name = "ble"

    def __init__(self, hub, config, dispatch):
        """dispatch(action, params) -> concurrent.futures.Future (runs on the main loop)."""
        super().__init__(name="ble", daemon=True)
        self.hub, self.config, self.dispatch = hub, config, dispatch
        self.available, self.error = False, None
        self.connected = {}     # address -> {"name", "group"}
        self.loop = None
        self._rxbuf = bytearray()
        config.on_change(self._config_changed)

    # --- thread / lifecycle ----------------------------------------------------
    def run(self):
        asyncio.run(self._main())

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self._stop.set)

    def _config_changed(self, section, _):
        if section == "ble" and self.loop:
            self.loop.call_soon_threadsafe(self._reconf.set)

    def status(self):
        return {"available": self.available, "error": self.error,
                "connected": [{"address": a, **d} for a, d in self.connected.items()],
                "service_uuid": SERVICE_UUID, "rx_uuid": RX_UUID, "tx_uuid": TX_UUID}

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._stop, self._reconf = asyncio.Event(), asyncio.Event()
        self._events = asyncio.Queue(maxsize=100)
        self.hub.loop.call_soon_threadsafe(self.hub.subscribe, self._on_hub_event)
        while not self._stop.is_set():
            self._reconf.clear()
            try:
                await self._serve()
            except Exception as e:
                self.available, self.error = False, f"{type(e).__name__}: {e}"
                log.warning("unavailable: %s", self.error)
                self.hub.emit("ble.status", **self.status())
                try:   # retry later (e.g. bluetoothd restarted) or on config change
                    await asyncio.wait_for(self._wait_any(), 15)
                except asyncio.TimeoutError:
                    pass

    async def _wait_any(self):
        stop, reconf = asyncio.create_task(self._stop.wait()), asyncio.create_task(self._reconf.wait())
        await asyncio.wait({stop, reconf}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel(), reconf.cancel()

    # --- GATT server -----------------------------------------------------------
    async def _serve(self):
        cfg = self.config.section("ble")
        if not cfg["enabled"]:
            self.available, self.error = False, "disabled in config"
            self.hub.emit("ble.status", **self.status())
            await self._wait_any()
            return
        from bless import BlessServer, GATTAttributePermissions as Perm, \
            GATTCharacteristicProperties as Prop
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        self.cfg = cfg
        server = BlessServer(name=cfg["name"], loop=self.loop)
        server.read_request_func = lambda char, **kw: char.value
        server.write_request_func = self._on_write
        info = json.dumps({"name": cfg["name"], "fw": "platformgo-0.1"}).encode()
        await server.add_new_service(SERVICE_UUID)
        await server.add_new_characteristic(SERVICE_UUID, RX_UUID, Prop.write | Prop.write_without_response,
                                            None, Perm.writeable)
        await server.add_new_characteristic(SERVICE_UUID, TX_UUID, Prop.notify | Prop.read,
                                            bytearray(b"{}"), Perm.readable)
        await server.add_new_characteristic(SERVICE_UUID, INFO_UUID, Prop.read,
                                            bytearray(info), Perm.readable)
        await server.start()
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.server, self.bus = server, bus
        self.available, self.error = True, None
        self.hub.emit("ble.status", **self.status())
        log.info("advertising as %r", cfg["name"])
        poll = asyncio.create_task(self._poll_loop())
        notify = asyncio.create_task(self._notify_loop())
        try:
            await self._wait_any()
        finally:
            poll.cancel(), notify.cancel()
            await server.stop()
            bus.disconnect()
            self.connected.clear()
            self.available = False

    # --- RX: commands from BLE clients ----------------------------------------
    def _on_write(self, char, value, **kw):
        if str(char.uuid).lower() != RX_UUID:
            return
        self._rxbuf += value
        try:
            msg = json.loads(self._rxbuf)       # accept commands split across several writes
        except ValueError:
            if len(self._rxbuf) > 4096:
                self._rxbuf.clear()
            return
        self._rxbuf.clear()
        mid = msg.get("id") if isinstance(msg, dict) else None
        keys = {g["key"] for g in self.cfg["groups"] if g.get("key")}
        if not isinstance(msg, dict) or "action" not in msg:
            return self._reply(mid, error="expected {\"action\": ..., \"params\": {...}}")
        if keys and msg.get("key") not in keys:
            return self._reply(mid, error="bad key")
        group = next((g["name"] for g in self.cfg["groups"] if g.get("key") and g["key"] == msg.get("key")), None)
        self.hub.emit("ble.command", action=msg["action"], group=group)
        fut = self.dispatch(msg["action"], msg.get("params") or {})
        fut.add_done_callback(lambda f: self.loop.call_soon_threadsafe(self._finish, mid, f))

    def _finish(self, mid, fut):
        try:
            self._reply(mid, result=fut.result())
        except Exception as e:
            self._reply(mid, error=str(e))

    def _reply(self, mid, result=None, error=None):
        self._enqueue({"type": "ble.reply", "id": mid, "ok": error is None,
                       **({"error": error} if error else {"result": result})})

    # --- TX: events to BLE clients ---------------------------------------------
    def _on_hub_event(self, event):
        """Called on the main loop - only hands the event over to the BLE loop."""
        if self.available and self.connected and \
                event["type"].startswith(tuple(self.cfg["notify_prefixes"])):
            self.loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event):
        if self._events.full():
            self._events.get_nowait()           # drop oldest, never block
        self._events.put_nowait(event)

    async def _notify_loop(self):
        while True:
            event = await self._events.get()
            data = json.dumps(event, separators=(",", ":")).encode()
            self.server.get_characteristic(TX_UUID).value = bytearray(data)
            self.server.update_value(SERVICE_UUID, TX_UUID)
            await asyncio.sleep(0.02)           # pace notifications

    # --- connected centrals ------------------------------------------------------
    async def _poll_loop(self):
        from dbus_next import Message
        while True:
            reply = await self.bus.call(Message(
                destination="org.bluez", path="/", member="GetManagedObjects",
                interface="org.freedesktop.DBus.ObjectManager"))
            now = {}
            for path, ifaces in reply.body[0].items():
                dev = ifaces.get("org.bluez.Device1")
                if dev and dev.get("Connected") and dev["Connected"].value:
                    addr = dev["Address"].value
                    name = dev["Alias"].value if "Alias" in dev else None
                    now[addr] = {"name": name, "group": self._group_of(addr), "path": path}
            for addr in now.keys() - self.connected.keys():
                info = now[addr]
                if info["group"] is None and not self.cfg["allow_unknown"]:
                    await self.bus.call(Message(destination="org.bluez", path=info["path"],
                                                interface="org.bluez.Device1", member="Disconnect"))
                    self.hub.emit("ble.rejected", address=addr, name=info["name"])
                    now.pop(addr)
                    continue
                self.hub.emit("ble.connected", address=addr, name=info["name"], group=info["group"])
            for addr in self.connected.keys() - now.keys():
                self.hub.emit("ble.disconnected", address=addr)
            self.connected = {a: {"name": d["name"], "group": d["group"]} for a, d in now.items()}
            await asyncio.sleep(POLL_S)

    def _group_of(self, addr):
        for g in self.cfg["groups"]:
            if addr.upper() in (a.upper() for a in g.get("addresses", [])):
                return g["name"]
        return None

    async def disconnect(self, address):
        from dbus_next import Message
        path = "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")
        await self.bus.call(Message(destination="org.bluez", path=path,
                                    interface="org.bluez.Device1", member="Disconnect"))
