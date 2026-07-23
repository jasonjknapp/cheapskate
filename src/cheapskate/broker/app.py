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
from urllib.parse import urlparse

# ``Request`` must be resolvable in THIS module's globals at route-registration
# time: with ``from __future__ import annotations`` a route's ``request: Request``
# annotation is stored as the string ``"Request"``, and FastAPI resolves it via
# ``get_type_hints`` against the handler's ``__globals__`` (i.e. this module).
# A function-LOCAL ``from fastapi import Request`` is invisible there, so the
# framework mistakes ``request`` for a required query param (HTTP 422 on every
# request). Binding it at module scope fixes that. The import is guarded so the
# module still imports (for the stdlib-only unit tests) when FastAPI is absent.
try:  # pragma: no cover - exercised via the integration smoke, not unit tests
    from fastapi import Request as Request  # noqa: PLC0414 - re-export for annotations
except Exception:  # noqa: BLE001 - FastAPI is optional at import time
    Request = Any  # type: ignore[assignment,misc]

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
    genkey,
    keys_path,
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
_LOCAL_SERVING_BACKENDS = frozenset({"ollama", "mlx", "mlx_vlm", "lmstudio"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _url_host_is_local(url: Any) -> bool:
    """Loopback check on a concrete URL — the endpoint actually dispatched to,
    which may differ from the pre-check spec after backend preparation."""
    try:
        host = urlparse(str(url or "")).hostname
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _never_cloud_spec_is_local(spec: BackendSpec) -> bool:
    return (
        spec.backend in _LOCAL_SERVING_BACKENDS
        and _url_host_is_local(spec.endpoint)
    )


def _broker_cfg(config: Any) -> dict:
    broker = _get(config, "broker", {}) or {}
    if not isinstance(broker, dict):
        broker = {
            "host": _get(broker, "host"),
            "port": _get(broker, "port"),
            "gate": _get(broker, "gate"),
            "max_tokens_floor": _get(broker, "max_tokens_floor"),
            "bind_lan": _get(broker, "bind_lan"),
            "bind_loopback": _get(broker, "bind_loopback"),
            "keys_file": _get(broker, "keys_file"),
        }
    return broker


# Hosts that count as loopback for the bind guard.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_bind_host(config: Any) -> str:
    """Decide the host the broker binds, enforcing the ``bind_loopback`` guard.

    * ``bind_lan`` true → the configured host is used as-is (deliberate opt-in
      for LAN/tailnet reach; a bare loopback host is widened to ``0.0.0.0``).
    * else ``bind_loopback`` true (the default) → the effective host MUST be
      loopback; a non-loopback configured host is a hard startup error naming the
      two ways to allow it.
    * else (``bind_loopback`` explicitly false) → the configured host is used
      as-is, loopback or not.

    Pure (no server started) so the policy is unit-testable directly."""
    broker_cfg = _broker_cfg(config)
    host = broker_cfg.get("host") or DEFAULT_HOST
    bind_lan = bool(broker_cfg.get("bind_lan"))
    # Default to loopback-enforced when the field is absent (matches the config
    # default of bind_loopback=True).
    bl = broker_cfg.get("bind_loopback")
    bind_loopback = True if bl is None else bool(bl)

    if bind_lan:
        if host in ("127.0.0.1", "localhost"):
            return "0.0.0.0"  # deliberate opt-in: LAN/tailnet reach for remote machines
        return host
    if bind_loopback:
        if host not in _LOOPBACK_HOSTS:
            raise RuntimeError(
                f"broker.host is {host!r} but bind_loopback is on: refusing to "
                "bind a non-loopback address. Set broker.bind_lan: true to allow "
                "LAN/tailnet reach, or broker.bind_loopback: false to bind this "
                "host explicitly."
            )
        return host
    return host


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


# ── OpenAI-payload + task_type routing (module-level so they stay testable) ──


def plan_task_type_route(config: Any, task_type: str, dial: Any = None) -> dict:
    """Pure planner for a ``task_type``-tagged OpenAI request. Runs the router's
    routing decision and maps it to a broker action, WITHOUT any ASGI/HTTP
    dependency so it is directly unit-testable.

    Returns a dict with ``action`` ∈ {refuse, cloud, local} plus:
      * refuse → ``status`` (HTTP code) and ``error`` (message), ``class``.
      * cloud  → ``decision`` (the route decision, incl. role).
      * local  → ``decision`` and ``model`` (the ``role:<name>`` to pin, or None).
    """
    from ..router import routes as _routes
    from ..router.dial import read_dial as _read_dial

    if dial is None:
        try:
            dial = _read_dial(config)
        except Exception:  # noqa: BLE001 — a bad dial file must not break the request
            dcfg = _get(config, "dial", None)
            level = _get(dcfg, "default_level", 2)
            sub = _get(dcfg, "default_sub_dial", "std")
            dial = (level, sub)

    decision = _routes.route_decision(task_type, dial, config)
    route = decision["route"]

    if route == _routes.REFUSE_NEVER_LOCAL:
        return {
            "action": "refuse", "status": 422, "class": "never_local",
            "error": f"task_type {task_type!r} is never_local: {decision['reason']}",
        }
    if route == _routes.REFUSE_NEVER_CLOUD:
        return {
            "action": "refuse", "status": 422, "class": "never_cloud",
            "error": f"task_type {task_type!r} is never_cloud: {decision['reason']}",
        }
    if route in (_routes.CLOUD, _routes.CLOUD_DOWNGRADED):
        return {"action": "cloud", "decision": decision}
    # local / unknown
    model = None
    if route == _routes.LOCAL:
        model = f"role:{decision.get('role', 'reasoning')}"
    return {"action": "local", "decision": decision, "model": model}


def cloud_dispatch_openai(
    config: Any,
    body: dict,
    decision: dict,
    task_type: str,
    *,
    max_tokens_floor: int,
    dispatch: Any = None,
    log: Any = None,
) -> tuple[int, dict]:
    """Dispatch a cloud-routed OpenAI payload and return ``(status_code, body)``
    as plain data (no ASGI dependency). A missing/disabled provider or provider
    failure is a fail-closed 502 — never a local fallback. ``dispatch`` is the
    injectable cloud dispatch (defaults to :func:`cheapskate.cloud.dispatch_role`);
    ``log`` is an optional telemetry callback."""
    if dispatch is None:
        from ..cloud import dispatch_role as dispatch

    from ..cloud import CloudError

    def _emit(**fields):
        if log is not None:
            log("generation", **fields)

    role = decision.get("role", "reasoning")
    messages = body.get("messages") or []
    prompt = _last_user_text(messages)
    system = _system_text(messages)
    start = time.monotonic()
    try:
        result = dispatch(
            config, role, prompt,
            system=system,
            temperature=float(body.get("temperature", 0.2)),
            max_tokens=int(body.get("max_tokens") or max_tokens_floor),
        )
    except CloudError as e:
        _emit(task_type=task_type, route="cloud", ok=False, error=str(e), status_code=502)
        return (502, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        _emit(task_type=task_type, route="cloud", ok=False,
              error=f"{type(e).__name__}: {e}", status_code=502)
        return (502, {"error": f"cloud dispatch failed: {e}"})
    _emit(task_type=task_type, route="cloud", model=result.model, ok=True,
          latency_s=round(time.monotonic() - start, 3),
          tokens_in=result.tokens_in, tokens_out=result.tokens_out)
    return (200, _openai_chat_shape(result, task_type))


def _last_user_text(messages: list) -> str:
    """The most recent user message's text from an OpenAI messages array."""
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # content-parts form
                return "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""


def _system_text(messages: list) -> str | None:
    """Concatenated system-message text from an OpenAI messages array, or None."""
    parts = [
        m.get("content", "")
        for m in (messages or [])
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    joined = "\n".join(p for p in parts if p)
    return joined or None


def _openai_chat_shape(result: Any, task_type: str) -> dict:
    """Shape a cloud :class:`CloudResult` as an OpenAI chat-completion object."""
    return {
        "id": f"cheapskate-{task_type}",
        "object": "chat.completion",
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.tokens_in or 0,
            "completion_tokens": result.tokens_out or 0,
            "total_tokens": (result.tokens_in or 0) + (result.tokens_out or 0),
        },
    }


class _RequestWithBody:
    """Wraps a Starlette Request so ``_proxy_generation`` re-reads an already
    parsed+mutated JSON body (the request body stream is single-use). Delegates
    everything else — headers, client — to the real request."""

    def __init__(self, request: Any, body: dict) -> None:
        self._request = request
        self._body = body

    async def json(self) -> dict:
        return self._body

    @property
    def headers(self):
        return self._request.headers

    @property
    def client(self):
        return self._request.client


def prepare_backend(spec: BackendSpec, budget_gb: float, config: Any = None) -> str:
    """Ensure the backend for ``spec`` is up and return its OpenAI base URL
    (``.../v1``). For MLX this LOADS the model through the backend lifecycle
    (flock, foreign guard, eviction, one-large-model). For Ollama the daemon
    auto-loads a present model on request, so this only needs to guarantee the
    model is PRESENT: ``ensure_role`` gate-pulls an absent Ollama model (behind
    the fail-closed disk/size/RAM gate) before the request reaches the daemon.
    BLOCKING; call via a thread. Raises :class:`LocalUnavailable` on failure."""
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

    from fastapi import FastAPI, Response
    from fastapi.responses import JSONResponse
    from starlette.background import BackgroundTask
    import httpx

    # ``Request`` is bound at module scope (see the top-of-file note) so the route
    # annotations resolve; a local re-import here would re-break annotation
    # resolution. Reference the module-level name.
    global Request
    if Request is Any:  # module imported before FastAPI was available
        from fastapi import Request as _Req

        Request = _Req  # type: ignore[assignment,misc]

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
        # An internal call is one cheapskate's own router made (via client.py); the
        # router already emits the costable ``generation`` event, so the broker must
        # NOT emit a second one for these (that double-counts econ). External
        # OpenAI-compat traffic has no such header, so the broker IS its only meter.
        internal = bool(request.headers.get("x-cheapskate-internal"))
        try:
            spec = resolve(model=payload.get("model"), config=config)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": f"model resolution failed: {e}"}, status_code=400
            )
        privacy = request.headers.get("x-model-privacy", "cloud_allowed")
        if privacy not in {"never_cloud", "cloud_allowed"}:
            return JSONResponse({"error": "invalid privacy constraint"}, status_code=400)
        if privacy == "never_cloud" and not _never_cloud_spec_is_local(spec):
            return JSONResponse(
                {"error": "never_cloud route is not a verified local backend"},
                status_code=422,
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
        route = f"{spec.backend}:{spec.role or spec.model}"  # ops label
        handed_off = False  # True only once a StreamingResponse OWNS the release

        def _emit_serve(ok: bool, *, status_code: int | None = None, error: str | None = None):
            """Every served request logs one ops record (kind ``broker.serve``:
            latency/queue/status; the econ report ignores it). For genuinely
            external OpenAI-compat CHAT traffic (no X-Cheapskate-Internal) the
            broker is the only meter, so it ALSO logs one cost-shaped
            ``generation`` event with a clean ``route="local"`` and the request's
            task_type. Internal router calls skip the cost event (the router
            already emitted it); embeddings are not econ tasks, so they get only
            the ops record. ``duration_s`` (not ``latency_s``) is the field the
            econ report reads, so the cost event carries it."""
            duration_s = round(time.monotonic() - start, 3)
            queued_ms = round(queued * 1000)
            _log("broker.serve", model=spec.model, route=route, user=user,
                 task_type=task_type, latency_s=duration_s, queued_ms=queued_ms,
                 ok=ok, status_code=status_code, error=error)
            if not internal and not embed:
                _log("generation", model=spec.model, route="local", user=user,
                     task_type=task_type, duration_s=duration_s, ok=ok,
                     retries=0, escalated=False, error=error)
        try:
            try:
                await asyncio.to_thread(enforce_capacity, spec, budget_gb)
                base = await asyncio.to_thread(prepare_backend, spec, budget_gb, config)
            except RuntimeError as e:
                if str(e).startswith("503:"):
                    _emit_serve(False, status_code=503, error=str(e))
                    return JSONResponse({"error": str(e)[5:].strip()}, status_code=503)
                raise
            except LocalUnavailable as e:
                _emit_serve(False, status_code=503, error=str(e))
                return JSONResponse({"error": str(e)}, status_code=503)

            # never_cloud is verified again against the ACTUAL prepared base URL,
            # not just the pre-check spec: prepare_backend re-resolves the role
            # through ensure_role, so a registry change between the check above and
            # here (or an MLX endpoint assigned during load) could yield a
            # non-loopback target. Fail closed before the prompt leaves the box.
            if privacy == "never_cloud" and not _url_host_is_local(base):
                _emit_serve(False, status_code=422,
                            error="never_cloud prepared endpoint is not local")
                return JSONResponse(
                    {"error": "never_cloud prepared endpoint is not local",
                     "model": spec.model, "backend": spec.backend},
                    status_code=422,
                )

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
                        _emit_serve(ok)
                        await _release()

                resp = SafeStreamingResponse(
                    gen(), media_type="text/event-stream",
                    background=BackgroundTask(_release),
                )
                handed_off = True
                return resp

            r = await app.state.client.post(url, json=payload)
            _emit_serve(r.status_code < 400, status_code=r.status_code)
            return Response(
                content=r.content, status_code=r.status_code,
                media_type=r.headers.get("content-type", "application/json"),
            )
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            _emit_serve(False, status_code=502, error=err)
            return JSONResponse({"error": err}, status_code=502)
        finally:
            # Every path that did NOT hand the release to a streaming generator
            # releases here — including 503s and raised exceptions on stream:true
            # requests (those previously leaked the gate and wedged the broker).
            if not handed_off:
                await _release()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        # The drop-in adoption surface: any OpenAI-client tool pointed at the
        # broker gets econ routing. A ``task_type`` extension field routes the
        # request through the dial/task_types machinery (local vs cloud, safety
        # classes); otherwise it is a direct role/model resolution proxied local.
        cls, user = _auth(request)
        if not cls:
            return JSONResponse({"error": "invalid or missing API key"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)

        if body.get("stream"):
            # Streaming through the task_type econ path is not implemented; a
            # concrete-model/role stream still works via the direct local proxy.
            # 400 invalid_request_error (OpenAI-style) so OpenAI clients handle it
            # gracefully; 501 reads as endpoint-fatal.
            if body.get("task_type"):
                return JSONResponse(
                    {"error": {
                        "message": "streaming is not supported when task_type "
                                   "routing is requested; omit task_type to stream "
                                   "a concrete model/role, or set stream=false",
                        "type": "invalid_request_error",
                        "code": "stream_not_supported",
                    }},
                    status_code=400,
                )

        task_type = body.get("task_type")
        if task_type:
            return await _route_task_type(request, body, task_type, user)
        return await _proxy_generation(request, "/chat/completions")

    async def _route_task_type(request, body, task_type: str, user: str):
        """Run the OpenAI-payload request through the router's routing decision
        (module-level :func:`plan_task_type_route`). A cloud route is dispatched
        via the cloud adapter and returned in OpenAI shape; a local route is
        proxied to the local backend. Safety classes fail closed with a clear
        HTTP status."""
        privacy = request.headers.get("x-model-privacy", "cloud_allowed")
        if privacy not in {"never_cloud", "cloud_allowed"}:
            return JSONResponse({"error": "invalid privacy constraint"}, status_code=400)

        plan = plan_task_type_route(config, task_type)
        action = plan["action"]

        if action == "refuse":
            _log("generation", task_type=task_type, route="refused", user=user,
                 ok=False, error=plan["class"], status_code=plan["status"])
            return JSONResponse({"error": plan["error"]}, status_code=plan["status"])

        # never_cloud must never take the cloud dispatch, even when the dial would
        # otherwise route this task_type to cloud. The local-proxy branch below
        # re-verifies privacy in _proxy_generation; only the cloud branch needs an
        # explicit refusal here.
        if action == "cloud" and privacy == "never_cloud":
            _log("generation", task_type=task_type, route="refused", user=user,
                 ok=False, error="never_cloud forbids a cloud route", status_code=422)
            return JSONResponse(
                {"error": "never_cloud route is not a verified local backend"},
                status_code=422,
            )

        if action == "cloud":
            status, out = await asyncio.to_thread(
                cloud_dispatch_openai, config, body, plan["decision"], task_type,
                max_tokens_floor=max_tokens_floor,
                log=lambda kind, **f: _log(kind, user=user, **f),
            )
            return JSONResponse(out, status_code=status)

        # local / unknown → proxy locally, pinning the route's role when known.
        if plan.get("model") and not body.get("model"):
            body["model"] = plan["model"]
        return await _proxy_generation(_RequestWithBody(request, body), "/chat/completions")

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
    """OpenAI /v1/models payload: the effective role table as ``role:<name>`` ids.

    Uses the same precedence-layered table as resolution (config > registry >
    shipped defaults) so ``/v1/models`` advertises exactly the roles a request
    can target, including the shipped defaults on a fresh install."""
    from ..backends.resolve import _roles

    roles = _roles(config)
    data = []
    for role, spec in roles.items():
        model = _get(spec, "model")
        if model:
            data.append({
                "id": f"role:{role}", "object": "model",
                "owned_by": _get(spec, "backend", "local"),
            })
    return {"object": "list", "data": data}


LOCAL_KEY_CLASS = "interactive"


def ensure_local_key(config: Any = None) -> tuple[str, bool]:
    """Guarantee a usable broker key exists, minting one mode-600 on first run so
    local CLI use works out of the box. The single local user runs at the
    ``interactive`` QoS class (higher priority, never preempted by background
    work); the client finds this key whether it asks for interactive or falls
    back to any registered key. Returns ``(key, created)``: ``created`` is True
    only when a new key was just minted. Remote/LAN callers still need a key;
    this only removes the "cannot talk to my own broker" wall for localhost.
    """
    keys = load_keys(config=config)
    if keys:
        # Any existing key means the operator already provisioned access; do not
        # add a second one. Prefer an interactive key if present, else the first.
        for k, v in keys.items():
            if v.get("class") == LOCAL_KEY_CLASS:
                return k, False
        return next(iter(keys)), False
    key = genkey("local", LOCAL_KEY_CLASS, config=config)
    return key, True


def serve(config: Any = None) -> None:
    """Run the broker daemon. Binds loopback by default, and provisions a local
    interactive key on first run so ``cheapskate task`` works immediately."""
    import uvicorn

    if config is None:
        from cheapskate.config import load

        config = load()
    broker_cfg = _broker_cfg(config)
    host = resolve_bind_host(config)  # enforces bind_loopback / bind_lan policy
    port = int(broker_cfg.get("port") or DEFAULT_PORT)
    key, created = ensure_local_key(config)
    if created:
        print(
            f"[cheapskate] provisioned a local interactive key (saved mode-600 to "
            f"{keys_path(config)}). Local `cheapskate task` and the client use it "
            f"automatically; pass it as a Bearer token for remote/LAN calls."
        )
    uvicorn.run(build_app(config), host=host, port=port, log_level="warning")
