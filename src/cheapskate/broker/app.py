# SPDX-License-Identifier: Apache-2.0
"""FastAPI broker app + capacity enforcement + backend dispatch prep.

The core (auth, gate, capacity, dispatch prep) is plain stdlib and imports
without FastAPI, so it is unit-testable under any environment. :func:`build_app`
and :func:`serve` lazily import FastAPI / httpx / uvicorn — they run only when
the daemon actually serves.

The model-request/download subsystem from the private stack is intentionally
NOT ported: model acquisition is a separate concern owned by the registry layer.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..backends import (
    LocalUnavailable,
    lms_loaded,
    lms_resident_gb,
    lms_unload_all,
    ollama_resident_gb,
    resolve,
)
from ..backends.ollama import ollama_model_resident
from ..backends.preflight import ensure_role
from ..backends.resolve import BackendSpec
from .capacity import capacity_decision, memory_snapshot
from .gates import (
    ModelAwareGate,
    PriorityGate,
    class_for_key,
    load_keys,
    priority_of,
)

# Broker defaults; the config's ``broker`` block overrides these.
DEFAULT_PORT = 4747
DEFAULT_HOST = "127.0.0.1"
# A reasoning model can spend its whole default token budget on chain-of-thought
# and return EMPTY content; floor any chat request that OMITS max_tokens so a
# thinking model has room to think AND answer. Explicit values are respected.
DEFAULT_MAX_TOKENS_FLOOR = 4096


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _broker_cfg(config: Any) -> dict:
    broker = _get(config, "broker", {}) or {}
    if not isinstance(broker, dict):
        broker = {
            "host": _get(broker, "host"),
            "port": _get(broker, "port"),
            "gate": _get(broker, "gate"),
            "max_tokens_floor": _get(broker, "max_tokens_floor"),
            "bind_lan": _get(broker, "bind_lan"),
            "keys_file": _get(broker, "keys_file"),
        }
    return broker


def _budget_gb(config: Any) -> float:
    """RAM budget for loads: explicit ``ram_budget_gb`` override wins; else
    detected/configured total ``ram_gb`` minus headroom; else 0.0 — unknown RAM
    fails closed (every large load is refused rather than gambling on OOM)."""
    machine = _get(config, "machine", {}) or {}
    budget = _get(machine, "ram_budget_gb")
    if budget is not None:
        return float(budget)
    ram = _get(machine, "ram_gb")
    if ram is None:
        return 0.0
    headroom = _get(machine, "ram_headroom_gb", 24)
    return max(float(ram) - float(headroom), 0.0)


# ── Capacity enforcement (runs under the gate; blocking parts go to a thread) ──


def enforce_capacity(spec: BackendSpec, budget_gb: float) -> tuple[str, str]:
    """Apply the capacity decision for an OLLAMA spec before dispatch.

    Returns the ``(action, reason)``. Acts on ``evict-lms``. Raises
    ``RuntimeError('503: ...')`` (fail-closed) when a model alone exceeds the
    whole RAM budget. MLX specs return ``('mlx-gated', ...)`` — their limits are
    enforced inside the backend lifecycle.
    """
    if spec.backend != "ollama":
        return ("mlx-gated", "enforced in the backend lifecycle")
    loaded = lms_loaded()
    lms_gb = lms_resident_gb() if loaded else None
    action, reason = capacity_decision(
        spec.approx_gb,
        ollama_resident_gb(),
        loaded,
        budget_gb,
        model_resident=ollama_model_resident(spec.model),
        lms_gb=(lms_gb if lms_gb and lms_gb > 0 else None),
    )
    if action == "evict-lms":
        lms_unload_all()
    if action == "503":
        raise RuntimeError(f"503: {reason}")
    return (action, reason)


def prepare_backend(spec: BackendSpec, budget_gb: float, config: Any = None) -> str:
    """Ensure the backend for ``spec`` is up and return its OpenAI base URL
    (``.../v1``). For MLX this LOADS the model through the backend lifecycle
    (flock, foreign guard, eviction, one-large-model). BLOCKING — call via a
    thread. Raises :class:`LocalUnavailable` on failure."""
    if spec.backend == "ollama":
        return spec.endpoint.rstrip("/") + "/v1"
    resolved = ensure_role(model=spec.model, config=config, budget_gb=budget_gb)
    return resolved.endpoint.rstrip("/") + "/v1"


# ── FastAPI app (lazy — only when the daemon serves) ─────────────────────────


def _streaming_response_cls():
    """A StreamingResponse subclass whose ``background`` runs on EVERY exit path.

    Starlette runs a response's ``background`` only after a SUCCESSFUL
    ``__call__``; a client disconnect before the first byte skips it AND never
    starts the body generator, which used to leak the gate ticket. This subclass
    takes sole ownership of ``background`` and runs it exactly once always.
    """
    from fastapi.responses import StreamingResponse

    class SafeStreamingResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            background, self.background = self.background, None
            try:
                await super().__call__(scope, receive, send)
            finally:
                if background is not None:
                    await background()

    return SafeStreamingResponse


def build_app(config: Any = None):
    """Construct the FastAPI ASGI app. Imports FastAPI / httpx lazily so the
    module stays importable (and unit-testable) without them.

    ``config`` is a Config object (or dict). It is loaded once here if omitted.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse
    from starlette.background import BackgroundTask
    import httpx

    if config is None:
        from cheapskate.config import load  # provided by the config owner

        config = load()

    SafeStreamingResponse = _streaming_response_cls()

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="cheapskate-broker", version="1.0", lifespan=lifespan)

    broker_cfg = _broker_cfg(config)
    budget_gb = _budget_gb(config)
    gate_kind = (broker_cfg.get("gate") or "serial").lower()
    max_tokens_floor = broker_cfg.get("max_tokens_floor", DEFAULT_MAX_TOKENS_FLOOR)

    app.state.model_aware = gate_kind == "model-aware"
    app.state.gate = (
        ModelAwareGate(budget_gb) if app.state.model_aware else PriorityGate()
    )
    app.state.client = client

    def _log(kind: str, **fields):
        try:
            from cheapskate.telemetry import log_event  # provided by telemetry owner

            log_event(kind, **fields)
        except Exception:  # noqa: BLE001 — telemetry must never break a request
            pass

    def _auth(request):
        hdr = request.headers.get("authorization", "")
        key = hdr.split(" ", 1)[1].strip() if hdr.lower().startswith("bearer ") else ""
        keys = load_keys(config=config)
        cls = class_for_key(key, keys)
        if not cls:
            return (None, None)
        return (cls, keys.get(key, {}).get("user", "?"))

    def _is_loopback(request):
        host = request.client.host if request.client else ""
        return host in ("127.0.0.1", "::1", "localhost")

    async def _proxy_generation(request, path, *, embed=False):
        cls, user = _auth(request)
        if not cls:
            return JSONResponse({"error": "invalid or missing API key"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        task_type = request.headers.get("x-task-type")
        try:
            spec = resolve(model=payload.get("model"), config=config)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": f"model resolution failed: {e}"}, status_code=400
            )

        # Thinking-model floor: a chat request that omits max_tokens would inherit
        # a stingy backend default; only fill the gap, respect explicit values.
        if not embed and max_tokens_floor:
            payload.setdefault("max_tokens", max_tokens_floor)

        gate = app.state.gate
        if app.state.model_aware:
            ticket = await gate.acquire_ticket(
                model=spec.model, size=spec.approx_gb,
                backend=spec.backend, priority=priority_of(cls),
            )
            queued = ticket.queued_s
        else:
            ticket = None
            queued = await gate.acquire(priority_of(cls))

        released = False

        async def _release():
            # Idempotent: the streaming path can reach this from both gen()'s
            # finally and the response BackgroundTask.
            nonlocal released
            if released:
                return
            released = True
            try:
                await asyncio.shield(
                    gate.release(ticket) if app.state.model_aware else gate.release()
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # A failed release must stay VISIBLE and retryable — otherwise the
                # gate wedges silently.
                released = False
                _log("gate_release_failed", model=spec.model, user=user, error=str(e))

        start = time.monotonic()
        route = f"{spec.backend}:{spec.role or spec.model}"
        handed_off = False  # True only once a StreamingResponse OWNS the release
        try:
            try:
                await asyncio.to_thread(enforce_capacity, spec, budget_gb)
                base = await asyncio.to_thread(prepare_backend, spec, budget_gb, config)
            except RuntimeError as e:
                if str(e).startswith("503:"):
                    _log("generation", model=spec.model, route=route, user=user,
                         task_type=task_type, queued_ms=round(queued * 1000),
                         ok=False, error=str(e), status_code=503)
                    return JSONResponse({"error": str(e)[5:].strip()}, status_code=503)
                raise
            except LocalUnavailable as e:
                _log("generation", model=spec.model, route=route, user=user,
                     task_type=task_type, queued_ms=round(queued * 1000),
                     ok=False, error=str(e), status_code=503)
                return JSONResponse({"error": str(e)}, status_code=503)

            payload["model"] = spec.model
            url = base + path
            stream = bool(payload.get("stream")) and not embed

            if stream:
                async def gen():
                    ok = True
                    try:
                        async with app.state.client.stream("POST", url, json=payload) as r:
                            async for chunk in r.aiter_raw():
                                yield chunk
                    except BaseException:
                        ok = False
                        raise
                    finally:
                        _log("generation", model=spec.model, route=route, user=user,
                             task_type=task_type, latency_s=round(time.monotonic() - start, 3),
                             queued_ms=round(queued * 1000), ok=ok)
                        await _release()

                resp = SafeStreamingResponse(
                    gen(), media_type="text/event-stream",
                    background=BackgroundTask(_release),
                )
                handed_off = True
                return resp

            r = await app.state.client.post(url, json=payload)
            _log("generation", model=spec.model, route=route, user=user,
                 task_type=task_type, latency_s=round(time.monotonic() - start, 3),
                 queued_ms=round(queued * 1000), ok=r.status_code < 400,
                 status_code=r.status_code)
            return Response(
                content=r.content, status_code=r.status_code,
                media_type=r.headers.get("content-type", "application/json"),
            )
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            _log("generation", model=spec.model, route=route, user=user,
                 task_type=task_type, latency_s=round(time.monotonic() - start, 3),
                 queued_ms=round(queued * 1000), ok=False, error=err, status_code=502)
            return JSONResponse({"error": err}, status_code=502)
        finally:
            # Every path that did NOT hand the release to a streaming generator
            # releases here — including 503s and raised exceptions on stream:true
            # requests (those previously leaked the gate and wedged the broker).
            if not handed_off:
                await _release()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _proxy_generation(request, "/chat/completions")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await _proxy_generation(request, "/embeddings", embed=True)

    @app.get("/v1/models")
    async def models(request: Request):
        cls, _ = _auth(request)
        if not cls:
            return JSONResponse({"error": "invalid or missing API key"}, status_code=401)
        return _list_models(config)

    @app.get("/admin/status")
    async def admin_status(request: Request):
        if not _is_loopback(request):
            return JSONResponse({"error": "admin is loopback-only"}, status_code=403)
        return {
            "memory": memory_snapshot(budget_gb, config=config),
            "queue_depths": app.state.gate.depths(),
            "busy": app.state.gate._busy,
        }

    return app


def _list_models(config: Any) -> dict:
    """OpenAI /v1/models payload: registry roles as ``role:<name>``."""
    roles = _get(config, "roles", {}) or {}
    if not roles:
        from ..registry import registry as _registry

        roles = _registry.load().get("roles", {}) or {}
    data = []
    for role, spec in roles.items():
        model = _get(spec, "model")
        if model:
            data.append({
                "id": f"role:{role}", "object": "model",
                "owned_by": _get(spec, "backend", "local"),
            })
    return {"object": "list", "data": data}


def serve(config: Any = None) -> None:
    """Run the broker daemon. Binds loopback by default."""
    import uvicorn

    if config is None:
        from cheapskate.config import load

        config = load()
    broker_cfg = _broker_cfg(config)
    host = broker_cfg.get("host") or DEFAULT_HOST
    if broker_cfg.get("bind_lan") and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"  # deliberate opt-in: LAN/tailnet reach for remote machines
    port = int(broker_cfg.get("port") or DEFAULT_PORT)
    uvicorn.run(build_app(config), host=host, port=port, log_level="warning")
