import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

# El contenedor `app` comparte network namespace con `provider`
# (network_mode: "service:provider"), por eso el provider es localhost.
PROVIDER_URL = "http://localhost:3001/v1/notify"
API_KEY = "test-dev-2026"

# Por debajo del límite de 50 concurrentes del provider, dejando espacio para reintentos.
WORKERS = 30
HTTP_TIMEOUT = 5.0
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notification-service")


class NotificationIn(BaseModel):
    to: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    type: Literal["email", "sms", "push"]


class CreatedOut(BaseModel):
    id: str


class StatusOut(BaseModel):
    id: str
    status: Literal["queued", "processing", "sent", "failed"]


requests_store: dict[str, dict] = {}
queue: asyncio.Queue[str] = asyncio.Queue()


class RetryableProviderError(Exception):
    pass


@retry(
    retry=retry_if_exception_type(RetryableProviderError),
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential_jitter(initial=0.2, max=2.0),
    reraise=True,
)
async def send_to_provider(client: httpx.AsyncClient, payload: dict) -> None:
    try:
        resp = await client.post(
            PROVIDER_URL,
            json=payload,
            headers={"X-API-Key": API_KEY},
        )
    except httpx.RequestError as e:
        raise RetryableProviderError(str(e)) from e

    if resp.status_code == 200:
        return
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryableProviderError(f"provider {resp.status_code}")
    raise RuntimeError(f"provider rejected request: {resp.status_code} {resp.text}")


async def worker(client: httpx.AsyncClient) -> None:
    while True:
        req_id = await queue.get()
        try:
            entry = requests_store.get(req_id)
            if entry is None:
                continue
            entry["status"] = "processing"
            try:
                await send_to_provider(client, entry["payload"])
                entry["status"] = "sent"
            except Exception as e:
                entry["status"] = "failed"
                log.warning("request %s failed: %s", req_id, e)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=limits) as client:
        tasks = [asyncio.create_task(worker(client)) for _ in range(WORKERS)]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Notification Service", lifespan=lifespan)


@app.post("/v1/requests", response_model=CreatedOut, status_code=status.HTTP_201_CREATED)
async def create_request(body: NotificationIn) -> CreatedOut:
    req_id = uuid.uuid4().hex
    requests_store[req_id] = {"status": "queued", "payload": body.model_dump()}
    return CreatedOut(id=req_id)


@app.post("/v1/requests/{req_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def process_request(req_id: str) -> dict:
    if req_id not in requests_store:
        raise HTTPException(status_code=404, detail="request not found")
    await queue.put(req_id)
    return {"id": req_id, "status": "queued"}


@app.get("/v1/requests/{req_id}", response_model=StatusOut)
async def get_request(req_id: str) -> StatusOut:
    entry = requests_store.get(req_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="request not found")
    return StatusOut(id=req_id, status=entry["status"])
