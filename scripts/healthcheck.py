import os
import sys
import urllib.request
import grpc
from generated import flight_pb2 as pb, flight_pb2_grpc as rpc_pb

if sys.argv[1] == "booking":
    urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2).close()
else:
    with grpc.insecure_channel("127.0.0.1:50051", options=[("grpc.enable_retries",0)]) as channel:
        rpc_pb.FlightServiceStub(channel).SearchFlights(pb.SearchFlightsRequest(origin="SVO",destination="LED"),
            metadata=(("x-api-key",os.environ["GRPC_API_KEY"]),), timeout=2)
