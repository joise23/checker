# Ozon Card Checker

Локальное веб-приложение для ручной проверки карточек товара по загруженной базе правил. База хранится в SQLite (`data/ozon_checker.sqlite3`), а изображения не сохраняются в журнале проверок.

При обнаружении нескольких ошибок или нарушений веб-приложение выводит **все найденные ошибки одновременно**, а не ограничивается первой попавшейся.

## Запуск

В PowerShell из папки проекта:

```powershell
python .\app.py
```

Откройте в браузере `http://127.0.0.1:8080`.

## LLM-проверка (Gemini 2.5 Flash Lite / OpenAI-compatible API)

Без настроек LLM приложение выполняет только строгие детерминированные проверки по правилам базы данных.

Для подключения модели **`gemini-2.5-flash-lite`** (через OpenAI-совместимый API Google Gemini) выполните в PowerShell:

```powershell
$env:LLM_API_KEY = "ваш_API_ключ_Gemini"
$env:LLM_MODEL = "gemini-2.5-flash-lite"
$env:LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
python .\app.py
```

*Примечание:* Если вы используете сторонний провайдер или прокси (например, OpenRouter), укажите соответствующий `LLM_BASE_URL` и API-ключ:

```powershell
$env:LLM_API_KEY = "ваш_ключ"
$env:LLM_MODEL = "google/gemini-2.5-flash-lite"
$env:LLM_BASE_URL = "https://openrouter.ai/api/v1"
python .\app.py
```

API-ключ хранится только в переменной среды и не записывается в SQLite.

## Данные и SQLite база

- `data/ozon_rules_source_base.json` — исходные DOCX/PDF, извлечённые с указателями на фрагменты.
- `data/ozon_checker.sqlite3` — создаётся/обновляется при запуске: правила, фрагменты источников и история проверок.
- `CORE_RULES` в `app.py` — начальный нормализованный слой наиболее важных карточечных правил, включая принципы честной конкуренции.
