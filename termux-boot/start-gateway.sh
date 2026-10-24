#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
#  Termux:Boot — автозапуск шлюза WoL после перезагрузки Android.
#  Файл должен лежать в:  ~/.termux/boot/start-gateway.sh  (и быть +x).
#  Требуется приложение Termux:Boot (бесплатно, F-Droid).
# ============================================================================
set -u

# Путь к проекту. Если распаковали не в $HOME/wol-telegram-gateway —
# задайте свой путь переменной окружения WOL_PROJECT_DIR.
PROJECT_DIR="${WOL_PROJECT_DIR:-$HOME/wol-telegram-gateway}"

# 1) Не даём системе усыпить процесс (снимает часть ограничений Doze).
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
fi

cd "$PROJECT_DIR" || {
    echo "[boot] проект не найден: $PROJECT_DIR" >&2
    exit 1
}
mkdir -p logs

echo "[boot] $(date '+%F %T') автозапуск шлюза WoL (pid $$)" >> logs/daemon.log

# 2) Вечный цикл: если процесс упал (сеть, OOM, обновление Python) —
#    поднимаем снова. Telegram long polling сам переживает обрывы сети.
while true; do
    python bot.py >> logs/daemon.log 2>&1
    code=$?
    echo "[boot] $(date '+%F %T') bot.py завершился (code=$code), рестарт через 15 с" >> logs/daemon.log
    sleep 15
done
