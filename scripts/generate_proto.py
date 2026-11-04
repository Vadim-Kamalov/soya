"""Run from the repository root: python scripts/generate_proto.py."""
from pathlib import Path
import grpc_tools
from grpc_tools import protoc

root = Path(__file__).resolve().parents[1]
output = root / "generated"
output.mkdir(exist_ok=True)
(output / "__init__.py").touch()
result = protoc.main([
    "protoc", f"-I{root / 'proto'}",
    f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
    f"--python_out={output}", f"--grpc_python_out={output}",
    str(root / "proto/flight.proto"),
])
if result:
    raise SystemExit(result)
# protoc emits a top-level import; package it for both services.
stub = output / "flight_pb2_grpc.py"
stub.write_text(stub.read_text().replace(
    "import flight_pb2 as flight__pb2", "from . import flight_pb2 as flight__pb2"
))

