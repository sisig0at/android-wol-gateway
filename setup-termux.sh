#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
#  Установка шлюза WoL в Termux (запускать ПОСЛЕ распаковки проекта в ~)
#  Использование:  bash setup-termux.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

echo "==> 1/4. Обновление пакетов Termux"
pkg update -y
pkg upgrade -y

echo "==> 2/4. Установка Python (и необязательного ping)"
pkg install -y python
pkg install -y inetutils || echo "    inetutils не встал — /status будет работать только по TCP, это нормально"

echo "==> 3/4. Установка зависимостей проекта"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "==> 4/4. Подготовка автозапуска (Termux:Boot)"
mkdir -p "$HOME/.termux/boot"
cp -f termux-boot/start-gateway.sh "$HOME/.termux/boot/start-gateway.sh"
chmod +x "$HOME/.termux/boot/start-gateway.sh"
echo "    Скрипт автозапуска установлен: $HOME/.termux/boot/start-gateway.sh"
echo "    (сработает, когда установлена бесплатная программа Termux:Boot с F-Droid)"

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo ""
    echo "    ВНИМАНИЕ: создан пустой шаблон .env — заполните СВОИ значения:"
    echo "    nano $PROJECT_DIR/.env"
fi

echo ""
echo "==> Готово. Дальше:"
echo "    1) Заполните .env:           nano $PROJECT_DIR/.env"
echo "    2) Проверьте конфигурацию:   python bot.py --check"
echo "    3) Тестовый запуск:          python bot.py"
echo "    4) Подробности и диагностика: см. README.md"
