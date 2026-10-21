# Marketplace Implementation Plan

> **For agentic workers:** use superpowers:executing-plans. Work is performed inline.

**Goal:** Выполнить все 10 пунктов домашнего задания.
**Architecture:** Contract-first FastAPI; generated DTO and routes; transactional PostgreSQL services.
**Tech Stack:** Python 3.12, FastAPI, Pydantic, psycopg, PyJWT, Argon2, PostgreSQL, Flyway.
**Spec:** `docs/superpowers/specs/2026-10-20-marketplace-design.md`

## Global Constraints

- Контракт: `src/main/resources/openapi/marketplace.yaml`.
- Генерируемые DTO и хэндлеры: `app/generated/`, в .gitignore.
- Миграции исключительно Flyway. Денежные операции Decimal.
- Авторизация всех бизнес-операций; роли USER, SELLER, ADMIN.

## Review Focus

- Конкурентные резервы не делают stock отрицательным.
- Неуспешный PUT возвращает прежние остатки, позиции и скидку.
- Повторные product_id учитываются суммарно.
- Refresh нельзя использовать как access или повторно после ротации.
- Отмена не возвращает остатки и usage дважды.

### Task 1: Контракт, генерация, инфраструктура

Files: OpenAPI, scripts/generate.py, Dockerfile, compose.yaml, migrations, tests/test_contract.py.
Produces: generated models and router; database tables and constraints.

- [x] Проверить тестом полноту эндпоинтов и ограничения контракта; получить RED.
- [x] Описать OpenAPI, реализовать воспроизводимую генерацию и Docker build.
- [x] Добавить Flyway-миграции с индексами, триггерами и ограничениями.
- [x] Валидировать контракт и импорт generated-кода; получить GREEN.

### Task 2: Auth, товары, ошибки и логирование

Files: app/{main,config,db,errors,auth,products,handlers,middleware}.py, tests/test_api.py.
Consumes: generated models/router, migrated database.
Produces: authenticated HTTP API and consistent errors.

- [x] Написать HTTP-тесты регистрации, JWT, ролей, CRUD, пагинации и валидации.
- [x] Запустить RED; реализовать сервисы и обработчики.
- [x] Проверить ротацию refresh, права продавцов и маскирование логов.

### Task 3: Заказы и промокоды

Files: app/{orders,promos}.py, tests/test_orders.py.
Consumes: auth context, database, generated order and promo models.
Produces: atomic order lifecycle with stock, promo usage and rate limits.

- [x] Написать тесты создания, обновления, отмены, переходов и rollback.
- [x] Запустить RED; реализовать транзакции и блокировки.
- [x] Проверить конкурентный склад, один активный заказ, лимиты и usage.

### Task 4: Демонстрация и окончательная проверка

Files: README.md, scripts/{demo,admin}.py, docs/defense.md.

- [x] Добавить E2E и SELECT для проверки таблиц.
- [x] Проверить чистую генерацию, весь pytest, lint и HTTP E2E.
- [x] Провести обзор реализации по всем требованиям; исправить найденные дефекты.
- [x] Задокументировать запуск, принятые решения и фактические результаты проверки.
