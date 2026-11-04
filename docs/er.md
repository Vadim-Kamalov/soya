# ER-диаграммы и 3NF

## Flight PostgreSQL

```mermaid
erDiagram
  AIRLINES ||--o{ FLIGHTS : operates
  AIRPORTS ||--o{ FLIGHTS : origin
  AIRPORTS ||--o{ FLIGHTS : destination
  FLIGHTS ||--o{ SEAT_RESERVATIONS : has
  AIRLINES {
    varchar code PK
    text name
  }
  AIRPORTS {
    char code PK
    text name
  }
  FLIGHTS {
    uuid id PK
    text flight_number "unique with UTC departure date"
    varchar airline_code FK
    char origin FK
    char destination FK
    timestamptz departure_at
    timestamptz arrival_at
    int total_seats
    int available_seats
    numeric price
    text status
  }
  SEAT_RESERVATIONS {
    uuid id PK
    uuid booking_id UK "logical reference to Booking DB"
    uuid flight_id FK
    int seat_count
    numeric unit_price "snapshot"
    text status
    timestamptz created_at
  }
  RESERVATION_CANCELLATIONS {
    uuid booking_id PK "durable cancellation fence"
    timestamptz created_at
  }
  CACHE_REVISION {
    boolean singleton PK
    bigint revision
  }
```

## Booking PostgreSQL

```mermaid
erDiagram
  BOOKINGS {
    uuid id PK
    text user_id
    uuid flight_id "logical reference to Flight DB"
    text passenger_name
    text passenger_email
    int seat_count
    numeric total_price "snapshot at booking time"
    text status
    timestamptz created_at
  }
  BOOKING_OPERATIONS {
    uuid booking_id PK "created before a booking exists"
    text kind "CREATE or CANCEL"
    boolean completed
    timestamptz created_at
  }
```

Обе БД также содержат служебную `schema_migrations(version PK, checksum, applied_at)`.
Между независимыми БД нет физического foreign key: соответствие проверяется gRPC
и поддерживается протоколом компенсации. Operations и cancellation fences могут
существовать без Booking/SeatReservation, поэтому FK на них намеренно нет.

Атрибуты атомарны; все неключевые атрибуты зависят от ключа, всего ключа и только
ключа. Названия авиакомпаний и аэропортов вынесены в справочники; рейс не дублирует
их описания. Пассажир — снимок данных конкретного бронирования, а не профиль
пользователя: user_id не определяет имя или email пассажира. Стоимость бронирования
не выводится из текущей цены рейса: это исторический snapshot. Счётчик доступных
мест хранится согласно условию и меняется атомарно вместе с резервациями.

Ограничения SQL: `0 <= available_seats <= total_seats`, `total_seats > 0`,
`seat_count > 0`, цены > 0, `arrival_at > departure_at`, разные origin/destination,
IATA-коды `[A-Z]{3}`, уникальные `(flight_number, UTC departure date)` и
`seat_reservations.booking_id`, допустимые статусы и FK внутри Flight DB.
