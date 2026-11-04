from uuid import uuid4
from decimal import Decimal
import grpc
import pytest
from fastapi.testclient import TestClient
from generated import flight_pb2 as pb

pytestmark = pytest.mark.integration


@pytest.fixture
def booking_api(rpc):
    import os
    from common.db import migrate, connect
    from booking_service.service import BookingService
    from booking_service.app import create_app
    stub, fid, _ = rpc
    class LocalClient:
        def get_flight(self, id):
            return stub.GetFlight(pb.GetFlightRequest(id=id), metadata=(("x-api-key","test-key"),))
        def reserve(self, fid, count, bid, price):
            return stub.ReserveSeats(pb.ReserveSeatsRequest(flight_id=fid,seat_count=count,
                booking_id=bid,expected_unit_price=price), metadata=(("x-api-key","test-key"),))
        def release(self, bid):
            return stub.ReleaseReservation(pb.ReleaseReservationRequest(booking_id=bid), metadata=(("x-api-key","test-key"),))
        def search(self, origin, destination, date=None):
            req=pb.SearchFlightsRequest(origin=origin,destination=destination)
            if date:
                from datetime import datetime, timezone
                req.date.FromDatetime(datetime.combine(date,datetime.min.time(),tzinfo=timezone.utc))
            return stub.SearchFlights(req,metadata=(("x-api-key","test-key"),))
    dsn = os.environ["BOOKING_DATABASE_URL"]
    migrate(dsn, "booking")
    service = BookingService(dsn, LocalClient(), recovery_grace=0)
    user_id = "test-" + uuid4().hex
    body = {"user_id":user_id,"flight_id":fid,"passenger_name":"Иван Иванов",
        "passenger_email":"ivan@example.com","seat_count":2}
    with TestClient(create_app(service, start_worker=False)) as api:
        yield api, body, service, stub
    with connect(dsn) as conn:
        conn.execute("DELETE FROM booking_operations WHERE booking_id IN (SELECT id FROM bookings WHERE user_id=%s)", (user_id,))
        conn.execute("DELETE FROM bookings WHERE user_id=%s", (user_id,))


def test_rest_booking_flow_and_price_snapshot(booking_api, metadata):
    api, body, _, stub = booking_api
    response = api.post("/bookings", json=body)
    assert response.status_code == 201, response.text
    booking = response.json()
    assert booking["status"] == "CONFIRMED"
    assert booking["total_price"] == "246.90"
    assert api.get(f"/bookings/{booking['id']}").json() == booking
    assert api.get("/bookings", params={"user_id":body["user_id"]}).json() == [booking]
    assert api.get("/bookings", params={"user_id":"someone-else"}).json() == []
    stub.UpdateFlight(pb.UpdateFlightRequest(id=body["flight_id"], price="500.00"), metadata=metadata)
    assert api.get(f"/bookings/{booking['id']}").json()["total_price"] == "246.90"
    for _ in range(2):
        cancelled = api.post(f"/bookings/{booking['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2


def test_insufficient_seats_does_not_create_booking(booking_api):
    api, body, _, _ = booking_api
    body["seat_count"] = 3
    assert api.post("/bookings", json=body).status_code == 409
    assert api.get("/bookings", params={"user_id":body["user_id"]}).json() == []
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2


def test_local_save_failure_compensates_reservation(booking_api, monkeypatch):
    import psycopg
    api, body, service, _ = booking_api
    def fail(*args):
        raise psycopg.OperationalError("simulated commit path failure")
    monkeypatch.setattr(service, "_save_booking", fail)
    assert api.post("/bookings", json=body).status_code == 503
    assert api.get("/bookings", params={"user_id":body["user_id"]}).json() == []
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2


def test_recovery_releases_reservation_after_process_crash(booking_api):
    from common.db import connect
    api, body, service, _ = booking_api
    bid = str(uuid4())
    with connect(service.dsn) as conn:
        conn.execute("INSERT INTO booking_operations(booking_id,kind) VALUES (%s,'CREATE')", (bid,))
    service.client.reserve(body["flight_id"], 2, bid, "123.45")
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 0
    service.recover_once()
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2
    with connect(service.dsn) as conn:
        assert conn.execute("SELECT completed FROM booking_operations WHERE booking_id=%s", (bid,)).fetchone()["completed"]


def test_cancellation_recovers_after_release_response_loss(booking_api, monkeypatch):
    api, body, service, _ = booking_api
    bid = api.post("/bookings", json=body).json()["id"]
    real_release = service.client.release
    from booking_service.grpc_client import ClientError
    def lost_response(key):
        real_release(key)
        raise ClientError(grpc.StatusCode.UNAVAILABLE, "response lost")
    monkeypatch.setattr(service.client, "release", lost_response)
    assert api.post(f"/bookings/{bid}/cancel").status_code == 503
    monkeypatch.setattr(service.client, "release", real_release)
    service.recover_once()
    assert api.get(f"/bookings/{bid}").json()["status"] == "CANCELLED"
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2


def test_invalid_inputs_and_missing_resources(booking_api):
    api, body, _, _ = booking_api
    for patch in [{"seat_count":0},{"seat_count":True},{"passenger_email":"bad"},
                  {"passenger_name":" "},{"user_id":" "},{"flight_id":"bad"}]:
        assert api.post("/bookings", json={**body,**patch}).status_code == 422
    assert api.get("/bookings").status_code == 422
    assert api.get(f"/bookings/{uuid4()}").status_code == 404
    assert api.post(f"/bookings/{uuid4()}/cancel").status_code == 404
    assert api.get(f"/flights/{uuid4()}").status_code == 404
    assert api.get("/flights",params={"origin":"SVO","destination":"LED","date":"bad"}).status_code == 422
    found = api.get("/flights",params={"origin":"SVO","destination":"LED","date":"2026-12-20"})
    assert found.status_code == 200
    assert body["flight_id"] in {f["id"] for f in found.json()}
    assert api.get("/flights",params={"origin":"SVO","destination":"LED","date":"9999-12-31"}).json() == []


def test_lost_reserve_response_is_compensated(booking_api, monkeypatch):
    from booking_service.grpc_client import ClientError
    api, body, service, _ = booking_api
    real_reserve = service.client.reserve
    def lost_response(*args):
        real_reserve(*args)
        raise ClientError(grpc.StatusCode.DEADLINE_EXCEEDED,"response lost after reserve committed")
    monkeypatch.setattr(service.client,"reserve",lost_response)
    assert api.post("/bookings",json=body).status_code == 503
    assert api.get("/bookings",params={"user_id":body["user_id"]}).json() == []
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2


def test_concurrent_booking_cancellations_return_seats_once(booking_api):
    from concurrent.futures import ThreadPoolExecutor
    api, body, service, _ = booking_api
    bid = api.post("/bookings",json=body).json()["id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _:service.cancel(bid),range(6)))
    assert all(row["status"] == "CANCELLED" for row in results)
    assert api.get(f"/flights/{body['flight_id']}").json()["available_seats"] == 2
