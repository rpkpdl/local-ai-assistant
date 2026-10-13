"""Web UI for the personal assistant.

Run:  python server.py   ->  http://127.0.0.1:8000

Local-only by design: binds to 127.0.0.1, rejects requests whose Host isn't
localhost (DNS rebinding), and rejects cross-origin / non-JSON POSTs so other
websites open in your browser can't drive the agent.
"""

import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent import EFFORT, MODEL, Agent

HOST, PORT = "127.0.0.1", 8000
STATIC = Path(__file__).parent / "static"

agent = Agent()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


def check_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
        raise HTTPException(403, "cross-origin request refused")
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON required")


class ChatIn(BaseModel):
    message: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    return {"model": MODEL, "effort": EFFORT, "tracing": agent.tracing,
            "search": agent.search_error is None, "search_error": agent.search_error}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    check_same_origin(request)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "empty message")
    # Newline-delimited JSON: one event per line, flushed as it happens
    events = (json.dumps(ev, default=str) + "\n" for ev in agent.stream_turn(text))
    return StreamingResponse(events, media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store"})


@app.post("/api/reset")
def reset(request: Request):
    check_same_origin(request)
    agent.reset()
    return {"ok": True}


@app.get("/api/memories")
def memories():
    return [{"id": m.id, "content": m.content, "tags": m.tags, "created_at": m.created_at}
            for m in agent.store.all()]


@app.get("/api/sources")
def sources():
    return [vars(s) for s in agent.sources.all()]


if __name__ == "__main__":
    print(f"Open http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
