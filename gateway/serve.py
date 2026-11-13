"""``python -m gateway`` — run the gateway over HTTP or HTTPS.

A thin, documented layer over uvicorn that reads the same ``Settings``
as the app (``GATEWAY_*`` env vars / ``.env``) and adds TLS with three
certificate stories:

- **self-signed** — ``--self-signed [DIR]``: a development certificate
  (SANs ``localhost``, ``127.0.0.1``, ``::1``) generated on first use
  under ``deploy/certs/`` and reused afterwards. Clients must trust it
  explicitly (``curl --cacert``, SDK ``verify``/``http_client``) or
  skip verification — it is not a publicly trusted certificate.
- **private** — ``--ssl-cert FILE --ssl-key FILE``: any PEM pair, e.g.
  issued by an organizational CA or bought from a commercial CA.
- **Let's Encrypt** — ``--letsencrypt DOMAIN``: resolves certbot's
  standard layout (``/etc/letsencrypt/live/DOMAIN/fullchain.pem`` +
  ``privkey.pem``). After certbot renews, restart the gateway — the
  certificate is read once at bind time, not per connection.

With HTTPS on, ``--redirect-http [PORT]`` additionally listens on
plain HTTP (default port 80) and answers every request with a 308
Permanent Redirect to the same URL on HTTPS — browsers and SDKs that
default to ``http://`` are bounced to the encrypted listener instead
of a connection error.

Plain HTTP when no certificate is configured (today's behavior).
Precedence: CLI flags > ``GATEWAY_SSL_CERT``/``GATEWAY_SSL_KEY`` env
vars. The same env vars also work without this module for operators
who prefer plain uvicorn::

    uv run uvicorn gateway.main:app \
        --ssl-certfile "$GATEWAY_SSL_CERT" --ssl-keyfile "$GATEWAY_SSL_KEY"

``scripts/gateway.sh`` wraps this module (uv run + repo root).
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import logging
import os
import stat
import threading
from pathlib import Path

from .config import Settings

logger = logging.getLogger("gateway.serve")

# Where --self-signed keeps the dev certificate pair.
DEFAULT_CERT_DIR = Path("deploy/certs")
# certbot's standard live-certificate layout.
LETSENCRYPT_BASE = Path("/etc/letsencrypt/live")
# Dev-certificate lifetime. Short enough to matter, long enough not to
# annoy: regenerating is one flag away (delete the pair and rerun).
SELF_SIGNED_DAYS = 365


class TlsConfigError(Exception):
    """Misconfigured TLS (bad flag combination, missing files...) —
    surfaces as a CLI usage error, never as a stack trace."""


# ----------------------------------------------------------------------
# Certificate generation
# ----------------------------------------------------------------------


def generate_self_signed_cert(
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    common_name: str = "localhost",
    days: int = SELF_SIGNED_DAYS,
) -> None:
    """Write a self-signed server certificate + RSA private key pair.

    For development and internal use: clients must trust the
    certificate explicitly (or skip verification). The SANs cover
    ``localhost``, ``127.0.0.1`` and ``::1`` so a client can verify both
    names against the same pair. Requires the ``cryptography``
    package (imported lazily — the gateway core never needs it).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - depends on install
        raise TlsConfigError(
            "generating a self-signed certificate requires the "
            "'cryptography' package (uv sync installs it; or pass "
            "--ssl-cert/--ssl-key with your own pair)"
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    # CA:TRUE so the pair also works loaded as a trust anchor (the
    # shape `openssl req -x509` produces; SANs are what matter for
    # hostname verification).
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))  # clock skew
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    cert_file, key_file = Path(cert_path), Path(key_path)
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # The private key is the secret; the certificate is public.
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.chmod(cert_file, 0o644)


def ensure_self_signed(
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    days: int = SELF_SIGNED_DAYS,
) -> bool:
    """Generate the pair unless both files already exist.

    Returns True when a fresh pair was written, False when reusing.
    A half pair (one file missing) is regenerated whole — a dev cert
    costs nothing and self-heals; a deployed pair never lives in the
    dev directory anyway. Existing files are never overwritten, which
    is also what makes Let's Encrypt paths safe to point at.
    """
    cert_file, key_file = Path(cert_path), Path(key_path)
    if cert_file.is_file() and key_file.is_file():
        return False
    generate_self_signed_cert(cert_file, key_file, days=days)
    return True


def letsencrypt_paths(
    domain: str, base: Path = LETSENCRYPT_BASE
) -> tuple[Path, Path]:
    """certbot's live paths for ``domain`` — validated to exist.

    Standard certbot layout; a nonstandard location can be passed with
    plain ``--ssl-cert`` / ``--ssl-key``.
    """
    cert = base / domain / "fullchain.pem"
    key = base / domain / "privkey.pem"
    if not cert.is_file() or not key.is_file():
        raise TlsConfigError(
            f"Let's Encrypt certificate for {domain!r} not found "
            f"(looked for {cert} and {key}). Obtain one with e.g. "
            f"certbot certonly --standalone -d {domain}, or point "
            "--ssl-cert/--ssl-key at the files directly."
        )
    return cert, key


