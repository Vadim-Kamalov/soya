# Marketplace: OpenAPI + CRUD

Решение всех 10 пунктов [задания](marketplace_homework_openapi_crud.md).
Python 3.12, FastAPI, PostgreSQL, Flyway, JWT и Argon2.

## Запуск в Docker

Нужны Docker Desktop с Linux containers и Docker Compose v2.

```sh
docker compose up --build -d --wait
docker compose logs api migrate
```

API: http://localhost:8000, Swagger UI: http://localhost:8000/docs,
исходный контракт: http://localhost:8000/openapi.json.
БД доступна на `localhost:5432` (marketplace / marketplace_local).
Если порт занят, укажите свободный `POSTGRES_PORT` в `.env`; в текущем окружении
используется `55432`. Внутри Docker PostgreSQL всегда слушает порт `5432`.
Для локальной демонстрации в Compose заданы значения по умолчанию;
свои `POSTGRES_PASSWORD`, `JWT_SECRET`, `ORDER_RATE_LIMIT_MINUTES` можно указать
в `.env` по образцу `.env.example`. Python автоматически `.env` не загружает.

Сначала запускается PostgreSQL, затем Flyway, затем API.
Повторный запуск миграций безопасен: Flyway проверяет историю и checksum.
Генерируемые файлы отсутствуют в git: Docker генерирует их при сборке,
а `python -m scripts.run` — перед запуском.

```sh
docker compose exec api python -m scripts.demo
```

Скрипт проверяет регистрацию, вход, товар, промокод, заказ, откат неуспешного
обновления, перерасчёт скидки, отмену, восстановление склада, права, валидацию,
архивацию товара и ротацию refresh. Имена уникальны: его можно запускать повторно.
Пароли и токены скрипт не печатает. Созданные демонстрационные записи сохраняются.

Создать ADMIN (пароль вводится интерактивно):

```sh
docker compose exec api python -m scripts.admin admin
```

USER и SELLER регистрируются через `/auth/register`. Публично создать ADMIN нельзя.
В Swagger выполните `/auth/login`, затем **Authorize** с `access_token`.

## Проверка таблиц на защите

```sh
docker compose exec db psql -U marketplace -d marketplace
```

```sql
SELECT id, username, role FROM users;
SELECT id, name, price, stock, category, status, seller_id, created_at, updated_at FROM products;
SELECT * FROM orders;
SELECT * FROM order_items;
SELECT code, current_uses, max_uses, active FROM promo_codes;
SELECT * FROM user_operations ORDER BY created_at;
SELECT version, description, success FROM flyway_schema_history;
```

После E2E товар имеет `stock=10`, `status=ARCHIVED`, заказ — `CANCELED`,
промокод — `current_uses=0`; в `user_operations` есть CREATE_ORDER и UPDATE_ORDER.
Заказ и позиции сохранены. Снимки цен находятся в `order_items.price_at_order`.

## Тесты

Полный прогон в отдельной одноразовой БД, без изменения рабочей БД:

```sh
docker compose -p marketplace-tests -f compose.test.yaml run --build --rm tests
docker compose -p marketplace-tests -f compose.test.yaml down
```

Тесты проверяют API на настоящем PostgreSQL после Flyway, включая конкурентные
резервы, активные заказы, rollback, промокоды, JWT, роли, логирование и валидацию.
Они очищают только БД из `TEST_DATABASE_URL`; имя обязано оканчиваться на `_test`.
Без этой переменной интеграционные тесты пропускаются.

## Локальная разработка без Docker

