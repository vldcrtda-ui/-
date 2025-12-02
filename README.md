# Moderation Bot

Бот для модерации анонимных сообщений в Telegram.

## Быстрый старт (локально)
- Установите зависимости: `pip install -r requirements.txt`.
- Скопируйте `.env.example` в `.env` и укажите свои значения.
- Запустите: `python bot.py` (или `run_bot.bat` на Windows).

Нужные переменные окружения: `BOT_TOKEN`, `MOD_CHAT_ID`, `PUBLIC_CHAT_ID`, `MAIN_ADMIN_ID` (опционально `FORCE_IPV4=1`, если нужен только IPv4).

## Деплой на Render
- В настройках сервиса задайте переменные окружения с теми же именами (`.env` в репозитории не используется).
- Бот стартует из `/opt/render/project/src`, `load_dotenv` не обязателен — достаточно переменных окружения.
