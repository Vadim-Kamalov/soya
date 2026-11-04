from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4
from threading import Event
import grpc
import pytest
from generated import flight_pb2 as pb

pytestmark = pytest.mark.integration


def reserve(stub, flight_id, key, count=1, **kw):
    return stub.ReserveSeats(pb.ReserveSeatsRequest(
        flight_id=flight_id, booking_id=key, seat_count=count, **kw),
        metadata=(("x-api-key", "test-key"),), timeout=5)


@pytest.mark.parametrize("method,rpc_request", [
    ("GetFlight", pb.GetFlightRequest()),
    ("SearchFlights", pb.SearchFlightsRequest()),
    ("ReserveSeats", pb.ReserveSeatsRequest()),
    ("ReleaseReservation", pb.ReleaseReservationRequest()),
    ("UpdateFlight", pb.UpdateFlightRequest()),
])
@pytest.mark.parametrize("credentials", [(), (("x-api-key", "wrong"),)])
def test_all_methods_require_authentication(rpc, method, rpc_request, credentials):
    stub, _, _ = rpc
    with pytest.raises(grpc.RpcError) as error:
        getattr(stub, method)(rpc_request, metadata=credentials)
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_last_seat_cannot_be_reserved_twice(rpc, metadata):
    stub, fid, _ = rpc
    reserve(stub, fid, str(uuid4()))
    def attempt(_):
        try:
            reserve(stub, fid, str(uuid4()))
            return "ok"
        except grpc.RpcError as error:
            return error.code()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 1
    assert results.count(grpc.StatusCode.RESOURCE_EXHAUSTED) == 7
    assert stub.GetFlight(pb.GetFlightRequest(id=fid), metadata=metadata).available_seats == 0


def test_concurrent_duplicate_reserve_is_idempotent(rpc, metadata):
    stub, fid, _ = rpc
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        reservations = list(pool.map(lambda _: reserve(stub, fid, key), range(6)))
    assert len({r.id for r in reservations}) == 1
    assert stub.GetFlight(pb.GetFlightRequest(id=fid), metadata=metadata).available_seats == 1
    with pytest.raises(grpc.RpcError) as error:
        reserve(stub, fid, key, 2)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
    for _ in range(2):
        stub.ReleaseReservation(pb.ReleaseReservationRequest(booking_id=key), metadata=metadata)
    assert stub.GetFlight(pb.GetFlightRequest(id=fid), metadata=metadata).available_seats == 2
    with pytest.raises(grpc.RpcError) as error:
        reserve(stub, fid, key)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_release_unknown_reservation_fences_late_reserve(rpc, metadata):
    stub, fid, _ = rpc
    key = str(uuid4())
    stub.ReleaseReservation(pb.ReleaseReservationRequest(booking_id=key), metadata=metadata)
    with pytest.raises(grpc.RpcError) as error:
        reserve(stub, fid, key)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert stub.GetFlight(pb.GetFlightRequest(id=fid), metadata=metadata).available_seats == 2


def test_search_filters_date_and_status(rpc, metadata):
    stub, fid, _ = rpc
    req = pb.SearchFlightsRequest(origin="SVO", destination="LED")
    req.date.FromDatetime(datetime(2026, 12, 20, tzinfo=timezone.utc))
    assert fid in {f.id for f in stub.SearchFlights(req, metadata=metadata).flights}
    stub.UpdateFlight(pb.UpdateFlightRequest(id=fid, status=pb.CANCELLED), metadata=metadata)
    assert fid not in {f.id for f in stub.SearchFlights(req, metadata=metadata).flights}
    with pytest.raises(grpc.RpcError) as error:
        reserve(stub, fid, str(uuid4()))
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_cache_ttl_and_mutations_invalidate_flight_and_search(rpc, metadata):
    stub, fid, cache = rpc
    get = lambda: stub.GetFlight(pb.GetFlightRequest(id=fid), metadata=metadata)
    search = lambda: stub.SearchFlights(pb.SearchFlightsRequest(origin="SVO", destination="LED"), metadata=metadata)
    assert get().available_seats == 2
    assert next(f for f in search().flights if f.id == fid).available_seats == 2
    # Second reads exercise cache hits. No infinite cache entries.
    get(); search()
    keys = list(cache.redis.scan_iter(f"flight:{fid}:*"))
    assert keys and all(0 < cache.redis.ttl(key) <= 300 for key in keys)
    key = str(uuid4())
    reserve(stub, fid, key)
    assert get().available_seats == 1
    assert next(f for f in search().flights if f.id == fid).available_seats == 1
    stub.UpdateFlight(pb.UpdateFlightRequest(id=fid, price="200.01", total_seats=3), metadata=metadata)
    assert get().price == "200.01"
    assert get().available_seats == 2
    stub.ReleaseReservation(pb.ReleaseReservationRequest(booking_id=key), metadata=metadata)
    assert get().available_seats == 3


