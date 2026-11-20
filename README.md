# Синоникс Ассистент

Синоникс 1.9 уточняет описание двух контуров, рассчитывает сетевые и файловые
правила, сохраняет ревизии проекта и выгружает экспертный JSON и редактируемую
схему draw.io. Справочный поиск учитывает русские словоформы и доступ к фрагментам.

## Локальная разработка

Нужны Python 3.12, uv, Node.js 22 и PostgreSQL 17. Задайте
`PARTNER_DATABASE_URL` с адресом своей БД в формате
`postgresql+psycopg://` и ролью, которая имеет право создавать объекты в этой
БД. Для локальной проверки задайте `PARTNER_PUBLIC_ORIGIN=http://localhost:8080`
и `PARTNER_RUNTIME_PROFILE=offline_fixture`.

```powershell
uv sync --project backend --locked --group dev
npm ci --ignore-scripts --prefix outputs/partner-prototype
uv run --project backend --no-sync alembic -c backend/alembic.ini upgrade head
uv run --project backend --no-sync python -m partner_agent.infra.provision
uv run --project backend --no-sync partner-admin first-admin
uv run --project backend --no-sync python -c "import asyncio, uvicorn; server = uvicorn.Server(uvicorn.Config('partner_agent.main:app', host='127.0.0.1', port=8080)); asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)"
```

Во втором терминале запустите клиент и откройте `http://127.0.0.1:5173`.

```powershell
npm run dev --prefix outputs/partner-prototype
```

Для обработки запросов нужен Hatchet с локальным токеном. Для загрузки и
индексации документов нужны Garage и Qdrant, для конвертации нужен
изолированный Linux-координатор. Compose использует отдельные локальные
учётные данные из `.env` и конфигурацию Garage из `infra/local/garage.toml`.
Для чистой рабочей копии их создаёт команда ниже. Проверка конфигурации
требует Docker Compose.

```powershell
uv run --project backend --no-sync python scripts/bootstrap-env.py
docker compose --profile "*" config --quiet
npm test --prefix outputs/partner-prototype
npm run build --prefix outputs/partner-prototype
```

Новые проекты используют требования версии 3. Переменная
`PARTNER_DESIGN_INPUT_VERSION=2` включает прежний режим извлечения требований.
Формат сохранённого проекта привязан к его ревизии.

Для отдельной проверки с закрытым корпусом укажите каталог в
`PARTNER_KNOWLEDGE19_SOURCE`, файл контрольных запросов в
`PARTNER_KNOWLEDGE19_TEST_HOLDOUT` и подтвердите одноразовый стенд через
`PARTNER_KNOWLEDGE19_TEST_CONFIRMED=1`. Храните оба входа вне репозитория.
