"""Base class for hardware workers running in their own thread.

The web layer never touches hardware directly - it only calls submit(),
which puts a command on a queue consumed by the worker thread."""
import logging
import queue
import threading


class Worker(threading.Thread):
    name = "worker"
    tick = 0.5              # seconds between idle() calls when the queue is empty

    def __init__(self, hub, config):
        super().__init__(name=self.name, daemon=True)
        self.hub = hub
        self.config = config
        self.log = logging.getLogger(f"worker.{self.name}")
        self._cmds: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self.available = False
        self.error = None
        config.on_change(self._config_changed)

    # --- API used from the asyncio side -------------------------------------
    def submit(self, cmd: str, **args):
        try:
            self._cmds.put_nowait((cmd, args))
        except queue.Full:
            raise RuntimeError(f"{self.name}: command queue full")

    def stop(self):
        self._stop.set()
        self._cmds.put_nowait(("__stop__", {}))

    def status(self) -> dict:
        return {"available": self.available, "error": self.error}

    # --- to override ---------------------------------------------------------
    def setup(self):
        """Open hardware. Raise to mark the worker unavailable."""

    def teardown(self):
        pass

    def handle(self, cmd: str, args: dict):
        raise ValueError(f"unknown command {cmd}")

    def idle(self):
        """Called every `tick` seconds when there are no commands."""

    def reconfigure(self, section: dict):
        """Called in the worker thread after its config section changed."""
        self.teardown()
        self._open()

    # --- internals -----------------------------------------------------------
    def _config_changed(self, section_name, section):
        if section_name == self.name:
            self._cmds.put_nowait(("__reconfigure__", {"section": section}))

    def _open(self):
        try:
            self.setup()
            self.available, self.error = True, None
        except Exception as e:
            self.available, self.error = False, f"{type(e).__name__}: {e}"
            self.log.warning("unavailable: %s", self.error)
        self.hub.emit(f"{self.name}.status", **self.status())

    def run(self):
        self._open()
        while not self._stop.is_set():
            try:
                cmd, args = self._cmds.get(timeout=self.tick)
            except queue.Empty:
                if self.available:
                    self._safe(self.idle)
                continue
            if cmd == "__stop__":
                break
            if cmd == "__reconfigure__":
                self._safe(self.reconfigure, args["section"])
                continue
            if not self.available:
                self.hub.emit(f"{self.name}.error", cmd=cmd, error=self.error or "unavailable")
                continue
            self._safe(self.handle, cmd, args)
        self._safe(self.teardown)

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            self.log.exception("error in %s", getattr(fn, "__name__", fn))
            self.hub.emit(f"{self.name}.error", error=f"{type(e).__name__}: {e}")
