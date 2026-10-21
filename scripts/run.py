import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "scripts.generate"], check=True)
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, access_log=False)
