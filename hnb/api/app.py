"""HTTP layer. Thin by design: authenticate, validate the request shape, call
HubService, translate errors. No domain decisions are made here.

Request pipeline (outermost first):
  1. network policy   — only home-network / loopback / private-overlay callers
  2. body size limit  — 64 KiB for API requests
  3. security headers — CSP (self only), no framing, no sniffing, no referrer
  4. route: session cookie → owner; writes also need X-CSRF-Token
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from hnb.auth import AuthError, NewSession, Session
from hnb.config import Settings
from hnb.core.capabilities import catalog as capability_catalog
from hnb.core.model import Ctx
from hnb.security.netpolicy import is_allowed
from hnb.service import HubService, ServiceError

log = logging.getLogger("hnb.api")

COOKIE = "hnb_session"
MAX_BODY = 64 * 1024
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
API_DESCRIPTION = """The Home Network BRAIN Starter API — the same API the Home Hub uses.

**Auth.** Sign in with `POST /api/auth/login`; the session is an HttpOnly cookie.
Every write (POST/PATCH/DELETE) also needs the session's CSRF token in the
`X-CSRF-Token` header (returned by login and `GET /api/session`).

**Network.** The hub answers only callers on the home network, loopback or a
private overlay; others get 403.

**Errors.** Failures return `{"error": "<plain-English reason>"}` with a 4xx/5xx
status. Device commands report truthful outcomes (see `POST /api/devices/{id}/actions`).
"""
OPENAPI_TAGS = [
    {"name": "auth", "description": "First owner, sign-in, sessions, setup completion."},
    {"name": "home", "description": "The Home Hub payload."},
    {"name": "rooms", "description": "Rooms the owner defines."},
    {"name": "devices", "description": "Devices, review decisions, per-device history."},
    {"name": "actions", "description": "Controlling devices, with verified outcomes."},
    {"name": "providers", "description": "Connecting services (provider modules) and their settings."},
    {"name": "discovery", "description": "Finding and adding devices."},
    {"name": "audit", "description": "The append-only history."},
    {"name": "backups", "description": "Verified backups (restore runs on the hub)."},
    {"name": "system", "description": "Health, system facts, capability vocabulary, this schema."},
]


class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        self.status, self.message, self.headers = status, message, headers or {}


# ── request bodies ──────────────────────────────────────────────────────
class BootstrapBody(BaseModel):
    setup_code: str = Field(max_length=64)
    username: str = Field(max_length=64)
    passphrase: str = Field(max_length=1024)
    house_name: str = Field(default="My Home", max_length=120)


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    passphrase: str = Field(max_length=1024)


class ReviewBody(BaseModel):
    status: Literal["MINE", "NOT_MINE", "IGNORED"]


class ActionBody(BaseModel):
    capability: str = Field(max_length=40)
    value: Any = None
    confirm: bool = False


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    room_id: str | None = Field(default=None, max_length=64)
    room_name: str | None = Field(default=None, max_length=120)
    clear_room: bool = False
    favorite: bool | None = None


class RoomBody(BaseModel):
    name: str = Field(max_length=120)


class ProviderBody(BaseModel):
    provider_id: str = Field(max_length=40, description="An installed provider id, e.g. `demo`, `matter`.")
    settings: dict[str, str] = Field(default_factory=dict, description="Values for the provider's `settings` fields.")
    secrets: dict[str, str] = Field(default_factory=dict,
                                    description="Values for the provider's `secrets` fields. Stored encrypted; never returned.")


class ProviderSettingsBody(BaseModel):
    settings: dict[str, str] = Field(default_factory=dict, description="Changed settings (merged into the stored ones).")
    secrets: dict[str, str] = Field(default_factory=dict, description="Replacement credentials; blank keeps the stored one.")


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="The device's setup code (e.g. Matter 11-digit code).")


def create_app(settings: Settings, service: HubService | None = None, *, poll: bool = True) -> FastAPI:
    svc = service or HubService(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start(poll=poll)
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(title="Home Network BRAIN Starter", version=settings.version, lifespan=lifespan,
                  description=API_DESCRIPTION, openapi_tags=OPENAPI_TAGS,
                  docs_url=None, redoc_url=None, openapi_url=None)   # schema served to owners at /api/openapi.json
    app.state.service = svc

    # ── middleware ───────────────────────────────────────────────────────
    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else None
        if not is_allowed(client, settings.allowed_networks):
            return JSONResponse({"error": "This hub only answers on the home network."}, status_code=403)
        if request.url.path.startswith("/api/") and request.method in WRITE_METHODS:
            length = request.headers.get("content-length")
            if length is None and request.headers.get("transfer-encoding"):
                return JSONResponse({"error": "Send the body with a Content-Length."}, status_code=411)
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                return JSONResponse({"error": "Request body is too large."}, status_code=413)
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HttpError)
    async def _http_error(request: Request, exc: HttpError):
        return JSONResponse({"error": exc.message}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
        return JSONResponse({"error": exc.message}, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return JSONResponse({"error": f"Check {field}: {first.get('msg', 'invalid value')}."}, status_code=422)

    # ── helpers ──────────────────────────────────────────────────────────
    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if origin.split("://", 1)[-1] != host:
                raise HttpError(403, "Cross-site request refused.")

    def require_json(request: Request) -> None:
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HttpError(415, "Send JSON.")

    def cookie_secure(request: Request) -> bool:
        mode = settings.cookie_secure
        return mode == "true" or (mode == "auto" and request.url.scheme == "https")

    def set_session_cookie(request: Request, response: Response, s: NewSession) -> None:
        response.set_cookie(COOKIE, s.token, max_age=s.max_age, httponly=True, samesite="strict",
                            secure=cookie_secure(request), path="/")

    # async on purpose: FastAPI runs a plain `def` dependency in a worker thread,
    # and the database connection belongs to the event-loop thread.
    async def owner(request: Request) -> Session:
        session = svc.auth.resolve(request.cookies.get(COOKIE))
        if session is None:
            raise HttpError(401, "Please sign in.")
        if request.method in WRITE_METHODS:
            same_origin(request)
            if not svc.auth.csrf_ok(session, request.headers.get("x-csrf-token")):
                raise HttpError(403, "This request is missing its security token. Reload the page and try again.")
        return session

    def ctx(session: Session) -> Ctx:
        return Ctx(f"owner:{session.username}", "api")

    # ── system ───────────────────────────────────────────────────────────
    @app.get("/api/health", tags=["system"], summary="Health (minimal when signed out, detailed when signed in)")
    async def health(request: Request):
        """Anonymous callers (Docker, the installer) get `status`, `version`,
        `setup_required`. Signed-in owners also get `components`: api, database,
        vault, providers, backup, storage, poller. Never contains secrets."""
        detailed = svc.auth.resolve(request.cookies.get(COOKIE)) is not None
        body = svc.health(detailed)
        return JSONResponse(body, status_code=200 if body["status"] != "error" else 503)

    @app.get("/api/system", tags=["system"], summary="Version, network, counts, installed provider modules")
    async def system(s: Session = Depends(owner)):
        return svc.system_info()

    @app.get("/api/capabilities", tags=["system"], summary="The capability vocabulary and value formats")
    async def capabilities(s: Session = Depends(owner)):
        return {"capabilities": capability_catalog()}

    @app.get("/api/openapi.json", tags=["system"], summary="This API as OpenAPI 3 (owners only)")
    async def openapi_schema(s: Session = Depends(owner)):
        return app.openapi()

    # ── auth ─────────────────────────────────────────────────────────────
    @app.get("/api/session", tags=["auth"], summary="Who am I, is setup open, is setup finished")
    async def session_info(request: Request):
        s = svc.auth.resolve(request.cookies.get(COOKIE))
        base = {"setup_required": svc.auth.setup_required(), "house": svc.home.house.name}
        if s is None:
            return {**base, "authenticated": False}
        return {**base, "authenticated": True, "username": s.username, "csrf_token": s.csrf_token,
                "setup_complete": svc.repo.kv_get("setup_completed_at") is not None}

    @app.post("/api/auth/bootstrap", tags=["auth"], summary="Create the first owner (setup code required)")
    async def bootstrap(body: BootstrapBody, request: Request, response: Response):
        """Only while no owner exists, only from the home network, only with the
        one-time code from `sudo /opt/hnb/bin/setup-code`. Signs the owner in."""
        require_json(request)
        same_origin(request)
        new = await svc.bootstrap(setup_code=body.setup_code, username=body.username,
                                  passphrase=body.passphrase, house_name=body.house_name,
                                  client=client_of(request))
        set_session_cookie(request, response, new)
        return {"authenticated": True, "username": new.username, "csrf_token": new.csrf_token}

    @app.post("/api/auth/login", tags=["auth"], summary="Sign in")
    async def login(body: LoginBody, request: Request, response: Response):
        require_json(request)
        same_origin(request)
        new = await svc.auth.login(username=body.username, passphrase=body.passphrase, client=client_of(request))
        set_session_cookie(request, response, new)
        return {"authenticated": True, "username": new.username, "csrf_token": new.csrf_token}

    @app.post("/api/auth/logout", tags=["auth"], summary="Sign out")
    async def logout(request: Request, response: Response, s: Session = Depends(owner)):
        svc.auth.logout(s, client_of(request))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=cookie_secure(request))
        return {"authenticated": False}

    @app.post("/api/setup/complete", tags=["auth"], summary="Mark the first-run wizard finished")
    async def setup_complete(s: Session = Depends(owner)):
        return svc.complete_setup(ctx(s))

    # ── home ─────────────────────────────────────────────────────────────
    @app.get("/api/home", tags=["home"], summary="Everything the Home Hub shows, in one payload")
    async def home(s: Session = Depends(owner)):
        """Status, rooms, sections (favorites, lights, entertainment, cameras,
        network, more), claimed devices with their controls, and system status.
        Never calls a provider: a slow provider can't slow the Hub."""
        return svc.home_view()

    # ── rooms ────────────────────────────────────────────────────────────
    @app.get("/api/rooms", tags=["rooms"], summary="List rooms")
    async def rooms(s: Session = Depends(owner)):
        return {"rooms": svc.rooms()}

    @app.post("/api/rooms", tags=["rooms"], summary="Create a room (returns the existing one if the name is taken)")
    async def create_room(body: RoomBody, s: Session = Depends(owner)):
        return await svc.create_room(body.name, ctx(s))

    @app.patch("/api/rooms/{room_id}", tags=["rooms"], summary="Rename a room")
    async def rename_room(room_id: str, body: RoomBody, s: Session = Depends(owner)):
        return await svc.rename_room(room_id, body.name, ctx(s))

    @app.delete("/api/rooms/{room_id}", tags=["rooms"], summary="Delete a room (its devices keep working, unassigned)")
    async def delete_room(room_id: str, s: Session = Depends(owner)):
        await svc.delete_room(room_id, ctx(s))
        return {"deleted": room_id}

    # ── devices ──────────────────────────────────────────────────────────
    @app.get("/api/devices", tags=["devices"], summary="Every device the hub knows, with review status")
    async def devices(s: Session = Depends(owner)):
        return {"devices": svc.devices()}

    @app.get("/api/devices/{device_id}", tags=["devices"], summary="One device: capabilities, state, connections")
    async def device(device_id: str, s: Session = Depends(owner)):
        return svc.device_detail(device_id)

    @app.get("/api/devices/{device_id}/history", tags=["devices"], summary="Audit entries for one device")
    async def device_history(device_id: str, limit: int = 50, s: Session = Depends(owner)):
        svc.device_detail(device_id)                     # 404 for unknown devices
        return {"entries": svc.audit(limit, None, device_id)}

    @app.post("/api/devices/{device_id}/review", tags=["devices"], summary="Mark a device MINE / NOT_MINE / IGNORED")
    async def review(device_id: str, body: ReviewBody, s: Session = Depends(owner)):
        return await svc.review(device_id, body.status, ctx(s))

    @app.patch("/api/devices/{device_id}", tags=["devices"], summary="Rename, move to a room, favorite")
    async def patch_device(device_id: str, body: DevicePatch, s: Session = Depends(owner)):
        return await svc.update_device(device_id, ctx(s), name=body.name, room_id=body.room_id,
                                       room_name=body.room_name, clear_room=body.clear_room,
                                       favorite=body.favorite)

    # ── actions ──────────────────────────────────────────────────────────
    @app.post("/api/devices/{device_id}/actions", tags=["actions"], summary="Control a device")
    async def action(device_id: str, body: ActionBody, s: Session = Depends(owner)):
        """Outcome is truthful: CONFIRMED (device read back the value), ACCEPTED
        (sent; not read back), FAILED (provider error or no answer — the device
        may or may not have changed), REJECTED (refused before any provider call:
        not claimed, unsupported capability, bad value, needs `confirm`). REJECTED
        answers HTTP 422; the others 200."""
        result = await svc.act(device_id, body.capability, body.value, ctx(s), confirmed=body.confirm)
        return JSONResponse(result, status_code=422 if result["outcome"] == "REJECTED" else 200)

    # ── providers & discovery ────────────────────────────────────────────
    @app.get("/api/providers", tags=["providers"], summary="Connected services, plus everything that could be added")
    async def providers(s: Session = Depends(owner)):
        """`available` lists installed provider modules (connectable) and known
        providers whose module isn't installed (`installed: false`)."""
        return {"providers": svc.providers(), "available": svc.available_providers()}

    @app.post("/api/providers", tags=["providers"], summary="Connect a service")
    async def connect_provider(body: ProviderBody, s: Session = Depends(owner)):
        return await svc.connect_provider(body.provider_id, body.settings, ctx(s), secrets=body.secrets)

    @app.patch("/api/providers/{provider_id}", tags=["providers"], summary="Change a service's settings or credentials")
    async def provider_settings(provider_id: str, body: ProviderSettingsBody, s: Session = Depends(owner)):
        return await svc.update_provider_settings(provider_id, body.settings, ctx(s), secrets=body.secrets)

    @app.post("/api/providers/{provider_id}/discover", tags=["discovery"], summary="Look for devices now")
    async def discover(provider_id: str, s: Session = Depends(owner)):
        """New devices arrive UNREVIEWED; known devices keep the owner's choices."""
        return await svc.discover_now(provider_id, ctx(s))

    @app.post("/api/providers/{provider_id}/pair", tags=["discovery"], summary="Add a device with a setup code")
    async def pair(provider_id: str, body: PairBody, s: Session = Depends(owner)):
        """Only for providers with `supports_pairing`. The code is never stored or
        logged. Added devices start UNREVIEWED."""
        return await svc.pair_device(provider_id, body.code, ctx(s))

    # ── audit ────────────────────────────────────────────────────────────
    @app.get("/api/audit", tags=["audit"], summary="Append-only history, newest first")
    async def audit(limit: int = 100, before: int | None = None, target: str | None = None,
                    s: Session = Depends(owner)):
        """Page with `before=<seq>`; filter with `target=<device/room/provider id>`."""
        return {"entries": svc.audit(limit, before, target)}

    # ── backups ──────────────────────────────────────────────────────────
    @app.get("/api/backups", tags=["backups"], summary="List backups on the hub")
    async def backups(s: Session = Depends(owner)):
        return {"backups": svc.list_backups(), "status": svc.backup_status()}

    @app.post("/api/backups", tags=["backups"], summary="Back up now (verified; the vault key is never included)")
    async def create_backup(s: Session = Depends(owner)):
        return await svc.create_backup(ctx(s))

    @app.post("/api/backups/{name}/verify", tags=["backups"], summary="Re-verify a backup archive")
    async def verify_backup(name: str, s: Session = Depends(owner)):
        """Restoring replaces the database, so it isn't an API call: stop-and-restore
        runs on the hub with `sudo bin/restore <file>` (see BACKUP_RESTORE.md)."""
        return await svc.verify_backup(name)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def api_404(rest: str):
        raise HttpError(404, "No such endpoint.")

    # ── web app ──────────────────────────────────────────────────────────
    static = {"app.js": "text/javascript", "app.css": "text/css", "favicon.svg": "image/svg+xml"}

    # no-cache = revalidate (ETag) on every load, so an upgraded hub never
    # runs yesterday's JavaScript against today's API.
    revalidate = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=revalidate)

    @app.get("/{name}", include_in_schema=False)
    async def asset(name: str):
        if name in static:
            return FileResponse(WEB_DIR / name, media_type=static[name], headers=revalidate)
        raise HttpError(404, "Not found.")

    @app.get("/fonts/{name}", include_in_schema=False)
    async def font(name: str):
        path = WEB_DIR / "fonts" / name
        if name.endswith(".woff2") and "/" not in name and "\\" not in name and path.is_file():
            return FileResponse(path, media_type="font/woff2", headers={"Cache-Control": "public, max-age=604800"})
        raise HttpError(404, "Not found.")

    return app
