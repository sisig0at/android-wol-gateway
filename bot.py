#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram -> Wake-on-LAN шлюз для Termux (Android).

Архитектура:
    iPhone -> Telegram Bot API -> (long polling) -> Termux/Android -> WoL Magic Packet -> Windows PC

Особенности:
  * long polling (никаких webhook и открытых входящих портов);
  * принимает команды ТОЛЬКО от одного TELEGRAM_USER_ID (чужим отвечаем молча, пишем в лог);
  * кулдаун между пусками (защита от случайного/повторного запуска);
  * /status учитывает, что пинг выключенного/загружающегося ПК не работает:
    после WoL состояние «просыпается» в течение WAKING_TIMEOUT_SECONDS,
    затем TCP-проверка портов и (если есть) ping;
  * логи: logs/gateway.log (ротация) + stdout;
  * режимы самодиагностики:  python bot.py --check   и   python bot.py --send-wol

Token, ID, MAC и IP хранятся ТОЛЬКО в .env и в исходный код не вшиты.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import urllib.request
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from wakeonlan import send_magic_packet

# --------------------------------------------------------------------------
# Базовые пути и загрузка .env
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG = logging.getLogger("wol-gateway")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def setup_logging(level_name: str) -> None:
    """Логирование в stderr (для logcat/экрана) и в файл с ротацией."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    # httpx logs full request URLs at INFO during polling; bot API URLs contain
    # the bot token. Keep them out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        LOG_DIR / "gateway.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    token: str
    user_id: int
    mac: str
    local_ip: Optional[str]
    status_ports: Tuple[int, ...]
    wol_broadcast: str
    wol_port: int
    cooldown_seconds: int
    waking_timeout_seconds: int
    ping_timeout_seconds: int
    log_level: str


def _env(name: str, default: str = "") -> str:
    # An explicitly empty value counts as unset: fall back to the default.
    return os.environ.get(name, default).strip() or default


def _env_int(name: str, default: int, errors: List[str],
             minimum: int = 0, maximum: int = 65535) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name}='{raw}' — должно быть целое число")
        return default
    if not (minimum <= value <= maximum):
        errors.append(f"{name}={value} — вне диапазона {minimum}..{maximum}")
        return default
    return value


def load_config() -> Tuple[Optional[Config], List[str]]:
    """Читает .env, валидирует. При ошибке возвращает (None, [описания ошибок])."""
    errors: List[str] = []

    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        errors.append("TELEGRAM_BOT_TOKEN пуст — создайте бота у @BotFather и впишите токен в .env")
    elif ":" not in token:
        errors.append("TELEGRAM_BOT_TOKEN выглядит неверно (ожидается формат 123456789:AAAA...)")

    raw_uid = _env("TELEGRAM_USER_ID")
    user_id: Optional[int] = None
    if not raw_uid:
        errors.append("TELEGRAM_USER_ID пуст — узнайте свой ID (например, у @userinfobot) и впишите в .env")
    elif not raw_uid.lstrip("-").isdigit():
        errors.append(f"TELEGRAM_USER_ID='{raw_uid}' — должен быть числом")
    else:
        user_id = int(raw_uid)

    mac = _env("PC_MAC_ADDRESS")
    if not mac:
        errors.append("PC_MAC_ADDRESS пуст — впишите MAC-адрес сетевой карты ПК (проводной Ethernet!)")
    elif not MAC_RE.match(mac):
        errors.append(f"PC_MAC_ADDRESS='{mac}' — неверный формат. Нужно XX:XX:XX:XX:XX:XX или XX-XX-XX-XX-XX-XX")

    local_ip_raw = _env("PC_LOCAL_IP")
    local_ip: Optional[str] = None
    if local_ip_raw:
        try:
            ipaddress.ip_address(local_ip_raw)
            local_ip = local_ip_raw
        except ValueError:
            errors.append(f"PC_LOCAL_IP='{local_ip_raw}' — неверный IP-адрес (нужен локальный IP ПК, напр. 192.168.1.10)")

    wol_broadcast = _env("WOL_BROADCAST_ADDR", "255.255.255.255")
    try:
        ipaddress.ip_address(wol_broadcast)
    except ValueError:
        errors.append(f"WOL_BROADCAST_ADDR='{wol_broadcast}' — должен быть IP-адресом подсети (255.255.255.255 или 192.168.1.255)")

    wol_port = _env_int("WOL_PORT", 9, errors, minimum=1, maximum=65535)
    cooldown = _env_int("WAKE_COOLDOWN_SECONDS", 20, errors, minimum=0, maximum=3600)
    waking_timeout = _env_int("WAKING_TIMEOUT_SECONDS", 180, errors, minimum=5, maximum=3600)
    ping_timeout = _env_int("PING_TIMEOUT_SECONDS", 2, errors, minimum=1, maximum=30)

    ports_raw = _env("PC_STATUS_PORTS", "3389,445")
    status_ports: List[int] = []
    for piece in ports_raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            port = int(piece)
            if not (1 <= port <= 65535):
                raise ValueError
            status_ports.append(port)
        except ValueError:
            errors.append(f"PC_STATUS_PORTS: '{piece}' — неверный порт (формат: 3389,445)")
    if not status_ports and not errors:
        errors.append("PC_STATUS_PORTS пуст — укажите хотя бы один порт (например 3389,445)")

    log_level = _env("LOG_LEVEL", "INFO") or "INFO"
    if log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append(f"LOG_LEVEL='{log_level}' — допустимо DEBUG/INFO/WARNING/ERROR")

    if errors:
        return None, errors

    assert user_id is not None  # уже проверено выше: иначе errors непуст
    return Config(
        token=token,
        user_id=user_id,  # type: ignore[arg-type]
        mac=mac,
        local_ip=local_ip,
        status_ports=tuple(status_ports),
        wol_broadcast=wol_broadcast,
        wol_port=wol_port,
        cooldown_seconds=cooldown,
        waking_timeout_seconds=waking_timeout,
        ping_timeout_seconds=ping_timeout,
        log_level=log_level,
    ), []


# --------------------------------------------------------------------------
# Состояние шлюза (в памяти процесса)
# --------------------------------------------------------------------------

class GatewayState:
    def __init__(self) -> None:
        self.last_wol_at: float = 0.0          # unix-time последнего успешного WoL
        self.last_wol_ok: bool = False
        self.wake_commands_total: int = 0


CFG: Optional[Config] = None
STATE = GatewayState()


# --------------------------------------------------------------------------
# Сетевые проверки
# --------------------------------------------------------------------------

async def tcp_reachable(host: str, port: int, timeout: float) -> Tuple[bool, float]:
    """Возвращает (открыт ли порт, задержка в мс)."""
    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False, (time.perf_counter() - started) * 1000
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 — соединение уже установлено, результат нам известен
        pass
    return True, (time.perf_counter() - started) * 1000


async def ping_reachable(host: str, timeout: int) -> Optional[bool]:
    """True/False по результату ping, None — если ping в системе недоступен."""
    ping_bin = shutil.which("ping")
    if not ping_bin:
        return None
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            ping_bin, "-c", "1", "-W", str(timeout), host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout + 3)
        except asyncio.TimeoutError:
            proc.kill()
            return False
    except (OSError, asyncio.SubprocessError):
        return None
    _ = (time.perf_counter() - started)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:  # хост недоступен (iputils-семантика)
        return False
    return None  # прочие ошибки (нет прав, неверные аргументы)


@dataclass
class ProbeResult:
    online: Optional[bool]   # True — отвечает, False — недоступен, None — проверить нечем
    detail: str
    elapsed_ms: Optional[float] = None


async def probe(cfg: Config) -> ProbeResult:
    """Проверка ПК: TCP-порты, затем ping (best effort)."""
    if not cfg.local_ip:
        return ProbeResult(
            None,
            "проверка недоступна: PC_LOCAL_IP не задан в .env "
            "(для самого WoL он не нужен, нужен только для /status)",
        )

    started = time.perf_counter()
    for port in cfg.status_ports:
        ok, delay = await tcp_reachable(cfg.local_ip, port, timeout=2.5)
        if ok:
            return ProbeResult(
                True,
                f"TCP {cfg.local_ip}:{port} открыт",
                elapsed_ms=delay,
            )

    ping_res = await ping_reachable(cfg.local_ip, cfg.ping_timeout_seconds)
    total_ms = (time.perf_counter() - started) * 1000

    if ping_res is True:
        return ProbeResult(True, f"ping {cfg.local_ip} проходит", elapsed_ms=total_ms)
    if ping_res is False:
        return ProbeResult(
            False,
            f"ping {cfg.local_ip} без ответа, TCP-порты ({', '.join(map(str, cfg.status_ports))}) закрыты",
            elapsed_ms=total_ms,
        )
    return ProbeResult(
        False,
        f"TCP-порты ({', '.join(map(str, cfg.status_ports))}) закрыты; ping в Termux недоступен "
        f"(необязательно: pkg install inetutils)",
        elapsed_ms=total_ms,
    )


# --------------------------------------------------------------------------
# Тексты ответов
# --------------------------------------------------------------------------

HELP_TEXT = (
    "Команды шлюза WoL:\n\n"
    "/start — приветствие и краткая сводка\n"
    "/pc — отправить Wake-on-LAN (Magic Packet) на домашний ПК\n"
    "/status — состояние ПК в локальной сети\n"
    "/help — эта справка\n\n"
    "После включения ПК подключайтесь к нему через приложение Tailscale на iPhone."
)

START_TEXT = (
    "Шлюз Wake-on-LAN запущен и ждёт команд.\n\n"
    "/pc — включить домашний ПК\n"
    "/status — проверить состояние ПК\n"
    "/help — справка"
)


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def is_allowed(update: Update) -> bool:
    """Фильтр по Telegram user ID. Чужим не отвечаем, но пишем в лог."""
    assert CFG is not None
    uid = update.effective_user.id if update.effective_user else None
    if uid == CFG.user_id:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    LOG.warning("Отклонена команда: user_id=%s chat_id=%s (разрешён только %s)",
                uid, chat_id, CFG.user_id)
    return False


# --------------------------------------------------------------------------
# Обработчики команд
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    LOG.info("Получена команда /start")
    await update.message.reply_text(START_TEXT)  # type: ignore[union-attr]


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    LOG.info("Получена команда /help")
    await update.message.reply_text(HELP_TEXT)  # type: ignore[union-attr]


async def cmd_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправка Magic Packet с кулдауном."""
    if not is_allowed(update):
        return
    assert CFG is not None

    now = time.time()
    if STATE.last_wol_at and (now - STATE.last_wol_at) < CFG.cooldown_seconds:
        remain = int(CFG.cooldown_seconds - (now - STATE.last_wol_at))
        LOG.info("WoL отклонён: кулдаун, осталось %s с", remain)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"⏳ Подождите: последний запуск был недавно, кулдаун ещё {remain} с.\n"
            f"Защита от случайного/повторного пуска."
        )
        return

    LOG.info("Команда /pc: отправляю Magic Packet mac=%s -> %s:%s",
             CFG.mac, CFG.wol_broadcast, CFG.wol_port)
    try:
        await asyncio.to_thread(
            send_magic_packet,
            CFG.mac,
            ip_address=CFG.wol_broadcast,
            port=CFG.wol_port,
        )
    except Exception:
        LOG.exception("Ошибка отправки Magic Packet")
        await update.message.reply_text(  # type: ignore[union-attr]
            "❌ Ошибка отправки Wake-on-LAN пакета. Подробности в logs/gateway.log."
        )
        return

    STATE.last_wol_at = time.time()
    STATE.last_wol_ok = True
    STATE.wake_commands_total += 1

    await update.message.reply_text(  # type: ignore[union-attr]
        "✅ Wake-on-LAN пакет отправлен.\n"
        f"MAC: {CFG.mac}\n"
        f"Куда: {CFG.wol_broadcast}:{CFG.wol_port}\n\n"
        "ПК обычно поднимается за 20–60 секунд (иногда до 2–3 минут).\n"
        "Проверяйте через /status — состояние «🟡 просыпается» это нормально."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Состояние ПК с учётом фазы загрузки после WoL."""
    if not is_allowed(update):
        return
    assert CFG is not None

    LOG.info("Команда /status: выполняю проверку ПК")
    try:
        res = await probe(CFG)
    except Exception:
        LOG.exception("Ошибка проверки ПК")
        await update.message.reply_text(  # type: ignore[union-attr]
            "❌ Ошибка проверки состояния ПК. См. logs/gateway.log."
        )
        return

    since_wol = (time.time() - STATE.last_wol_at) if STATE.last_wol_at else None
    lines: List[str] = []

    if res.online:
        lines.append(f"🟢 ПК ВКЛЮЧЁН и отвечает — {res.detail}")
        if res.elapsed_ms is not None:
            lines.append(f"Отклик: {int(res.elapsed_ms)} мс")
        if since_wol is not None and since_wol < CFG.waking_timeout_seconds:
            lines.append(f"Проснулся после WoL за {fmt_elapsed(since_wol)}.")
    elif res.online is None:
        lines.append(f"⚪ {res.detail}")
        lines.append("Сам WoL при этом работает независимо от этой проверки.")
    else:
        waking = since_wol is not None and since_wol < CFG.waking_timeout_seconds
        if waking:
            lines.append(
                f"🟡 ПК, похоже, ПРОСЫПАЕТСЯ: WoL отправлен "
                f"{fmt_elapsed(since_wol)} назад, загрузка ещё идёт "
                f"(ping/TCP ещё не отвечают — это нормально)."
            )
            lines.append(f"Детали проверки: {res.detail}")
            lines.append(f"Ещё подождите до {CFG.waking_timeout_seconds} с после команды /pc.")
        else:
            lines.append(f"🔴 ПК ВЫКЛЮЧЕН или недоступен в сети — {res.detail}")
            if since_wol is not None:
                lines.append(
                    f"Последний WoL был {fmt_elapsed(since_wol)} назад. "
                    "Если дольше — вероятно, пакет не дошёл: проверьте MAC, "
                    "BIOS/Windows, изоляцию сети (см. README, раздел диагностики)."
                )
            else:
                lines.append("Команда /pc в этой сессии ещё не отправлялась.")

    lines.append(f"Формат проверки: TCP {CFG.local_ip or '?'}:{','.join(map(str, CFG.status_ports))}"
                 + (", ping" if shutil.which("ping") else ""))
    await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# Самодиагностика и разовые команды
# --------------------------------------------------------------------------

def selfcheck() -> int:
    """python bot.py --check — валидация .env + проба соединения с Telegram."""
    print("== Проверка конфигурации (.env) ==")
    print(f"Файл: {ENV_FILE}")

    cfg, errors = load_config()
    if errors:
        for err in errors:
            print(f"  [ОШИБКА] {err}")
        print("Исправьте .env (см. .env.example) и повторите.")
        return 1

    assert cfg is not None
    print("  [OK] обязательные параметры заданы и валидны")
    print(f"  MAC ПК:            {cfg.mac}")
    print(f"  Telegram user_id:  {cfg.user_id}")
    print(f"  PC_LOCAL_IP:       {cfg.local_ip or 'не задан — /status будет ограничен'}")
    print(f"  WoL-адрес:         {cfg.wol_broadcast}:{cfg.wol_port}")
    print(f"  Порты для status:  {', '.join(map(str, cfg.status_ports))}")
    print(f"  Кулдаун/просыпается:{cfg.cooldown_seconds} с / {cfg.waking_timeout_seconds} с")
    print(f"  Логи:              {LOG_DIR / 'gateway.log'}")

    print("\n== Проверка связи с Telegram (getMe) ==")
    url = f"https://api.telegram.org/bot{cfg.token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 — наш токен
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            bot = data.get("result", {})
            print(f"  [OK] бот @{bot.get('username')} ({bot.get('first_name')}) отвечает")
        else:
            print(f"  [ОШИБКА] Telegram ответил отказом: {data.get('description')}")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [СЕТЬ] не удалось достучаться до Telegram: {exc!r}")
        print("  Проверьте интернет на устройстве и токен.")
        return 1

    print("\n== Проверка доступности ПК (как в /status) ==")
    if not cfg.local_ip:
        print("  [ПРОПУСК] PC_LOCAL_IP не задан")
    else:
        result = asyncio.run(probe(cfg))
        mark = {True: "[ONLINE]", False: "[OFFLINE]", None: "[???]"}[result.online]
        print(f"  {mark} {result.detail}")

    print("\nВсё готово. Запуск: python bot.py")
    return 0


def send_wol_now() -> int:
    """python bot.py --send-wol — разовая отправка Magic Packet (для теста из Termux)."""
    cfg, errors = load_config()
    if errors:
        for err in errors:
            print(f"[ОШИБКА] {err}")
        return 1
    assert cfg is not None
    print(f"Отправляю Magic Packet: {cfg.mac} -> {cfg.wol_broadcast}:{cfg.wol_port} ...")
    try:
        send_magic_packet(cfg.mac, ip_address=cfg.wol_broadcast, port=cfg.wol_port)
    except Exception as exc:  # noqa: BLE001
        print(f"[ОШИБКА] {exc!r}")
        return 1
    print("[OK] пакет отправлен без ошибок (это не гарантирует, что ПК примет его — см. README).")
    return 0


# --------------------------------------------------------------------------
# Главный цикл
# --------------------------------------------------------------------------

def build_application(cfg: Config) -> Application:
    app = Application.builder().token(cfg.token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pc", cmd_pc))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    return app


def main() -> int:
    if "--check" in sys.argv:
        return selfcheck()
    if "--send-wol" in sys.argv:
        return send_wol_now()

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

    cfg, errors = load_config()
    if errors:
        for err in errors:
            LOG.error("Конфигурация: %s", err)
        LOG.error("Исправьте %s (образец: .env.example) и перезапустите.", ENV_FILE)
        return 2

    assert cfg is not None
    globals()["CFG"] = cfg

    LOG.info("Шлюз WoL стартует: MAC=%s broadcast=%s:%s user_id=%s polling(без webhook)",
             cfg.mac, cfg.wol_broadcast, cfg.wol_port, cfg.user_id)
    if not cfg.local_ip:
        LOG.warning("PC_LOCAL_IP не задан: /status не сможет проверять ПК (WoL при этом работает)")

    # Внешний цикл: переживаем падения сети/исключения polling'а.
    # Внутри Termux:Boot скрипт дополнительно перезапускает процесс.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    backoff = 10
    while True:
        app = build_application(cfg)
        try:
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            LOG.info("Polling корректно остановлен — выходим.")
            return 0
        except (KeyboardInterrupt, SystemExit):
            LOG.info("Получен сигнал остановки — выходим.")
            return 0
        except Exception:  # noqa: BLE001
            LOG.exception("Polling упал; перезапуск через %s с", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    sys.exit(main())
