# PlatformGo — Raspberry Pi 3 A+ base

DietPi (headless, boot z USB) + usługa Python/aiohttp sterująca sprzętem:
WS2812 (SPI, GPIO10), GPIO (gpiozero/lgpio), mini UART `/dev/ttyS0` (9600), audio AV jack (ALSA),
BLE peripheral (serwer GATT, BlueZ), Wi‑Fi, bezpieczne łącze z zewnętrznym API (HTTPS/WSS, OAuth2 CC lub API key).

## Instalacja na Pi (jako root)

```bash
apt-get install -y git
git clone https://github.com/mherec/PlatformGo_PI3_base.git /opt/platformgo
bash /opt/platformgo/system/setup.sh
reboot
```

Panel: `http://<IP>:8080`

## Aktualizacja

```bash
bash /opt/platformgo/system/deploy.sh
```

## Architektura

```
REST / WebSocket / BLE RX / Cloud WSS
            │
        Actions  ──submit()──►  kolejki workerów (wątki: leds, gpio, uart, audio; ble = własny loop)
            ▲                               │
            └──────────  Hub  ◄──emit()─────┘   (zdarzenia → WS, BLE TX notify, cloud)
```

Web nigdy nie dotyka sprzętu — tylko wrzuca komendy do kolejek. Zdarzenia z wątków trafiają do event loopu
przez `call_soon_threadsafe`. Każdy subskrybent (klient WS, BLE, cloud) ma własną kolejkę.

| Funkcja | Zasób | Uwagi |
|---|---|---|
| WS2812 | SPI0 MOSI GPIO10, `/dev/spidev0.0` | 6.4 MHz, 1 bajt SPI na bit, `spidev.bufsiz=32768` |
| UART | `/dev/ttyS0`, GPIO14/15 | `core_freq=250`, konsola szeregowa wyłączona |
| BLE | PL011 `ttyAMA0` (hciuart) | `ControllerMode = le` |
| Audio | PWM audio → AV jack | wolne, bo LED nie używają PWM |
| GPIO | pozostałe piny | zarezerwowane: 8–11, 14, 15 |

## API

- `GET /api/status`, `GET|PATCH /api/config`
- `POST /api/action/<name>` z JSON `params`
- `GET /ws` — zdarzenia; komendy `{"action": "...", "params": {...}, "id": 1}`

Akcje: `leds.set {effect, color, pixels, speed}`, `gpio.write {name, value}`, `gpio.toggle {name}`,
`uart.write {data} | {hex}`, `audio.play {name}`, `audio.stop`, `audio.volume {percent}`,
`ble.disconnect {address}`, `config.update {patch}`, `status`.

## BLE (Pi = peripheral)

| | UUID |
|---|---|
| Service | `7e5a0001-5047-4f00-9c3a-9d1e2f3a4b5c` |
| RX (write) | `7e5a0002-…` — komenda JSON jak w WS, opcjonalnie `"key"` |
| TX (notify) | `7e5a0003-…` — zdarzenia i odpowiedzi JSON |
| INFO (read) | `7e5a0004-…` |

Grupy (`config.ble.groups`): `{"name", "addresses": [MAC...], "key"}`; `allow_unknown=false` rozłącza
urządzenia spoza list MAC. Telefony używają losowych MAC, dlatego dla nich stosuj `key`.

## Zewnętrzne API

Konfiguracja wyłącznie w `/etc/platformgo/env` (patrz `system/env.example`). Tylko `https://`/`wss://`,
certyfikaty weryfikowane. Serwer może wywoływać akcje (`{"action","params","id"}`), urządzenie wysyła
zdarzenia z prefiksów `CLOUD_FORWARD`.

## Uruchomienie lokalne (bez sprzętu)

```bash
python -m venv .venv && .venv/Scripts/pip install aiohttp pyserial
PG_DATA_DIR=./data .venv/Scripts/python -m platformgo
```
