"""Verify REST 503, fail-fast OPEN and recovery after stopping Flight; restores it."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

base = os.getenv("BOOKING_URL","http://localhost:8080")
url = base + "/flights/11111111-1111-4111-8111-111111111111"


def get():
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url,timeout=10) as response:
            return response.status,json.load(response),time.monotonic()-started
    except urllib.error.HTTPError as error:
        return error.code,json.load(error),time.monotonic()-started


assert get()[0] == 200
try:
    subprocess.check_call(["docker","compose","stop","flight-service"])
    for _ in range(20):
        status,body,elapsed = get()
        assert status == 503,(status,body)
        print(f"Flight stopped: HTTP {status} in {elapsed:.3f}s",flush=True)
        if "circuit" in body.get("detail","").lower():
            assert elapsed < 1,"OPEN should fail immediately"
            print("Circuit OPEN: no timeout, meaningful HTTP 503",flush=True)
            break
    else:
        raise AssertionError("Circuit did not open")
finally:
    subprocess.check_call(["docker","compose","start","flight-service"])

deadline = time.monotonic()+40
while time.monotonic() < deadline:
    status,body,elapsed = get()
    if status == 200:
        print("Flight restored: HALF_OPEN probe succeeded, REST HTTP 200",flush=True)
        break
    print(f"Recovering: HTTP {status} {body.get('detail','')}",flush=True)
    time.sleep(0.5)
else:
    raise AssertionError("Circuit did not recover")
