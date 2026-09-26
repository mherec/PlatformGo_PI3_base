"""GPIO via gpiozero (lgpio pin factory). Pins are defined in config["gpio"]["pins"]:
  {"name": "btn1",   "pin": 17, "mode": "in",  "pull": "up"|"down"|"none", "bounce_ms": 50}
  {"name": "relay1", "pin": 27, "mode": "out", "active_high": true, "initial": 0}
Reserved (used by other functions): 8-11 (SPI0: LED on 10), 14/15 (mini UART)."""
from .base import Worker

RESERVED = {8, 9, 10, 11, 14, 15}


class GpioWorker(Worker):
    name = "gpio"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.devices = {}   # name -> (mode, gpiozero device)
        self.values = {}    # name -> 0/1, cached so the web layer never touches hardware

    def setup(self):
        import gpiozero
        for p in self.config.section("gpio")["pins"]:
            name, pin, mode = p["name"], int(p["pin"]), p.get("mode", "in")
            if pin in RESERVED:
                raise ValueError(f"GPIO{pin} ({name}) is reserved for SPI/UART")
            if mode == "in":
                pull = p.get("pull", "up")
                dev = gpiozero.Button(
                    pin, pull_up={"up": True, "down": False}.get(pull),
                    active_state=None if pull in ("up", "down") else True,
                    bounce_time=p.get("bounce_ms", 50) / 1000)
                # gpiozero calls these from its own thread -> hub.emit is thread-safe
                dev.when_pressed = lambda n=name: self._input(n, 1)
                dev.when_released = lambda n=name: self._input(n, 0)
                self.values[name] = int(dev.is_pressed)
            elif mode == "out":
                dev = gpiozero.DigitalOutputDevice(
                    pin, active_high=p.get("active_high", True),
                    initial_value=bool(p.get("initial", 0)))
                self.values[name] = int(dev.value)
            else:
                raise ValueError(f"{name}: mode must be 'in' or 'out'")
            self.devices[name] = (mode, dev)

    def teardown(self):
        for _, dev in self.devices.values():
            dev.close()
        self.devices.clear()
        self.values.clear()

    def _input(self, name, value):
        self.values[name] = value
        self.hub.emit("gpio.input", name=name, value=value)

    def handle(self, cmd, args):
        name = args.get("name")
        if name not in self.devices:
            raise ValueError(f"unknown pin {name!r}")
        mode, dev = self.devices[name]
        if mode != "out":
            raise ValueError(f"{name} is an input")
        if cmd == "write":
            dev.value = bool(args["value"])
        elif cmd == "toggle":
            dev.toggle()
        else:
            super().handle(cmd, args)
        self.values[name] = int(dev.value)
        self.hub.emit("gpio.output", name=name, value=self.values[name])

    def status(self):
        return {**super().status(), "values": dict(self.values),
                "pins": self.config.section("gpio")["pins"]}
