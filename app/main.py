import logging
from pathlib import Path

import yaml
from fastapi import FastAPI

from app.db import transaction
from app.errors import install_handlers
from app.generated.router import router
from app.middleware import RequestLoggingMiddleware

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Marketplace", docs_url="/docs", redoc_url="/redoc")
app.openapi = lambda: yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "src/main/resources/openapi/marketplace.yaml").read_text(
        encoding="utf-8"
    )
)
install_handlers(app)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(router)


@app.get("/health", include_in_schema=False)
def health():
    with transaction() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
