from concurrent.futures import ThreadPoolExecutor
import grpc
import pytest
from generated import flight_pb2 as pb, flight_pb2_grpc as rpc_pb
from flight_service.server import AuthInterceptor, FlightService


def test_missing_api_key_is_rejected_before_business_logic():
    server = grpc.server(ThreadPoolExecutor(max_workers=1), interceptors=[AuthInterceptor("test-key")])
    rpc_pb.add_FlightServiceServicer_to_server(FlightService(None, None), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            with pytest.raises(grpc.RpcError) as error:
                rpc_pb.FlightServiceStub(channel).GetFlight(pb.GetFlightRequest())
            assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        server.stop(0).wait()
