"""End-to-end check against a running Compose deployment."""
import json
import os
import urllib.request
from uuid import uuid4

base = os.getenv("BOOKING_URL", "http://localhost:8080")


def call(method, path, body=None):
    request = urllib.request.Request(base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, json.load(response)


status, flights = call("GET", "/flights?origin=SVO&destination=LED&date=2026-12-01")
assert status == 200 and flights
flight = flights[0]
user = "smoke-" + uuid4().hex
status, booking = call("POST", "/bookings", {"user_id":user,"flight_id":flight["id"],
    "passenger_name":"Smoke Test","passenger_email":"smoke@example.com","seat_count":1})
assert status == 201 and booking["status"] == "CONFIRMED"
assert booking["total_price"] == flight["price"]
assert call("GET", "/bookings?user_id=" + user)[1] == [booking]
assert call("GET", "/flights/" + flight["id"])[1]["available_seats"] == flight["available_seats"] - 1
for _ in range(2):
    assert call("POST", "/bookings/" + booking["id"] + "/cancel")[1]["status"] == "CANCELLED"
assert call("GET", "/flights/" + flight["id"])[1]["available_seats"] == flight["available_seats"]
print("REST -> gRPC -> PostgreSQL + Redis: search/create/list/cancel/idempotent cancel OK")
