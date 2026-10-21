# Marketplace implementation progress

Plan: docs/superpowers/plans/2026-10-20-marketplace.md

Работа выполняется в исходном workspace: репозиторий пуст, кроме пользовательского
задания; отдельный worktree не нужен для изоляции существующей реализации.
Объём всех 10 пунктов и выбор стека разрешены исходным заданием.

Pre-flight: generated router вызывает operationId в handlers; handlers используют
generated DTO; сервисы используют одну transaction/connection на HTTP-операцию.

Task 1: complete — OpenAPI валиден; генерация детерминированна; V1 применена Flyway
11.8.2 к двум чистым PostgreSQL 15.5 базам (marketplace_test и marketplace_demo).
Task 2: complete — auth, products, errors, JSON logging; контракт проверяется
валидатором OAS30 для каждого документированного HTTP-ответа интеграционных тестов.
Task 3: complete — заказы, состояния, блокировки, промокоды; проверены rollback,
конкурентные покупки, единственный активный заказ и последний usage промокода.
Task 4: complete — README, defense.md, admin CLI, HTTP E2E и SELECT таблиц.

Проверка: 43 pytest passed (1 deprecation warning в стороннем Starlette/AnyIO),
ruff check passed, compileall passed, repeated generation hash equality passed.
Независимый обзор: Docker ownership, переполнение stock и datetime input исправлены;
регрессионные тесты прошли RED → GREEN. Предел процентной скидки дополнительно
сохраняется при округлении минимальных сумм до копеек.

Дополнительная проверка после установки Docker Desktop: production/test образы
собраны; 43 pytest passed в Linux-контейнере с PostgreSQL 16.8 и Flyway 11.8.2;
контейнерные Ruff, compileall, pip check и детерминированная генерация прошли.
E2E и SELECT выполнены через контейнер API; healthcheck, Swagger, OpenAPI и
JSON-логи проверены. Основной стек оставлен запущенным, тестовый удалён.

В ходе проверки переполненный C: вызвал ошибку хранилища Docker. По запросу
пользователя программа перенесена в D:\Apps\DockerDesktop, WSL-диски —
в D:\DockerData\wsl с проверкой копий и служебными NTFS junction на старых путях.
Проект сохранён на C:. Из-за занятого 5432 добавлен настраиваемый POSTGRES_PORT;
локальный .env выбирает 55432. Подробности: docs/verification.md.

Повторное ревью: исправлены NUL-ввод и проверка вместимости итоговых сумм
после скидки; уточнены правила ставок выше 70% и снимков цен при PUT.
OpenAPI, README и сценарий защиты приведены к этим правилам.
Регрессии прошли RED (14 failed / 48 passed) → GREEN (62 passed) в Docker;
Ruff, компиляция и повторная генерация также проверены внутри контейнера.
