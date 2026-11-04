import os
from uuid import uuid4
import pytest

FLIGHT_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def flight_repo():
    if os.getenv("TEST_INTEGRATION") != "1":
        pytest.skip("Run docker compose --profile test run --rm tests")
    from common.db import connect, migrate
    from flight_service.repository import FlightRepository
    dsn = os.environ["FLIGHT_DATABASE_URL"]
    migrate(dsn, "flight")
    # Isolated flight per test; test data does not change seeded flights.
    flight_id = str(uuid4())
    with connect(dsn) as conn:
        conn.execute("""INSERT INTO flights(id,flight_number,airline_code,origin,destination,
            departure_at,arrival_at,total_seats,available_seats,price,status)
            VALUES (%s,%s,'SU','SVO','LED','2026-12-20T10:00Z','2026-12-20T12:00Z',2,2,123.45,'SCHEDULED')""",
            (flight_id, "TEST" + uuid4().hex[:16]))
        conn.execute("UPDATE cache_revision SET revision=revision+1")
    repo = FlightRepository(dsn)
    yield repo, flight_id
    with connect(dsn) as conn:
        conn.execute("DELETE FROM seat_reservations WHERE flight_id=%s", (flight_id,))
        conn.execute("DELETE FROM flights WHERE id=%s", (flight_id,))
        conn.execute("UPDATE cache_revision SET revision=revision+1")


@pytest.fixture
def rpc(flight_repo):
    import grpc
    from concurrent.futures import ThreadPoolExecutor
    from generated import flight_pb2_grpc as rpc_pb
    from flight_service.server import FlightService, AuthInterceptor
    from flight_service.cache import FlightCache, redis_from_env
    repo, flight_id = flight_repo
    cache = FlightCache(redis_from_env(), 300)
    server = grpc.server(ThreadPoolExecutor(max_workers=12), interceptors=[AuthInterceptor("test-key")])
    rpc_pb.add_FlightServiceServicer_to_server(FlightService(repo, cache), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield rpc_pb.FlightServiceStub(channel), flight_id, cache
    channel.close()
    server.stop(0).wait()


@pytest.fixture
def metadata():
    return (("x-api-key", "test-key"),)

