"""Mini UART (/dev/ttyS0, GPIO14 TX / GPIO15 RX). Needs core_freq=250 for a stable baud rate."""
import time

from .base import Worker


class UartWorker(Worker):
    name = "uart"
    tick = 0.05             # poll RX 20x per second (9600 baud = ~960 B/s, kernel buffers the rest)
    PARTIAL_FLUSH = 0.5     # emit an unterminated line after this much silence

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.ser = None
        self._buf = bytearray()
        self._last_rx = 0.0
        self.rx_count = self.tx_count = 0

    def setup(self):
        cfg = self.config.section("uart")
        if not cfg["enabled"]:
            raise RuntimeError("disabled in config")
        import serial
        self.cfg = cfg
        self.ser = serial.Serial(cfg["port"], cfg["baudrate"], timeout=0)

    def teardown(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    def idle(self):
        n = self.ser.in_waiting
        if n:
            self._buf += self.ser.read(n)
            self._last_rx = time.monotonic()
            self.rx_count += n
            while b"\n" in self._buf:
                line, _, rest = self._buf.partition(b"\n")
                self._buf = bytearray(rest)
                self._emit(line)
        elif self._buf and time.monotonic() - self._last_rx > self.PARTIAL_FLUSH:
            self._emit(self._buf, partial=True)
            self._buf = bytearray()

    def _emit(self, raw, partial=False):
        raw = bytes(raw).rstrip(b"\r")
        self.hub.emit("uart.rx", text=raw.decode("utf-8", "replace"), hex=raw.hex(), partial=partial)

    def handle(self, cmd, args):
        if cmd != "write":
            return super().handle(cmd, args)
        if "hex" in args:
            data = bytes.fromhex(args["hex"])
        else:
            data = (str(args["data"]) + args.get("eol", self.cfg["eol"])).encode()
        self.ser.write(data)
        self.tx_count += len(data)
        self.hub.emit("uart.tx", hex=data.hex(), bytes=len(data))

    def status(self):
        return {**super().status(), "rx_bytes": self.rx_count, "tx_bytes": self.tx_count}
