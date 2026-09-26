"""WS2812 over SPI (MOSI = GPIO10).

Each WS2812 bit is encoded as one SPI byte at ~6.4 MHz (6.25 MHz real with
core_freq=250):  0 -> 0b11000000 (~0.32 us high),  1 -> 0b11111000 (~0.8 us high).
One byte per bit makes the timing immune to inter-byte gaps of the SPI driver.
The PWM peripheral is untouched, so analog audio keeps working."""
import colorsys
import math
import time

from .base import Worker

_BIT0, _BIT1 = 0b11000000, 0b11111000
_LUT = [bytes(_BIT1 if b & (0x80 >> i) else _BIT0 for i in range(8)) for b in range(256)]
_RESET = bytes(300)   # ~380 us low -> latch (WS2812B needs > 280 us)

EFFECTS = ("off", "solid", "pixels", "rainbow", "breathe", "chase")


class LedWorker(Worker):
    name = "leds"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.spi = None
        self.effect = "off"
        self.color = (255, 120, 0)
        self.pixels = []
        self.speed = 1.0
        self._t0 = time.monotonic()

    # --- hardware ------------------------------------------------------------
    def setup(self):
        cfg = self.config.section("leds")
        if not cfg["enabled"]:
            raise RuntimeError("disabled in config")
        import spidev
        self.cfg = cfg
        self.spi = spidev.SpiDev()
        self.spi.open(cfg["spi_bus"], cfg["spi_device"])
        self.spi.max_speed_hz = cfg["spi_hz"]
        self.spi.mode = 0
        self._render()

    def teardown(self):
        if self.spi:
            try:
                self._write([(0, 0, 0)] * self.cfg["count"])
            finally:
                self.spi.close()
                self.spi = None

    def _write(self, colors):
        k = self.cfg["brightness"] / 255
        idx = {"R": 0, "G": 1, "B": 2}
        order = [idx[c] for c in self.cfg["order"].upper()]
        buf = bytearray(b"\x00")
        for c in colors:
            for o in order:
                buf += _LUT[int(c[o] * k) & 0xFF]
        buf += _RESET
        self.spi.writebytes2(buf)

    # --- commands ------------------------------------------------------------
    def handle(self, cmd, args):
        if cmd == "set":
            effect = args.get("effect", self.effect)
            if effect not in EFFECTS:
                raise ValueError(f"effect must be one of {EFFECTS}")
            self.effect = effect
            if "color" in args:
                self.color = _rgb(args["color"])
            if "pixels" in args:
                self.pixels = [_rgb(p) for p in args["pixels"]]
                self.effect = "pixels"
            if "speed" in args:
                self.speed = max(0.05, float(args["speed"]))
            self._t0 = time.monotonic()
            self._render()
            self.hub.emit("leds.state", **self.state())
        else:
            super().handle(cmd, args)

    def state(self):
        return {"effect": self.effect, "color": list(self.color),
                "pixels": [list(p) for p in self.pixels], "speed": self.speed}

    def status(self):
        return {**super().status(), **self.state()}

    # --- rendering -----------------------------------------------------------
    @property
    def tick(self):
        return 0.03 if self.effect in ("rainbow", "breathe", "chase") else 0.5

    def idle(self):
        if self.effect in ("rainbow", "breathe", "chase"):
            self._render()

    def _render(self):
        n = self.cfg["count"]
        t = (time.monotonic() - self._t0) * self.speed
        if self.effect == "off":
            out = [(0, 0, 0)] * n
        elif self.effect == "solid":
            out = [self.color] * n
        elif self.effect == "pixels":
            out = (self.pixels + [(0, 0, 0)] * n)[:n]
        elif self.effect == "rainbow":
            out = [tuple(int(v * 255) for v in colorsys.hsv_to_rgb((t * 0.2 + i / n) % 1, 1, 1))
                   for i in range(n)]
        elif self.effect == "breathe":
            k = (math.sin(t * 2) + 1) / 2
            out = [tuple(int(v * k) for v in self.color)] * n
        else:  # chase
            pos = int(t * 8) % n
            out = [self.color if i == pos else (0, 0, 0) for i in range(n)]
        self._write(out)


def _rgb(v):
    if isinstance(v, str):
        v = v.lstrip("#")
        v = [int(v[i:i + 2], 16) for i in (0, 2, 4)]
    r, g, b = (max(0, min(255, int(x))) for x in v)
    return (r, g, b)
