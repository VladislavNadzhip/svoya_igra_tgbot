# 🎮 Своя Игра — Telegram Bot

Telegram-бот для игры "Своя Игра" на **aiogram 3.x**.

## Возможности

- ✅ Полная поддержка форматов `.siq` (SIGame)
- ✅ Загрузка паков локально из папки `packs/` (без ограничения 20 МБ)
- ✅ Поддержка тем (topics) в групповых чатах
- ✅ Медиа в вопросах: картинки, аудио, видео
- ✅ Нечёткий поиск ответов (расстояние Левенштейна)
- ✅ Таймауты на кнопку и ответ
- ✅ Турнирная таблица

## Установка

```bash
git clone https://github.com/YOUR_USERNAME/si_game_bot.git
cd si_game_bot
pip install -r requirements.txt
```

## Настройка

Откройте `config.py` и укажите токен бота:

```python
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
```

Получите токен у [@BotFather](https://t.me/BotFather).

## Использование

1. **Создайте пакет вопросов:** [vladimirkhil.com/si/siquester](http://vladimirkhil.com/si/siquester)
2. **Скачайте `.siq` файл**
3. **Положите его в папку `packs/`**
4. **Запустите бота:**

```bash
python bot.py
```

## Команды

| Команда | Описание |
|---|---|
| `/start` | Начать / Помощь |
| `/help` | Правила игры |
| `/newgame` | Создать новую игру |
| `/join` | Присоединиться |
| `/leave` | Покинуть игру |
| `/startgame` | Начать игру |
| `/scores` | Текущий счёт |
| `/stop` | Остановить игру |
| `/listpacks` | Список локальных паков |
| `/loadpack <имя>` | Загрузить пак из `packs/` |
| `/packinfo` | Инфо о паке |

## Структура проекта

```
si_game_bot/
├── bot.py           # Основной бот
├── config.py        # Настройки
├── game.py          # Логика игры
├── siq_parser.py    # Парсер .siq файлов
├── requirements.txt # Зависимости
└── packs/           # Папка для .siq файлов
```

## Лицензия

MIT
