"""Run from the root with Docker Desktop/Compose running; restores the stopped node."""
import subprocess
import sys
import time


def compose(*args):
    return subprocess.check_output(["docker","compose",*args],text=True,timeout=20).strip()


def role(service):
    try:
        return compose("exec","-T",service,"redis-cli","--raw","ROLE").splitlines()[0]
    except (subprocess.SubprocessError,IndexError):
        return "unavailable"


nodes = ["redis-master","redis-replica"]
master = next(node for node in nodes if role(node) == "master")
other = next(node for node in nodes if node != master)
deadline = time.monotonic()+30
while "master_link_status:up" not in compose("exec","-T",other,"redis-cli","INFO","replication"):
    if time.monotonic() > deadline:
        raise AssertionError("Replica not ready before fault injection")
    time.sleep(0.5)
try:
    compose("stop",master)
    deadline = time.monotonic()+45
    while time.monotonic() < deadline:
        if role(other) == "master":
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Sentinel did not promote replica in 45 seconds")
    # Real application traffic exercises redis-py's Sentinel reconnect.
    subprocess.check_call([sys.executable,"scripts/smoke.py"])
    print(f"Sentinel failover: {master} -> {other}; application flow OK",flush=True)
finally:
    compose("start",master)

deadline = time.monotonic()+30
while time.monotonic() < deadline:
    if role(master) == "slave":
        info = compose("exec","-T",master,"redis-cli","INFO","replication")
        if "master_link_status:up" in info:
            print("Former master rejoined as healthy replica",flush=True)
            break
    time.sleep(0.5)
else:
    raise AssertionError("Former master did not rejoin as replica")