def test_price_guard_and_capacity_constraints(rpc, metadata):
    stub, fid, _ = rpc
    with pytest.raises(grpc.RpcError) as error:
        reserve(stub, fid, str(uuid4()), expected_unit_price="1.00")
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    reserve(stub, fid, str(uuid4()), 2)
    with pytest.raises(grpc.RpcError) as error:
        stub.UpdateFlight(pb.UpdateFlightRequest(id=fid, total_seats=1), metadata=metadata)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION


@pytest.mark.parametrize("id,code", [("invalid",grpc.StatusCode.INVALID_ARGUMENT),(str(uuid4()),grpc.StatusCode.NOT_FOUND)])
def test_invalid_and_missing_flights(rpc, metadata, id, code):
    with pytest.raises(grpc.RpcError) as error:
        rpc[0].GetFlight(pb.GetFlightRequest(id=id), metadata=metadata)
    assert error.value.code() == code


def test_old_cache_fill_after_reserve_is_unreachable(rpc, metadata, monkeypatch):
    stub, fid, cache = rpc
    waiting, resume = Event(), Event()
    real_put = cache.put
    def delayed_put(key, revision, message):
        if key == f"flight:{fid}" and not waiting.is_set():
            waiting.set()
            assert resume.wait(3)
        real_put(key, revision, message)
    monkeypatch.setattr(cache, "put", delayed_put)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_read = pool.submit(stub.GetFlight, pb.GetFlightRequest(id=fid), metadata=metadata)
        assert waiting.wait(2)
        try:
            reserve(stub, fid, str(uuid4()))
        finally:
            resume.set()
        assert old_read.result().available_seats == 2
    assert stub.GetFlight(pb.GetFlightRequest(id=fid),metadata=metadata).available_seats == 1


def test_redis_outage_falls_back_to_postgresql(rpc, metadata, monkeypatch):
    from redis.exceptions import ConnectionError
    stub, fid, cache = rpc
    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis down")
    monkeypatch.setattr(cache.redis, "get", unavailable)
    monkeypatch.setattr(cache.redis, "set", unavailable)
    assert stub.GetFlight(pb.GetFlightRequest(id=fid),metadata=metadata).available_seats == 2
    reserve(stub,fid,str(uuid4()))
    assert stub.GetFlight(pb.GetFlightRequest(id=fid),metadata=metadata).available_seats == 1


def test_transaction_rolls_back_seats_and_reservation_on_late_failure(flight_repo, monkeypatch):
    from common.db import connect
    repo, fid = flight_repo
    bid = str(uuid4())
    def fail(conn):
        raise RuntimeError("Injected failure after INSERT")
    monkeypatch.setattr(repo,"_bump",fail)
    with pytest.raises(RuntimeError):
        repo.reserve(fid,1,bid)
    with connect(repo.dsn) as conn:
        assert conn.execute("SELECT available_seats FROM flights WHERE id=%s",(fid,)).fetchone()["available_seats"] == 2
        assert conn.execute("SELECT 1 FROM seat_reservations WHERE booking_id=%s",(bid,)).fetchone() is None


def test_search_maximum_supported_date_does_not_overflow(rpc, metadata):
    request = pb.SearchFlightsRequest(origin="SVO",destination="LED")
    request.date.FromDatetime(datetime(9999,12,31,tzinfo=timezone.utc))
    assert list(rpc[0].SearchFlights(request,metadata=metadata).flights) == []


def test_grpc_retry_after_committed_reserve_does_not_double_book(flight_repo, rpc, metadata):
    from flight_service.server import FlightService, AuthInterceptor
    from generated import flight_pb2_grpc as rpc_pb
    from booking_service.grpc_client import FlightClient
    repo, fid = flight_repo
    class LoseFirstReply(FlightService):
        calls = 0
        def ReserveSeats(self, request, context):
            result = super().ReserveSeats(request,context)
            self.calls += 1
            if self.calls == 1:
                context.abort(grpc.StatusCode.UNAVAILABLE,"reply lost after commit")
            return result
    servicer = LoseFirstReply(repo,rpc[2])
    server = grpc.server(ThreadPoolExecutor(max_workers=4),interceptors=[AuthInterceptor("test-key")])
    rpc_pb.add_FlightServiceServicer_to_server(servicer,server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    client = FlightClient(f"127.0.0.1:{port}","test-key")
    try:
        assert client.reserve(fid,1,str(uuid4()),"123.45").status == pb.ACTIVE
        assert servicer.calls == 2
        assert rpc[0].GetFlight(pb.GetFlightRequest(id=fid),metadata=metadata).available_seats == 1
    finally:
        client.close()
        server.stop(0).wait()

