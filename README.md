# Синоникс Ассистент

## Проверка платформы

Требуются Python 3.12, uv и Docker Compose. Выполните из корня репозитория.

```sh
uv sync --project backend --locked --group dev
uv run --project backend --no-sync python scripts/bootstrap-env.py
docker compose --profile "*" config --quiet
docker compose up -d --wait postgres
docker compose run --build --rm --no-deps migrate
docker compose run --build --rm --no-deps migrate alembic current
docker compose run --build --rm --no-deps provision-checkpoints
uv run --project backend --no-sync pytest -q backend/tests
```

Команда подготовки создаёт `.env` и `infra/local/garage.toml` с локальными
учётными данными. API, обработчики запросов, конвертер и клиент будут
доступны после подключения соответствующих модулей.
