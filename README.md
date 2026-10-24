# android-wol-gateway

Remote Wake-on-LAN gateway that runs on an Android phone. You send `/pc` to a
Telegram bot from anywhere; the phone receives it via long polling and sends a
UDP Magic Packet to the home PC on the local network. No open ports, no web
service, no app server: the phone polls Telegram's API as an ordinary client.

```
iPhone --(any network)--> Telegram Bot API --(long polling)--> Android/Termux
                                                                |
                                                                v
                                              UDP Magic Packet (broadcast)
                                                                |
                                                                v
                                                        sleeping PC wakes
```

The gateway is a single Python file (`bot.py`) run under
[Termux](https://termux.dev) on Android. Project goals: one-device
infrastructure (no always-on PC or NAS needed for the relay), minimal
dependencies, no secrets in code or logs.

## What was actually verified

These are real, measured results against my hardware (Redmi Note 12, Windows
11 desktop, home router):

- Telegram command delivery from the iPhone to the phone-gateway: works
  (tested over home Wi-Fi; the gateway polls outbound, so inbound
  connectivity to the phone is not needed).
- Magic Packet sent from the phone over LAN UDP broadcast: confirmed
  independently on the PC side (packet capture on the NIC shows
  `phone.ip → broadcast.9 UDP length 102`).
- PC wake-up from S3 sleep: 8 seconds from sending the packet to the PC being
  reachable.
- Automatic start of the gateway after rebooting the phone: works with
  Termux:Boot.

Your results will differ if your router blocks broadcast frames, if the PC
network card/BIOS does not enable Wake-on-LAN, or if the phone's power
management kills background processes. See [Limitations](#limitations).

## Files

- `bot.py` — the gateway itself (Telegram handlers, config validation,
  status probe, self-diagnostics).
- `.env.example` — configuration template; copy to `.env` and fill in.
- `setup-termux.sh` — one-shot installer for a fresh Termux environment.
- `termux-boot/start-gateway.sh` — boot script for Termux:Boot with a
  restart-at-exit loop.
- `tests/` — unit tests (config parsing, packet format, no-token logging).

## Configuration (`.env`)

Copy `.env.example` to `.env` and set:

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather. Kept only in `.env`. |
| `TELEGRAM_USER_ID` | Your numeric Telegram ID; the bot ignores everyone else. |
| `PC_MAC_ADDRESS` | MAC of the PC's **wired** NIC. Accepts `:` or `-` separators. |
| `PC_LOCAL_IP` | PC IP — used by `/status` only, not by WoL itself. |
| `PC_STATUS_PORTS` | TCP ports probed by `/status` (default `3389,445`). |
| `WOL_BROADCAST_ADDR` | `255.255.255.255` or a subnet broadcast; default global. |
| `WOL_PORT` | UDP port of the Magic Packet (default `9`). |
| `WAKE_COOLDOWN_SECONDS` | Anti-double-tap guard between `/pc` commands. |
| `WAKING_TIMEOUT_SECONDS` | How long after `/pc` a slow boot reports as "waking up" instead of "offline". |
| `PING_TIMEOUT_SECONDS` | Timeout for the ping check in `/status`. |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

## Android setup (Termux)

```bash
# In F-Droid/Play: install "Termux", then start it once.
pkg update -y && pkg upgrade -y
pkg install -y python inetutils   # inetutils adds a ping binary for /status
git clone https://github.com/sisig0at/android-wol-gateway.git
cd android-wol-gateway
python -m pip install -r requirements.txt
cp .env.example .env
nano .env          # fill in your token, user ID and PC NIC MAC
python bot.py --check    # validates .env and does a getMe round-trip
python bot.py            # run in foreground for a first smoke test
```

Grant Termux notification permission when asked; keep the app's notification
running (it holds the foreground service).

### Autostart after phone reboot

1. Install the **Termux:Boot** add-on from the same source as Termux itself
   (F-Droid build with F-Droid, GitHub build with GitHub, Play with Play —
   signatures must match).
2. Open Termux:Boot once after installing (Android requires one manual launch
   so the app may run at boot).
3. Place a boot script:

```bash
mkdir -p ~/.termux/boot
cp termux-boot/start-gateway.sh ~/.termux/boot/
chmod +x ~/.termux/boot/start-gateway.sh
```

4. If the project lives somewhere other than `$HOME/wol-telegram-gateway`,
   export `WOL_PROJECT_DIR` inside that script (point it at the checkout).

The boot script calls `termux-wake-lock` and restarts `python bot.py` with a
15-second delay whenever it exits.

### Keeping the phone alive

Background process management on Android varies widely by vendor. At minimum:

- exclude Termux from battery optimization (both the app and Termux:Boot);
- allow auto-start for Termux in vendor settings (MIUI/HyperOS: Settings app);
- the boot script already acquires a wake lock.

## Windows setup

In BIOS/UEFI, enable Wake-on-LAN (S3 and/or S5 "power on" behavior; the exact
option name depends on the board vendor). In Windows, on the Ethernet
adapter's Power Management tab, tick "Allow this device to wake the
computer", and in the NIC driver's advanced settings enable "Wake on Magic
Packet". Verify a wake-armed NIC with:

```powershell
powercfg /devicequery wake_armed
```

`/status` reliability depends on what the Windows firewall allows. A sleeping
PC cannot answer anything, and even an awake PC may have ICMP echo and all
probed TCP ports blocked. The bot first tries the TCP ports from
`PC_STATUS_PORTS` and then falls back to ping. If both are blocked, an awake
PC can still look "offline" to the bot — open the ports (or ICMP echo)
inbound for the local subnet.

## Telegram bot setup

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, get the token.
2. Get your own numeric ID from [@userinfobot](https://t.me/userinfobot).
3. Paste both into `.env` (the gateway reads them on startup only).

Commands:

- `/pc` — send the Magic Packet (guarded by a cooldown).
- `/status` — probe the PC over the TCP ports and/or ping.
- `/start`, `/help` — basic info.

Messages from any other user are ignored (and logged as rejected).

## Security notes

- The token never enters the git history: it lives only in `.env`, which is
  gitignored. `.env.example` ships with empty placeholders.
- `httpx` request logging (which contains token-bearing URLs) is explicitly
  silenced at WARNING level in `setup_logging()`, so both `gateway.log` and
  `daemon.log` are safe to read.
- The bot replies only to one configured `TELEGRAM_USER_ID`; other users get
  no reply, the attempt is logged.
- If a token ever leaks, revoke it with `/revoke` at @BotFather and update
  `.env`.

## Diagnostics

`python bot.py --check` validates configuration and connectivity:

```
== Configuration check (.env) ==
  [OK] required parameters are set and valid
  ...
== Telegram API check (getMe) ==
  [OK] bot @your_bot answers
== PC availability check (same as /status) ==
  [ONLINE] ...
```

Typical failure modes:

| Symptom | Likely cause |
|---|---|
| `bot.py --check` fails at getMe | wrong token, no internet on the phone |
| `/status` says offline, PC is on | firewall blocks probed TCP ports and ICMP on PC; or wrong `PC_LOCAL_IP` |
| `/pc` sends, but PC never wakes | wrong NIC MAC (use the wired one), WoL disabled in BIOS/UEFI, router blocks broadcast |
| PC sleeps fine but doesn't wake from S3 | "Wake on Magic Packet" disabled in NIC driver properties |
| Gateway dies after phone reboot | Termux:Boot missing, not started once, or vendor auto-start restrictions |
| `Installing pip is forbidden` in Termux pip wrapper | python-pip missing: `pkg install python-pip`; prefer `python -m pip install ...` |

Logs live in `logs/gateway.log` (per process) and `logs/daemon.log`
(as written by the boot script's restart loop).

## Tests

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

The tests are offline: no Telegram calls, no packets sent, no phone
involved. They cover config validation (missing/invalid variables), the Magic
Packet byte layout, the exact arguments passed to `send_magic_packet`, the
user-ID filter, and that logging never emits the token.

## Limitations

- Tested on one phone (Redmi Note 12, HyperOS, Android 15) and one Windows 11
  desktop. Broadcast behavior and background-process rules vary by vendor and
  Android version.
- Wake-from-S5 ("power on after shutdown") depends on the motherboard and
  power supply, not on this project.
- `/status` health reporting depends on host firewall configuration; the WoL
  itself does not depend on those ports.
- The gateway is reachable only where the phone has internet; Telegram long
  polling is the transport, so no inbound port forwarding is required.

## License

MIT — see [LICENSE](LICENSE).