Нужны Python 3.12+, PostgreSQL 15+ и [Flyway CLI](https://documentation.red-gate.com/flyway/reference/usage/command-line).
Ниже PowerShell; в Linux замените пути к Python на `.venv/bin/python` и используйте `export`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:DATABASE_URL='postgresql://marketplace:marketplace_local@localhost:5432/marketplace'
$env:JWT_SECRET='local-demo-secret-change-before-deployment-1234567890'
$env:ORDER_RATE_LIMIT_MINUTES='1'
flyway '-url=jdbc:postgresql://localhost:5432/marketplace' '-user=marketplace' '-password=marketplace_local' '-locations=filesystem:src/main/resources/db/migration' migrate
.\.venv\Scripts\python.exe -m scripts.generate
.\.venv\Scripts\python.exe -m scripts.run
```

БД и роль `marketplace` создаются заранее. Генерация одной командой:
`python -m scripts.generate`. Модели генерирует
[datamodel-code-generator](https://github.com/koxudaxi/datamodel-code-generator),
HTTP-хэндлеры — адаптер в `scripts/generate.py`, читающий operationId, параметры,
request/response schema и security из OpenAPI. AST-проход сохраняет точные Decimal
границы и строгую типизацию без потери Field-ограничений генератором.
Ручных DTO нет. `app/model_base.py` задаёт только общую политику сериализации.

Для тестов создайте отдельную БД `marketplace_test`, примените ту же миграцию,
затем:

```powershell
$env:TEST_DATABASE_URL='postgresql://marketplace:marketplace_local@localhost:5432/marketplace_test'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app scripts tests
.\.venv\Scripts\python.exe -m scripts.demo --database-url $env:DATABASE_URL
```

Последняя команда также выполняет SELECT и печатает строки созданного заказа.

## Архитектура и решения

| Требование | Реализация |
|---|---|
| OpenAPI + CRUD + схемы | `src/main/resources/openapi/marketplace.yaml` |
| Генерация моделей и хэндлеров | `scripts/generate.py`, `app/generated/` в `.gitignore` |
| PostgreSQL + Flyway | `src/main/resources/db/migration/V1__marketplace.sql` |
| Ошибки и валидация | generated Pydantic DTO, `app/errors.py` |
| Заказы и транзакции | `app/orders.py`, `app/promos.py`, `app/db.py` |
| JSON-логи каждого запроса | `app/middleware.py` |
| JWT, refresh, Argon2 | `app/auth.py` |
| Роли и владельцы | проверки в сервисах, `scripts/admin.py` |

Транзакция блокирует строку пользователя для лимита CREATE_ORDER/UPDATE_ORDER
и проверки активного заказа. Товары блокируются `SELECT ... ORDER BY id FOR UPDATE`
в одинаковом порядке; затем блокируется промокод. Остатки, позиции заказа,
использование промокода и журнал операций меняются атомарно. Дополнительные
ограничения БД запрещают отрицательные остатки, превышение max_uses и несколько
активных заказов одного пользователя. Успешные CREATE/UPDATE имеют раздельные
лимиты в минутах; неуспешные попытки не записываются в user_operations.
При изменении склада доступное количество вместе с возвратными резервами
не должно превышать вместимость int32: это сохраняет возможность отмены заказа.

Цены вычисляются в Decimal, скидки округляются до копеек ROUND_HALF_UP.
Процентная ставка свыше 70% оставляет полную цену (скидка равна нулю),
согласно формулировке задания «в ином случае цена 100%».
Для ставки до 70% включительно скидка ограничена 70% суммы;
фиксированная скидка — суммой заказа.
Предел 70% округляется вниз до копеек, чтобы округление скидки не превысило его.
Проверка вместимости DECIMAL(12,2) применяется к итоговой сумме и скидке после
расчёта промокода; промежуточная сумма до скидки может превышать этот предел.
При переполнении любой сохраняемой суммы операция полностью откатывается.
Повторные product_id суммируются до проверки склада. PUT заказа сохраняет
price_at_order для оставшихся товаров, в том числе при изменении количества.
Новые товары получают текущую цену каталога. Если товар удалить из заказа,
а затем добавить повторно, он получает новую текущую цену.
При PUT недействующий промокод снимается с возвратом usage; собственное
использование не расходует дополнительный лимит max_uses.
NUL в name, description, category и фильтре category запрещён контрактом:
такие запросы получают 400 VALIDATION_ERROR с указанием поля до обращения к БД.

Статусы: CREATED → PAYMENT_PENDING → PAID → SHIPPED → COMPLETED.
CREATED и PAYMENT_PENDING можно отменить. ADMIN endpoint
`POST /orders/{id}/status` позволяет продемонстрировать переходы.
ADMIN может работать с чужим заказом; USER — только со своим; SELLER — без доступа к заказам.

Access JWT живёт 20 минут, refresh — 14 дней. Refresh ротируется атомарно,
в БД хранится SHA-256, а не сам токен. Проверяются issuer, audience, signature,
expiry и тип токена. JWT содержит role; сервер дополнительно проверяет актуальную
роль в БД. Пароли хешируются Argon2. X-Request-Id генерируется сервером для каждого
запроса. Пароли и токены маскируются в JSON-логах; сырые невалидные тела не пишутся.

Подробнее о демонстрации и теории: [docs/defense.md](docs/defense.md).
