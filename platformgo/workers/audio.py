"""Audio on the 3.5 mm AV jack through ALSA (aplay/amixer).
Playback runs as a subprocess, so the worker thread only starts/stops it."""
import shutil
import subprocess
from pathlib import Path

from .base import Worker


class AudioWorker(Worker):
    name = "audio"
    tick = 0.2

    def __init__(self, hub, config, sounds_dir: Path):
        super().__init__(hub, config)
        self.sounds_dir = sounds_dir
        self.proc = None
        self.current = None
        self.queue = []

    def setup(self):
        cfg = self.config.section("audio")
        if not cfg["enabled"]:
            raise RuntimeError("disabled in config")
        if not shutil.which("aplay"):
            raise RuntimeError("aplay not found (apt install alsa-utils)")
        self.cfg = cfg
        self._set_volume(cfg["volume"])

    def teardown(self):
        self._stop()
        self.queue.clear()

    def sounds(self):
        return sorted(p.stem for p in self.sounds_dir.glob("*.wav"))

    def handle(self, cmd, args):
        if cmd == "play":
            path = self._path(args["name"])
            if args.get("enqueue") and self.proc:
                self.queue.append(path)
            else:
                self.queue.clear()
                self._stop()
                self._start(path)
        elif cmd == "stop":
            self.queue.clear()
            self._stop()
        elif cmd == "volume":
            self._set_volume(int(args["percent"]))   # persist via PATCH /api/config if needed
        else:
            super().handle(cmd, args)

    def idle(self):
        if self.proc and self.proc.poll() is not None:
            rc = self.proc.returncode
            err = self.proc.stderr.read().decode(errors="replace").strip() if rc else ""
            self.hub.emit("audio.done", name=self.current, rc=rc, error=err or None)
            self.proc, self.current = None, None
            if self.queue:
                self._start(self.queue.pop(0))

    def _path(self, name):
        p = (self.sounds_dir / f"{name}.wav").resolve()
        if p.parent != self.sounds_dir.resolve() or not p.exists():
            raise ValueError(f"unknown sound {name!r}")
        return p

    def _start(self, path):
        self.proc = subprocess.Popen(["aplay", "-q", "-D", self.cfg["device"], str(path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.current = path.stem
        self.hub.emit("audio.playing", name=self.current)

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.current = None, None

    def _set_volume(self, percent):
        percent = max(0, min(100, percent))
        r = subprocess.run(["amixer", "-q", "-c", self.cfg["mixer_card"], "sset",
                            self.cfg["mixer_control"], f"{percent}%"],
                           capture_output=True, text=True)
        if r.returncode:
            self.log.warning("amixer failed: %s", r.stderr.strip())
        self.volume = percent
        self.hub.emit("audio.volume", percent=percent)

    def status(self):
        return {**super().status(), "playing": self.current,
                "volume": getattr(self, "volume", None), "sounds": self.sounds()}
