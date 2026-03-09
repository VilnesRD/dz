# Битрикс24 ↔ Doczilla PRO

Генерация документов в Doczilla PRO по данным из CRM Битрикс24.
Кнопка в карточке сделки → iframe с выбором шаблона → PDF → ссылка в сделке.

## Структура

```
app/
  api/
    admin_api.py       # REST API: шаблоны, маппинги, логи, JWT-авторизация
    widget.py          # /bitrix/widget, /api/widget-config, /install
  core/
    config.py          # Pydantic Settings (.env)
    session.py         # Сессия Doczilla с auto re-signin
  db/
    database.py        # SQLAlchemy + init_db()
    models.py          # User, Template, FieldMapping, GenerationLog
    repository.py      # CRUD
  services/
    bitrix_client.py   # REST-клиент Б24
    doczilla_client.py # API-клиент Doczilla
    generation.py      # Оркестратор генерации
    mapper_db.py       # Маппинг Б24 → переменные Doczilla
  static/
    admin.html         # Админ-панель (Alpine.js SPA)
    widget.html        # Виджет для iframe
    install.html       # Страница установки Б24-приложения
  main.py
deploy/
  doczilla-bridge      # nginx server block для bridge.vird.cloud
scripts/
  inspect_template.py       # Просмотр переменных шаблона Doczilla
  register_bitrix_app.py    # Регистрация кнопки в Б24
docker-compose.yml
Dockerfile
.env.example
```

## Деплой на VPS

```bash
git clone git@github.com:ВАШ/РЕПО.git /opt/doczilla-integration
cd /opt/doczilla-integration
cp .env.example .env
nano .env  # заполнить все переменные
docker compose up -d
```

nginx (хостовый, не трогает другие приложения):
```bash
sudo cp deploy/doczilla-bridge /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/doczilla-bridge /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bridge.vird.cloud --email you@email.com --agree-tos
```

Проверка: `curl https://bridge.vird.cloud/health`

## Регистрация в Б24

Разработчикам → Локальные приложения:

| Поле | Значение |
|------|----------|
| URL для установки | `https://bridge.vird.cloud/install` |
| URL обработчика | `https://bridge.vird.cloud/webhook/bitrix` |

После установки: `python scripts/register_bitrix_app.py`

## Обновление

```bash
cd /opt/doczilla-integration
git pull
docker compose up -d --build
```

## Переменные .env

| Переменная | Описание |
|------------|----------|
| `DOCZILLA_BASE_URL` | URL Doczilla |
| `DOCZILLA_LOGIN` | Email |
| `DOCZILLA_PASSWORD` | Пароль |
| `BITRIX_WEBHOOK_URL` | Вебхук Б24 |
| `APP_PUBLIC_URL` | `https://bridge.vird.cloud` |
| `ADMIN_USERNAME` | Логин в панель |
| `ADMIN_PASSWORD` | Пароль в панель |
| `ADMIN_SECRET_KEY` | JWT-секрет (≥32 символа) |
| `DB_PATH` | Путь к SQLite (не менять) |
| `DEBUG` | `true` / `false` |
