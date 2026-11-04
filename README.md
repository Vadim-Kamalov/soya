# Flight Booking: gRPC + Redis

Реализация учебного задания, пункты **1–10**. Python 3.12,
FastAPI, grpcio, PostgreSQL, Redis Sentinel. Booking и Flight используют
разные базы данных и общаются только по gRPC.

```text
REST :8080 → Booking Service → gRPC :50051 → Flight Service
                │                              │       │
           Booking PostgreSQL           Flight PostgreSQL
                                                       │
                                    Redis master + replica + 3 Sentinels
```

## Запуск

Нужны Docker и Docker Compose. Из корня репозитория:

```sh
docker-compose up --build
# Также подходит современная команда:
docker compose up --build -d
```

Конфигурация для локальной проверки работает без `.env`. Для изменения настроек
скопируйте `.env.example` в `.env`. PostgreSQL и Redis доступны только внутри
Compose network; наружу опубликован REST на `http://localhost:8080`.
Миграции применяются автоматически перед запуском каждого приложения, повторный
старт безопасен; SQL-файлы версионируются и проверяются по SHA-256. При нескольких
экземплярах сервиса миграции сериализуются advisory lock.

- Swagger / OpenAPI: http://localhost:8080/docs
- Готовность Booking и его БД: http://localhost:8080/health
- ER-диаграммы и обоснование 3NF: [docs/er.md](docs/er.md)
- gRPC-контракт с описанием ошибок: [proto/flight.proto](proto/flight.proto)
- Архитектурные решения: [docs/design.md](docs/design.md)
- Результаты проверок и соответствие 10 пунктам: [docs/verification.md](docs/verification.md)

Тестовые рейсы: `11111111-1111-4111-8111-111111111111` (SVO → LED,
2026-12-01, 12 500 RUB), `22222222-2222-4222-8222-222222222222` (VKO → LED,
2026-12-01), `33333333-3333-4333-8333-333333333333` (SVO → LED, 2026-12-02).
Даты фиксированы для воспроизводимости примеров; поиск не исключает прошедшие
рейсы автоматически — критерием является статус SCHEDULED.

## REST API

| Метод | Путь | Результат |
|---|---|---|
| GET | `/flights?origin=SVO&destination=LED&date=2026-12-01` | Запланированные рейсы, date необязателен |
| GET | `/flights/{uuid}` | Информация о рейсе |
| POST | `/bookings` | Созданное бронирование, 201 |
| GET | `/bookings/{uuid}` | Бронирование |
| GET | `/bookings?user_id=user-1` | Бронирования пользователя |
| POST | `/bookings/{uuid}/cancel` | Отменённое бронирование; повтор безопасен |

```sh
curl 'http://localhost:8080/flights?origin=SVO&destination=LED&date=2026-12-01'

curl -X POST http://localhost:8080/bookings \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1","flight_id":"11111111-1111-4111-8111-111111111111","passenger_name":"Иван Иванов","passenger_email":"ivan@example.com","seat_count":2}'

curl 'http://localhost:8080/bookings?user_id=user-1'
curl -X POST http://localhost:8080/bookings/BOOKING_ID/cancel
```

Деньги возвращаются строкой с двумя десятичными знаками, например
`"total_price":"25000.00"`, валюта RUB. Время — ISO 8601 UTC.
`user_id` — внешний идентификатор заказчика, имя/email — снимок данных пассажира.
Публичная аутентификация пользователей не входит в задание; сервис предназначен
для локальной демонстрации. Сервисная аутентификация gRPC реализована отдельно.

Ошибки имеют JSON-формат `{"detail":"..."}`. Бизнес-ошибки Flight преобразуются:

| gRPC | HTTP |
|---|---|
| INVALID_ARGUMENT | 400 |
| NOT_FOUND | 404 |
| RESOURCE_EXHAUSTED, ALREADY_EXISTS, FAILED_PRECONDITION | 409 |
| UNAUTHENTICATED, PERMISSION_DENIED | 502 (ошибка credentials между сервисами) |
| UNAVAILABLE, DEADLINE_EXCEEDED, открытый circuit breaker | 503 |
| INTERNAL, UNKNOWN | 502 |

Ошибки валидации REST возвращают 422. Положительное целое seat_count,
UUID, email, обязательные поля и IATA-коды проверяются до бизнес-операции.

## gRPC и целостность

