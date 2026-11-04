from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import grpc
import pytest
from generated import flight_pb2 as pb, flight_pb2_grpc as rpc_pb
from booking_service.grpc_client import ResilienceInterceptor


class FlakyFlight(rpc_pb.FlightServiceServicer):
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = 0
        self.entered = Event()
        self.release = None

    def GetFlight(self, request, context):
        self.calls += 1
        self.entered.set()
        if self.release:
            self.release.wait(2)
        code = self.codes.pop(0) if self.codes else None
        if code:
            context.abort(code, "simulated failure")
        return pb.Flight(id=request.id)


@pytest.fixture
def resilient_rpc():
    resources = []
    def build(codes, **kwargs):
        service = FlakyFlight(codes)
        server = grpc.server(ThreadPoolExecutor(max_workers=4))
        rpc_pb.add_FlightServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        kwargs.setdefault("backoff",0.01)
        kwargs.setdefault("timeout",0.5)
        interceptor = ResilienceInterceptor(**kwargs)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = rpc_pb.FlightServiceStub(grpc.intercept_channel(channel, interceptor))
        resources.append((server, channel))
        return service, interceptor, lambda: stub.GetFlight(pb.GetFlightRequest(id="test"))
    yield build
    for server, channel in resources:
        channel.close()
        server.stop(0).wait()


def test_retry_transient_failures_with_exponential_backoff(resilient_rpc):
    service, _, call = resilient_rpc([grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED])
    start = time.monotonic()
    assert call().id == "test"
    assert service.calls == 3
    assert time.monotonic() - start >= 0.03


@pytest.mark.parametrize("code", [grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND,
    grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.FAILED_PRECONDITION])
def test_business_errors_are_not_retried_or_counted(resilient_rpc, code):
    service, interceptor, call = resilient_rpc([code], threshold=1)
    with pytest.raises(grpc.RpcError) as error:
        call()
    assert error.value.code() == code
    assert service.calls == 1
    assert interceptor.state == "CLOSED"


def test_breaker_opens_and_half_open_probe_closes_it(resilient_rpc):
    service, interceptor, call = resilient_rpc([grpc.StatusCode.UNAVAILABLE] * 3,
        threshold=1, reset_timeout=0.05)
    with pytest.raises(grpc.RpcError):
        call()
    assert service.calls == 3
    assert interceptor.state == "OPEN"
    with pytest.raises(grpc.RpcError) as error:
        call()
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    assert "circuit" in error.value.details().lower()
    assert service.calls == 3
    time.sleep(0.06)
    assert call().id == "test"
    assert interceptor.state == "CLOSED"


def test_half_open_allows_only_one_probe(resilient_rpc):
    service, interceptor, call = resilient_rpc([grpc.StatusCode.UNAVAILABLE] * 3,
        threshold=1, reset_timeout=0.01)
    with pytest.raises(grpc.RpcError):
        call()
    time.sleep(0.02)
    service.entered.clear()
    service.release = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        probe = pool.submit(call)
        assert service.entered.wait(1)
        with pytest.raises(grpc.RpcError):
            call()
        assert service.calls == 4
        service.release.set()
        assert probe.result().id == "test"
    assert interceptor.state == "CLOSED"


def test_failed_probe_reopens_breaker(resilient_rpc):
    _, interceptor, call = resilient_rpc([grpc.StatusCode.UNAVAILABLE] * 6,
        threshold=1, reset_timeout=0.01)
    with pytest.raises(grpc.RpcError):
        call()
    time.sleep(0.02)
    with pytest.raises(grpc.RpcError):
        call()
    assert interceptor.state == "OPEN"


def test_slow_rpc_expires_each_attempt_and_opens_breaker(resilient_rpc):
    service, interceptor, call = resilient_rpc([],timeout=0.03,threshold=1)
    service.release = Event()
    start = time.monotonic()
    try:
        with pytest.raises(grpc.RpcError) as error:
            call()
        assert error.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert service.calls == 3
        assert time.monotonic()-start < 1
        assert interceptor.state == "OPEN"
    finally:
        service.release.set()


def test_existing_client_recovers_after_server_restarts(monkeypatch):
    from booking_service.grpc_client import FlightClient
    monkeypatch.setenv("GRPC_TIMEOUT_SECONDS","0.1")
    monkeypatch.setenv("GRPC_BACKOFF_SECONDS","0.01")
    monkeypatch.setenv("CB_FAILURE_THRESHOLD","1")
    monkeypatch.setenv("CB_RESET_TIMEOUT_SECONDS","0.05")
    def start(port=0):
        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        rpc_pb.add_FlightServiceServicer_to_server(FlakyFlight([]),server)
        bound = server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        return server,bound
    server,port = start()
    client = FlightClient(f"127.0.0.1:{port}","test-key")
    try:
        assert client.get_flight("test").id == "test"
        server.stop(0).wait()
        with pytest.raises(grpc.RpcError):
            client.get_flight("test")
        server,_ = start(port)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            try:
                assert client.get_flight("test").id == "test"
                return
            except grpc.RpcError:
                time.sleep(0.03)
        pytest.fail("Existing gRPC channel failed to recover after restart")
    finally:
        client.close()
        server.stop(0).wait()