# ----------------------------------------------------------------------
# SSL resolution (CLI flags > GATEWAY_* env)
# ----------------------------------------------------------------------


def _validated_pair(cert: str | None, key: str | None) -> tuple[Path, Path]:
    """Both-or-neither + files must exist → resolved path pair."""
    if bool(cert) != bool(key):
        raise TlsConfigError(
            "--ssl-cert and --ssl-key (or GATEWAY_SSL_CERT and "
            "GATEWAY_SSL_KEY) must be set together"
        )
    assert cert is not None and key is not None  # both-or-neither checked
    for path, name in ((cert, "certificate"), (key, "private key")):
        if not Path(path).is_file():
            raise TlsConfigError(f"{name} file not found: {path}")
    return Path(cert), Path(key)


def resolve_ssl(
    args: argparse.Namespace, settings: Settings
) -> tuple[Path, Path] | None:
    """Which PEM pair to serve with, or None for plain HTTP.

    Precedence: ``--self-signed`` / ``--letsencrypt`` / explicit
    ``--ssl-cert``+``--ssl-key`` flags, then the ``GATEWAY_SSL_*``
    settings. Only one mode may be chosen.
    """
    explicit = bool(args.ssl_cert or args.ssl_key)
    if (args.self_signed is not None or args.letsencrypt) and explicit:
        raise TlsConfigError(
            "choose one certificate mode: --self-signed, --letsencrypt "
            "or --ssl-cert/--ssl-key"
        )
    if args.self_signed is not None:
        # nargs="?" — bare flag uses the default dev directory.
        cert_dir = Path(args.self_signed)
        cert, key = cert_dir / "gateway-dev.pem", cert_dir / "gateway-dev-key.pem"
        generated = ensure_self_signed(cert, key)
        logger.info(
            "%s self-signed certificate %s (valid %d days; clients must "
            "trust it or skip verification)",
            "generated" if generated else "reusing existing",
            cert,
            SELF_SIGNED_DAYS,
        )
        return cert, key
    if args.letsencrypt:
        return letsencrypt_paths(args.letsencrypt)
    if explicit:
        return _validated_pair(args.ssl_cert, args.ssl_key)
    if settings.ssl_cert or settings.ssl_key:
        return _validated_pair(settings.ssl_cert, settings.ssl_key)
    return None


# ----------------------------------------------------------------------
# HTTP → HTTPS redirect listener
# ----------------------------------------------------------------------

# Enough drain to let well-formed clients finish sending before the
# redirect is read; beyond this the connection is simply closed.
_REDIRECT_DRAIN_CAP = 1 << 20  # 1 MiB


