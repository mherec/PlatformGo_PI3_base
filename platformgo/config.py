"""Persistent JSON configuration with defaults, atomic save and change callbacks."""
import copy
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    "leds": {
        "enabled": True,
        "count": 5,
        "brightness": 64,          # 0-255, global scaler
        "order": "GRB",            # WS2812 = GRB, some clones RGB
        "spi_bus": 0,
        "spi_device": 0,           # /dev/spidev0.0 -> MOSI = GPIO10
        "spi_hz": 6400000,         # 8 SPI bits per WS bit -> 800 kHz
    },
    "gpio": {
        # [{"name": "btn1", "pin": 17, "mode": "in", "pull": "up"},
        #  {"name": "relay1", "pin": 27, "mode": "out", "active_high": true, "initial": 0}]
        "pins": [],
    },
    "uart": {
        "enabled": True,
        "port": "/dev/ttyS0",
        "baudrate": 9600,
        "eol": "\n",
    },
    "audio": {
        "enabled": True,
        "device": "default",
        "mixer_card": "Headphones",
        "mixer_control": "PCM",
        "volume": 70,
    },
    "ble": {
        "enabled": True,
        "name": "PlatformGo",      # advertised local name
        # Groups of devices allowed to connect (Pi = GATT server / peripheral).
        # [{"name": "remotes", "addresses": ["AA:BB:CC:DD:EE:FF"], "key": "secret"}]
        # addresses: allowlist of MACs (empty = any address)
        # key: if set, every command written to RX must carry {"key": ...}
        "groups": [],
        "allow_unknown": True,     # False -> devices not in any group's addresses are disconnected
        "notify_prefixes": ["gpio.", "uart.", "leds.state", "audio.", "ble.reply"],
    },
}


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._listeners = []
        self._data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                self._data = _merge(DEFAULTS, json.loads(path.read_text("utf-8")))
            except Exception:
                log.exception("Invalid config %s, using defaults", path)

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data.get(name, {}))

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def on_change(self, callback):
        """callback(section_name, new_section_dict) - called from the caller's thread."""
        self._listeners.append(callback)

    def update(self, patch: dict) -> dict:
        unknown = set(patch) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config sections: {sorted(unknown)}")
        with self._lock:
            self._data = _merge(self._data, patch)
            self._save()
            snapshot = copy.deepcopy(self._data)
        for name in patch:
            for cb in self._listeners:
                try:
                    cb(name, snapshot[name])
                except Exception:
                    log.exception("config listener failed for %s", name)
        return snapshot

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        os.replace(tmp, self.path)
