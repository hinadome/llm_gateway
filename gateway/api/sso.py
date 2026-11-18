"""The dashboard SSO routes (tier 60) — the OIDC authorization-code
flow's endpoints, all under /admin/ui/sso/ (beside the page they
serve).

Deliberately public: these ARE the login (a credential gate on them
would be circular). The anti-abuse story is the standard OAuth one —
random ``state`` bound to an HttpOnly SameSite=Lax cookie (CSRF),
PKCE S256 (code interception), and the email allowlist at the
callback — not a dashboard credential.

Session transport (the design decision worth restating): the session
token lives in an HttpOnly cookie; ``POST /admin/ui/sso/session``
exchanges it once for the Bearer the dashboard's ``api()`` uses.
The admin API itself stays Bearer-only — zero new auth paths on
every /admin/* endpoint, zero CSRF surface — while the browser never
holds the raw admin key, only a short-lived, identified, revocable
session token.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..core.sso import SsoError, SsoService
from ..errors import GatewayError

router = APIRouter()

# Cookie names + lifetimes. The state cookie carries the OAuth state
# AND the PKCE verifier as a flat ``state:verifier`` pair (both are
# URL-safe tokens, the colon unambiguous — JSON in a Set-Cookie value
# gets mangled by cookie-jar quoting rules); HttpOnly, so the verifier
# never reaches page JS. The session cookie carries the minted session
# token. Both are scoped to the SSO/dashboard paths — they ride no
# /admin/* API call, which is what keeps the API Bearer-only.
STATE_COOKIE = "gw_sso_state"
SESSION_COOKIE = "gw_sso_session"
STATE_COOKIE_MAX_AGE = 300  # 5 minutes to finish a login


def _sso_or_404(request: Request) -> SsoService:
    """The SSO service, or the honest 404 when SSO is off — "disabled"
    is a state with an explanation, not a connection error."""
    sso = getattr(request.app.state, "sso", None)
    if sso is None:
        raise GatewayError(
            404,
            "Single sign-on is not enabled on this gateway. "
            "Set auth.sso.enabled (see config.yaml) and sign in with "
            "an admin key.",
            "invalid_request_error",
            code="sso_disabled",
        )
    return sso


def _cookie_secure(request: Request) -> bool:
    # Secure when the request itself is TLS — never guess: forcing the
    # flag over plain http silently drops the cookie (an invisible
    # local-dev breakage), and skipping it on https would leak the
    # session over the wire.
    return request.url.scheme == "https"


@router.get("/admin/ui/sso/config")
async def sso_config(request: Request) -> dict:
    """The login overlay's probe: is SSO on, and what should the
    button say? Public by design — this is the sign-in page."""
    sso = getattr(request.app.state, "sso", None)
    if sso is None:
        return {"enabled": False, "label": None}
    return {"enabled": True, "label": sso.config.label}


@router.get("/admin/ui/sso/login")
async def sso_login(request: Request) -> RedirectResponse:
    """Start the OIDC flow: 302 to the provider's authorization
    endpoint, with state + PKCE verifier tucked into a short-lived
    HttpOnly cookie."""
    sso = _sso_or_404(request)
    try:
        url, state, verifier = await sso.prepare_login()
    except SsoError as exc:
        raise GatewayError(
            503,
            f"Sign-in could not start: {exc.message}",
            "api_error",
            code=exc.reason,
        ) from exc
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        f"{state}:{verifier}",
        max_age=STATE_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/admin/ui/sso",
    )
    return resp


@router.get("/admin/ui/sso/callback")
async def sso_callback(request: Request) -> RedirectResponse:
    """The IdP's redirect target: validate the state cookie, exchange
    the code, apply the allowlist, set the session cookie, land back on
    the dashboard — errors as query params the page renders, never a
    bare stack trace."""
    sso = _sso_or_404(request)

    def back(**params: str) -> RedirectResponse:
        query = urlencode({k: v for k, v in params.items() if v})
        return RedirectResponse(f"/admin/ui?{query}", status_code=302)

    # Provider-side errors (the user denied consent, &c): ?error=...
    error = request.query_params.get("error")
    if error:
        return back(sso="error", reason=error)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    cookie_state = request.cookies.get(STATE_COOKIE)
    if not code or not state or not cookie_state:
        return back(sso="error", reason="missing_code_or_state")
    # "state:verifier" — the flat pair the login route set.
    state_parts = cookie_state.split(":")
    if len(state_parts) != 2:
        return back(sso="error", reason="bad_state_cookie")
    carried_state, verifier = state_parts
    if carried_state != state:
        # The login did not originate in this browser — CSRF or replay.
        # The code is NOT exchanged: no redemption of a stolen
        # authorization code.
        return back(sso="error", reason="state_mismatch")

    client_secret = getattr(request.app.state, "sso_client_secret", None)
    try:
        session = await sso.complete_login(code, verifier, client_secret)
    except SsoError as exc:
        return back(sso="error", reason=exc.reason, message=exc.message)
    resp = back(sso="ok")
    resp.set_cookie(
        SESSION_COOKIE,
        session["token"],
        max_age=int(sso.config.session_ttl_seconds),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/admin/ui",
    )
    # The state cookie has served its purpose.
    resp.delete_cookie(STATE_COOKIE, path="/admin/ui/sso")
    return resp


@router.post("/admin/ui/sso/session")
async def sso_session(request: Request) -> dict:
    """Exchange the session cookie for the Bearer the dashboard's
    api() sends. SameSite=Lax + no CORS on this route: a cross-site
    POST may mint a token it can never read. 401-honest when the
    cookie is absent or the session expired."""
    sso = _sso_or_404(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise GatewayError(
            401,
            "No sign-in session — start SSO from the dashboard.",
            "invalid_request_error",
            code="no_session",
        )
    email = sso.verify_session(token)  # raises AuthError → the 401 envelope
    return {
        "token": token,
        "email": email,
        "expires_in": int(sso.config.session_ttl_seconds),
    }


@router.post("/admin/ui/sso/logout")
async def sso_logout(request: Request) -> JSONResponse:
    """Drop the session cookie (local revocation: a stateless session
    cannot be un-issued, so its short TTL is the real bound — the
    documented trade). The Bearer copy in the page's sessionStorage
    dies with its exp; the page clears its copy on logout too."""
    _sso_or_404(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/admin/ui")
    resp.delete_cookie(STATE_COOKIE, path="/admin/ui/sso")
    return resp