def _host_without_port(host: str) -> str:
    """The Host header's hostname (port stripped), IPv6-aware
    (``[::1]:8080`` → ``[::1]``, ``example.com:80`` → ``example.com``)."""
    if host.startswith("["):  # IPv6 literal
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def build_redirect_app(https_port: int):
    """A minimal ASGI app answering every request on plain HTTP with a
    ``308 Permanent Redirect`` to the same path + query on HTTPS.

    308 (not 301/302) so POST bodies are preserved when clients follow
    it. The target host comes from the request's own Host header, so
    the same listener works for every domain the gateway serves; the
    HTTPS port is appended unless it is 443.
    """
    async def redirect_app(scope, receive, send):
        if scope["type"] != "http":  # pragma: no cover — lifespan is off
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")
        # Drain the request body (bounded) so the client can finish
        # sending before the redirect is read.
        drained = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            drained += len(message.get("body", b""))
            if drained > _REDIRECT_DRAIN_CAP or not message.get("more_body"):
                break

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        host = headers.get("host") or scope.get("server", ("", 0))[0] or "localhost"
        target = _host_without_port(host)
        if https_port != 443:
            target = f"{target}:{https_port}"
        location = f"https://{target}{scope.get('path', '/')}"
        if scope.get("query_string"):
            location += "?" + scope["query_string"].decode("latin-1")

        body = b"Redirecting to HTTPS\n"
        await send(
            {
                "type": "http.response.start",
                "status": 308,
                "headers": [
                    (b"location", location.encode("latin-1")),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return redirect_app


def parse_redirect_port(
    args: argparse.Namespace, ssl_pair: tuple[Path, Path] | None
) -> int | None:
    """The redirect port, or None when not requested. Requires HTTPS —
    a redirect to plain HTTP would loop the client back to itself."""
    if getattr(args, "redirect_http", None) is None:
        return None
    if ssl_pair is None:
        raise TlsConfigError(
            "--redirect-http only makes sense with HTTPS on — pick a "
            "certificate mode (--self-signed / --letsencrypt / "
            "--ssl-cert/--ssl-key)"
        )
    return int(args.redirect_http)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gateway",
        description=(
            "Run the LLM Gateway over HTTP or HTTPS. Certificate modes: "
            "--self-signed (dev), --letsencrypt DOMAIN, or any PEM pair "
            "via --ssl-cert/--ssl-key (private CA)."
        ),
    )
    p.add_argument(
        "--host", default=None, help="bind address (default: GATEWAY_HOST or 127.0.0.1)"
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (default: GATEWAY_PORT or 8000)",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on code changes (development)",
    )
    g = p.add_argument_group("HTTPS (omit for plain HTTP)")
    mode = g.add_mutually_exclusive_group()
    mode.add_argument(
        "--self-signed",
        nargs="?",
        const=str(DEFAULT_CERT_DIR),
        default=None,
        metavar="DIR",
        help=(
            "serve HTTPS with a self-signed development certificate, "
            "generated under DIR if missing (default: %(const)s; gitignore "
            "the directory). Reused on subsequent runs."
        ),
    )
    mode.add_argument(
        "--letsencrypt",
        metavar="DOMAIN",
        help=(
            "serve HTTPS with certbot's certificate for DOMAIN "
            f"({LETSENCRYPT_BASE}/DOMAIN/fullchain.pem + privkey.pem). "
            "Restart after certbot renews."
        ),
    )
    g.add_argument(
        "--ssl-cert",
        metavar="FILE",
        help="PEM certificate chain — private/organizational CA, or Let's "
        "Encrypt files directly (env: GATEWAY_SSL_CERT)",
    )
    g.add_argument(
        "--ssl-key",
        metavar="FILE",
        help="PEM private key matching --ssl-cert (env: GATEWAY_SSL_KEY)",
    )
    g.add_argument(
        "--redirect-http",
        nargs="?",
        const=80,
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "also listen on plain HTTP (default port: %(const)s — may need "
            "sudo / CAP_NET_BIND_SERVICE) and answer every request with a "
            "308 redirect to the same URL on HTTPS. Requires a certificate "
            "mode above."
        ),
    )
    return p


def _log_cert_expiry(cert: Path) -> None:
    """Best-effort: log the certificate's expiry so renewals aren't a
    surprise (unparseable or missing 'cryptography' just skips this)."""
    try:
        from cryptography import x509

        not_after = x509.load_pem_x509_certificate(cert.read_bytes()).not_valid_after_utc
        logger.info(
            "certificate expires %s (%d days)",
            not_after.date(),
            (not_after - datetime.datetime.now(datetime.timezone.utc)).days,
        )
    except Exception:  # noqa: BLE001 — diagnostics only
        pass


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()

    try:
        ssl_pair = resolve_ssl(args, settings)
        redirect_port = parse_redirect_port(args, ssl_pair)
    except TlsConfigError as exc:
        parser.error(str(exc))
        return  # pragma: no cover — parser.error exits

    host = args.host or settings.host
    port = args.port or settings.port

    import uvicorn

    # Importing the app module now (not inside uvicorn) means the
    # gateway's own logging is configured before the banner below —
    # and config/credential errors surface with a clear traceback
    # instead of from inside uvicorn's import phase.
    import gateway.main  # noqa: F401 — builds and configures the app

    # The plain-HTTP redirect listener: a second, tiny ASGI app on its
    # own port, run in a daemon thread alongside the main server
    # (uvicorn skips signal handling off the main thread; we stop it
    # when the main server exits).
    redirect_server: uvicorn.Server | None = None
    redirect_thread: threading.Thread | None = None
    if redirect_port is not None:
        redirect_server = uvicorn.Server(
            uvicorn.Config(
                build_redirect_app(port),
                host=host,
                port=redirect_port,
                log_level="warning",
                lifespan="off",  # raw ASGI app with no lifespan of its own
            )
        )
        redirect_thread = threading.Thread(
            target=redirect_server.run,
            name="gateway-http-redirect",
            daemon=True,
        )
        redirect_thread.start()
        logger.info(
            "redirecting HTTP on http://%s:%d → https://%s:%d",
            host,
            redirect_port,
            host,
            port,
        )

    try:
        if ssl_pair is not None:
            cert, key = ssl_pair
            logger.info(
                "serving HTTPS on https://%s:%d (certificate: %s)", host, port, cert
            )
            _log_cert_expiry(cert)
            uvicorn.run(
                "gateway.main:app",
                host=host,
                port=port,
                reload=args.reload,
                log_level=settings.log_level.lower(),
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
            )
        else:
            logger.info(
                "serving HTTP on http://%s:%d (no certificate configured; "
                "see --self-signed / --letsencrypt / --ssl-cert)",
                host,
                port,
            )
            uvicorn.run(
                "gateway.main:app",
                host=host,
                port=port,
                reload=args.reload,
                log_level=settings.log_level.lower(),
            )
    finally:
        if redirect_server is not None:
            redirect_server.should_exit = True
            assert redirect_thread is not None
            redirect_thread.join(timeout=5.0)


if __name__ == "__main__":  # pragma: no cover
    main()
