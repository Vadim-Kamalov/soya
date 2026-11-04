# Проверка выполнения задания

Дата: 2026-11-03. Окружение: Windows, Docker Desktop (Linux containers),
Python 3.12.15 в образах, PostgreSQL 17, Redis 7.4, grpcio 1.71.2.

## Результаты

| Проверка | Результат |
|---|---|
| Стандартная сборка Dockerfile и запуск Compose | Оба приложения собраны, все 9 контейнеров healthy |
| `docker compose --profile test run --build --rm tests` | **43 passed in 5.85s**, exit 0, без warnings |
| `python scripts/smoke.py` | REST → gRPC: поиск, создание, список, снимок цены, отмена, повтор отмены — OK |
| `python scripts/check_failover.py` | Sentinel повысил реплику; REST flow прошёл при остановленном master; бывший master вернулся здоровой репликой |
| `python scripts/check_circuit.py` | При остановленном Flight — HTTP 503; OPEN ответил за 0.015 s; после старта Flight существующий Booking вернулся к HTTP 200 |
| Переходы в логах Booking | CLOSED → OPEN → HALF_OPEN → CLOSED |
| Python compileall | Успех |
| Compose config | Валидна |

Сервисы оставлены запущенными. REST: `http://localhost:8080`, Swagger: `/docs`.
Проверки отказов сами восстанавливают остановленные контейнеры.

## Соответствие условию

| Пункт | Реализация / доказательство |
|---|---|
| 1. gRPC-контракт | `proto/flight.proto`, protoc-generated `generated/`; Timestamp, enum, бизнес-операции и коды ошибок |
| 2. ER / 3NF | `docs/er.md`, SQL CHECK/FK/UNIQUE и индексы |
| 3. PostgreSQL и сервисы | Два контейнера PostgreSQL, REST Booking, gRPC Flight; версионированные миграции с checksum и блокировкой при старте |
| 4. gRPC-взаимодействие | GetFlight → ReserveSeats → INSERT Booking; ReleaseReservation при отмене; REST smoke и интеграционные тесты |
| 5. Транзакции | SELECT FOR UPDATE, атомарные seat/reservation изменения; rollback, последний seat, durable compensation и recovery проверены |
| 6. Аутентификация | API key из env, metadata x-api-key; каждый RPC без/с неверным ключом возвращает UNAUTHENTICATED |
| 7. Redis cache-aside | Flight и search, TTL 300 s, hit/miss логи; invalidation при Reserve/Release/Update, stale-fill race и Redis outage проверены |
| 8. Retry | Максимум 3 попытки, 100/200 ms backoff, только UNAVAILABLE/DEADLINE_EXCEEDED; потерянный ответ Reserve не дублирует списание |
| 9. Sentinel | master + replica + 3 Sentinel, quorum 2; Sentinel-aware redis-py; настоящий failover и rejoin проверены |
| 10. Circuit breaker | Клиентский interceptor, env-настройки, CLOSED/OPEN/HALF_OPEN, один probe, быстрый 503 и настоящее восстановление |

## Обнаруженные и исправленные проблемы

- Поиск на `9999-12-31` переполнял Python datetime: верхняя граница теперь
  вычисляется PostgreSQL. Regression test прошёл RED → GREEN.
- Fixture вставляла рейс, не повышая cache revision: тестовые записи теперь
  инвалидируют кеш в той же транзакции.
- DNS имени остановленного Redis master блокировал реплику. Репликация и
  Sentinel используют фиксированные числовые адреса в приватной сети Compose.
- Реальная остановка Flight выявила зависший старый gRPC channel. DNS, TCP и
  новый channel были исправны. Обновлён grpcio 1.71.0 → 1.71.2 с upstream
  [исправлением Python reconnect](https://github.com/grpc/grpc/pull/39948),
  ограничены задержки reconnect. Тот же Compose fault script после обновления
  прошёл без рестарта Booking.

Проведено независимое read-only ревью. Замечания по граничной дате и fixture
устранены; дополнительно проверены гонка записи кеша, реальный deadline,
потерянный ответ Reserve и конкурентная отмена. Неисправленных замечаний нет.

Границы согласованности двух независимых БД, повторный REST POST и формат денег
описаны в README. Строгой общей ACID-транзакции между сервисами нет: журнал и
компенсация завершают незаконченные операции после восстановления зависимостей.