Runtime grpcio зафиксирован на 1.71.2: в этой версии исправлена ошибка
переподключения Python-клиента после рестарта сервера
([release notes](https://github.com/grpc/grpc/releases/tag/v1.71.2)).

`SearchFlights`, `GetFlight`, `ReserveSeats`, `ReleaseReservation`, `UpdateFlight`.
Для вызовов требуется metadata `x-api-key: GRPC_API_KEY`, проверка interceptor-ом
выполняется для каждого метода. Proto использует Timestamp и enum для статусов.
Код в `generated/` создан protoc, а не написан вручную. Перегенерация:

```sh
python -m pip install -r requirements.txt
python scripts/generate_proto.py
```

Создание: GetFlight → ReserveSeats → локальный INSERT booking со статусом
CONFIRMED. Цена — снимок GetFlight.price × seat_count; Reserve проверяет
expected_unit_price, чтобы при изменении цены между запросами вернуть 409.
Уменьшение available_seats и INSERT SeatReservation выполняются в одной
транзакции под SELECT FOR UPDATE; возврат мест и RELEASED — аналогично.
Уникальный booking_id и advisory lock предотвращают дублирование даже при
одновременных повторных запросах. Повтор с другими параметрами — ALREADY_EXISTS.

Две независимые БД не имеют общей ACID-транзакции. Booking сначала фиксирует
служебную операцию CREATE, затем удерживает её блокировку во время gRPC и
локальной транзакции. Если резервирование/INSERT не удалось, бронирование
не создаётся, выполняется Release. Если процесс упал или Flight недоступен,
durable операция остаётся для фонового восстановления. Release неизвестного
booking_id сохраняет tombstone: поздний Reserve больше не сможет отнять места.
Это закрывает неоднозначный исход при потерянном ответе или deadline.

Отмена сохраняет durable операцию CANCEL до gRPC Release; при ошибке клиент
получает 503, worker завершает отмену после восстановления Flight. Между БД
возможен временный промежуточный статус; после восстановления зависимостей
операции сходятся. Незавершённые операции не теряются при рестарте контейнера.
Повторный **REST POST /bookings создаёт новое бронирование**; идемпотентность
по booking_id гарантируется для повторов межсервисных RPC, как требует задание.

## Кеш и отказоустойчивость

Cache-aside: Redis GET → при miss PostgreSQL → SET EX. TTL по умолчанию 300 s,
cache hit/miss и ошибки логируются. Ключи:
`flight:{uuid}:v{revision}` и `search:{origin}:{destination}:{date|all}:v{revision}`.
Каждая мутация в одной транзакции с данными повышает PostgreSQL revision.
Клиент кеша читает текущую revision из БД; старые поколения больше недоступны
для новых запросов, физически удаляются по TTL. Это также защищает от гонки,
когда старый cache miss записал данные после мутации, и от потери invalidation
при недоступном Redis. Чтение revision — короткий запрос к БД даже при cache hit;
кеш экономит загрузку/сериализацию рейсов и поисковой выборки. Revision глобальна
для Flight: мутация делает недоступными все старые результаты поиска.

Redis Sentinel: master + replica и три Sentinel с quorum 2. Клиент redis-py
Sentinel сам обнаруживает нового master. У каждого узла отдельный persistent
volume и изменяемая конфигурация; Sentinel сохраняет выбранную топологию.
При недоступном Redis Flight продолжает работать через PostgreSQL.
Redis/Sentinel имеют фиксированные адреса в приватной сети `172.30.76.0/24`:
соединения репликации не зависят от DNS остановленного контейнера, а сохранённая
топология остаётся действительной после пересоздания сети. При конфликте подсетей
задайте `REDIS_NET_PREFIX` до первого запуска.

Retry и circuit breaker реализованы клиентским gRPC interceptor-ом:

- До 3 попыток только для UNAVAILABLE/DEADLINE_EXCEEDED; паузы 100 и 200 ms.
- Каждая попытка ограничена deadline; бизнес-ошибки не повторяются.
- CLOSED → OPEN после порога transient ошибок логических вызовов в окне.
- OPEN быстро возвращает 503 без RPC; после timeout разрешён один HALF_OPEN probe.
- Успех probe (включая ответ с бизнес-ошибкой) закрывает breaker; transient сбой открывает снова.
- Успешные/бизнес-ответы сбрасывают серию ошибок, переходы видны в логах.

Задержка переподключения самого gRPC channel ограничена 100–1000 ms, минимальный
таймаут установки соединения — 500 ms. Это отдельный транспортный механизм:
прикладных попыток RPC по-прежнему не больше трёх, в OPEN RPC не выполняются.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| GRPC_API_KEY | local-development-key | Общий ключ контейнеров |
| FLIGHT_DB_PASSWORD / BOOKING_DB_PASSWORD | *_dev | Раздельные пароли БД |
| BOOKING_PORT | 8080 | REST порт хоста |
| CACHE_TTL_SECONDS | 300 | TTL всех cache entries (300–600) |
| REDIS_NET_PREFIX | 172.30.76 | Префикс приватной /24 сети Compose |
| GRPC_TIMEOUT_SECONDS | 2 | Deadline одной попытки |
| GRPC_BACKOFF_SECONDS | 0.1 | Начальная пауза retry |
| CB_FAILURE_THRESHOLD | 5 | Порог transient ошибок |
| CB_RESET_TIMEOUT_SECONDS | 10 | Время OPEN |
| CB_WINDOW_SECONDS | 30 | Окно счёта ошибок |
| RECOVERY_INTERVAL_SECONDS | 5 | Период worker |
| RECOVERY_GRACE_SECONDS | 30 | Возраст операции до фонового восстановления |

DSN, FLIGHT_GRPC_TARGET, REDIS_SENTINELS и REDIS_MASTER_NAME задаются в Compose.
Ключ и пароли по умолчанию предназначены для локального задания.

## Тесты

```sh
docker compose --profile test run --build --rm tests
# Unit tests без внешних сервисов после установки requirements:
pytest -m 'not integration'
```

Интеграционные тесты используют настоящие PostgreSQL, Redis Sentinel и gRPC
сервер на временном порту. Создают отдельные рейсы и пользователей; не меняют
seed-рейсы. Проверяют конкуренцию за последнее место, повторные резервы,
ошибки/аутентификацию, кеш, REST flow, фиксацию цены, компенсации и recovery.
Транспортные тесты проверяют retry, backoff и автомат circuit breaker с реальным
gRPC-соединением и управляемыми ответами тестового сервера.

```sh
docker compose logs -f booking-service flight-service
docker compose exec sentinel-1 redis-cli -p 26379 SENTINEL get-master-addr-by-name flight-cache
# Автоматическая проверка с восстановлением остановленного узла:
python scripts/check_failover.py
# Проверка OPEN / HALF_OPEN с остановкой и восстановлением Flight:
python scripts/check_circuit.py
# Полный REST-сценарий:
python scripts/smoke.py
```

Остановка без удаления данных: `docker compose down`. Миграции и seed сохраняются
в volumes; повторный запуск не создаёт дубликаты.
