import subprocess
from pathlib import Path
from uuid import uuid4


def main():
    root = Path(__file__).resolve().parent.parent
    compose = [
        "docker", "compose", "--project-name", f"cinema-test-{uuid4().hex}",
        "--file", "compose.yaml", "--file", "tests/compose.yaml", "--profile", "test",
    ]
    try:
        tests = subprocess.run(
            [*compose, "run", "--build", "--rm", "integration-tests"], cwd=root,
        )
    finally:
        cleanup = subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans", "--rmi", "local"], cwd=root,
        )
    return tests.returncode or cleanup.returncode


if __name__ == "__main__":
    raise SystemExit(main())
