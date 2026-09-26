"""Single action dispatcher shared by REST, WebSocket, BLE and the cloud link.

Actions only enqueue work for hardware workers (worker.submit) - they never
block the event loop. Result = "queued" acknowledgement; the actual outcome
arrives later as a hub event (e.g. leds.state, uart.tx, audio.done)."""
import asyncio


class Actions:
    def __init__(self, workers: dict, config, loop):
        self.w = workers
        self.config = config
        self.loop = loop
        self.handlers = {
            "leds.set": self._leds_set,           # {effect, color, pixels, speed}
            "gpio.write": self._submit("gpio", "write"),    # {name, value}
            "gpio.toggle": self._submit("gpio", "toggle"),  # {name}
            "uart.write": self._submit("uart", "write"),    # {data, eol} | {hex}
            "audio.play": self._submit("audio", "play"),    # {name, enqueue}
            "audio.stop": self._submit("audio", "stop"),
            "audio.volume": self._submit("audio", "volume"),  # {percent}
            "ble.disconnect": self._ble_disconnect,  # {address}
            "config.update": self._config_update,    # {patch}
            "status": self._status,
        }

    def names(self):
        return sorted(self.handlers)

    async def run(self, name: str, params: dict):
        if name not in self.handlers:
            raise KeyError(f"unknown action {name!r}")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        return await self.handlers[name](params)

    def run_threadsafe(self, name, params):
        """For other threads (BLE): returns a concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(self.run(name, params), self.loop)

    # --- handlers ------------------------------------------------------------
    def _submit(self, worker, cmd):
        async def handler(params):
            self.w[worker].submit(cmd, **params)
            return {"queued": f"{worker}.{cmd}"}
        return handler

    async def _leds_set(self, params):
        self.w["leds"].submit("set", **params)
        return {"queued": "leds.set"}

    async def _ble_disconnect(self, params):
        ble = self.w["ble"]
        if not ble.available:
            raise RuntimeError("ble unavailable")
        fut = asyncio.run_coroutine_threadsafe(ble.disconnect(params["address"]), ble.loop)
        await asyncio.wrap_future(fut)
        return {"disconnected": params["address"]}

    async def _config_update(self, params):
        # Config.update calls listeners which only enqueue reconfigure commands - cheap.
        return self.config.update(params.get("patch", {}))

    async def _status(self, params):
        return {name: w.status() for name, w in self.w.items()}
