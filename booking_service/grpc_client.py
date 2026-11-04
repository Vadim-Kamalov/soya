"""Authentication, retry and circuit breaking at the gRPC transport boundary."""
from collections import deque, namedtuple
from datetime import datetime, time as day_time, timezone
import logging
import os
import threading
import time
import grpc
from generated import flight_pb2 as pb, flight_pb2_grpc as rpc_pb

log = logging.getLogger(__name__)
TRANSIENT = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}


class ClientError(grpc.RpcError):
    def __init__(self, code, details):
        self._code, self._details = code, details
        super().__init__(details)

    def code(self):
        return self._code

    def details(self):
        return self._details


class CallDetails(namedtuple("CallDetails", "method timeout metadata credentials wait_for_ready compression"), grpc.ClientCallDetails):
    pass


class ResilienceInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, *, api_key="", timeout=2, backoff=0.1,
                 threshold=5, reset_timeout=10, window=30):
        if min(timeout, threshold, reset_timeout, window) <= 0 or backoff < 0:
            raise ValueError("Invalid gRPC resilience settings")
        self.api_key, self.timeout, self.backoff = api_key, timeout, backoff
        self.threshold, self.reset_timeout, self.window = threshold, reset_timeout, window
        self.state = "CLOSED"
        self._lock = threading.Lock()
        self._failures = deque()
        self._opened_at = 0
        self._generation = 0

    def _transition(self, state):
        log.info("circuit_breaker %s -> %s", self.state, state)
        self.state = state
        self._generation += 1

    def _before(self):
        with self._lock:
            if self.state == "OPEN" and time.monotonic() - self._opened_at >= self.reset_timeout:
                self._transition("HALF_OPEN")
                return self._generation
            if self.state != "CLOSED":
                raise ClientError(grpc.StatusCode.UNAVAILABLE, "Flight service circuit breaker is open; retry later")
            return self._generation

    def _after(self, generation, failed):
        with self._lock:
            if generation != self._generation:
                return
            now = time.monotonic()
            if not failed:
                self._failures.clear()
                if self.state == "HALF_OPEN":
                    self._transition("CLOSED")
                return
            while self._failures and self._failures[0] < now - self.window:
                self._failures.popleft()
            self._failures.append(now)
            if self.state == "HALF_OPEN" or len(self._failures) >= self.threshold:
                self._opened_at = now
                self._transition("OPEN")

    def intercept_unary_unary(self, continuation, details, request):
        generation = self._before()
        metadata = list(details.metadata or ())
        if self.api_key:
            metadata.append(("x-api-key", self.api_key))
        end = time.monotonic() + details.timeout if details.timeout is not None else None
        last_code = grpc.StatusCode.UNKNOWN
        try:
            for attempt in range(3):
                remaining = end - time.monotonic() if end else self.timeout
                if remaining <= 0:
                    raise ClientError(grpc.StatusCode.DEADLINE_EXCEEDED, "Flight call deadline exceeded")
                call_details = CallDetails(details.method, min(self.timeout, remaining), metadata,
                    details.credentials, False, getattr(details, "compression", None))
                try:
                    call = continuation(call_details, request)
                    call.result()
                    last_code = grpc.StatusCode.OK
                    return call
                except grpc.RpcError as error:
                    last_code = error.code()
                    if last_code not in TRANSIENT or attempt == 2:
                        raise
                    delay = self.backoff * 2**attempt
                    if end:
                        delay = min(delay, max(0, end - time.monotonic()))
                    log.warning("grpc_retry method=%s attempt=%d code=%s backoff=%.3f",
                                details.method, attempt + 1, last_code.name, delay)
                    time.sleep(delay)
        except grpc.RpcError as error:
            last_code = error.code()
            raise
        finally:
            self._after(generation, last_code in TRANSIENT)


class FlightClient:
    def __init__(self, target=None, api_key=None):
        key = api_key or os.environ["GRPC_API_KEY"]
        if not key:
            raise ValueError("GRPC_API_KEY must not be empty")
        self.interceptor = ResilienceInterceptor(api_key=key,
            timeout=float(os.getenv("GRPC_TIMEOUT_SECONDS", "2")),
            backoff=float(os.getenv("GRPC_BACKOFF_SECONDS", "0.1")),
            threshold=int(os.getenv("CB_FAILURE_THRESHOLD", "5")),
            reset_timeout=float(os.getenv("CB_RESET_TIMEOUT_SECONDS", "10")),
            window=float(os.getenv("CB_WINDOW_SECONDS", "30")))
        self.channel = grpc.insecure_channel(target or os.getenv("FLIGHT_GRPC_TARGET", "localhost:50051"),
            options=[("grpc.enable_retries", 0),
                ("grpc.initial_reconnect_backoff_ms", 100),
                ("grpc.min_reconnect_backoff_ms", 500),
                ("grpc.max_reconnect_backoff_ms", 1000),
                ("grpc.dns_min_time_between_resolutions_ms", 1000)])
        self.stub = rpc_pb.FlightServiceStub(grpc.intercept_channel(self.channel, self.interceptor))

    def get_flight(self, id):
        return self.stub.GetFlight(pb.GetFlightRequest(id=str(id)))

    def reserve(self, flight_id, count, booking_id, price):
        return self.stub.ReserveSeats(pb.ReserveSeatsRequest(flight_id=str(flight_id),
            seat_count=count, booking_id=str(booking_id), expected_unit_price=price))

    def release(self, booking_id):
        return self.stub.ReleaseReservation(pb.ReleaseReservationRequest(booking_id=str(booking_id)))

    def search(self, origin, destination, date=None):
        request = pb.SearchFlightsRequest(origin=origin, destination=destination)
        if date:
            request.date.FromDatetime(datetime.combine(date, day_time(), tzinfo=timezone.utc))
        return self.stub.SearchFlights(request)

    def close(self):
        self.channel.close()

