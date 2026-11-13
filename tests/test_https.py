"""HTTPS serving: certificate generation, TLS flag resolution, and a
real TLS round trip — uvicorn serving the full app, an httpx client
that actually verifies the certificate (not ``verify=False``).

Covers the three certificate modes of ``python -m gateway`` /
``scripts/gateway.sh`` (see ``gateway/serve.py``): self-signed dev
pair, private PEM pair (``--ssl-cert``/``--ssl-key``, also
``GATEWAY_SSL_CERT``/``GATEWAY_SSL_KEY``) and Let's Encrypt
(``--letsencrypt DOMAIN`` → certbot's live layout).
"""

from __future__ import annotations

import ssl
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from gateway.config import Settings
from gateway.db import reset_engine_for_tests
from gateway.main import create_app
from gateway.serve import (
    TlsConfigError,
    build_parser,
    ensure_self_signed,
    generate_self_signed_cert,
    letsencrypt_paths,
    resolve_ssl,
)

from .conftest import ADMIN_KEY, MockUpstream, auth, chat_body, create_key, make_config


def _args(argv: list[str]) -> "object":
    """Parsed CLI namespace via the real parser (tests the surface)."""
    return build_parser().parse_args(argv)


# ----------------------------------------------------------------------
# Self-signed certificate generation
# ----------------------------------------------------------------------


def test_generate_self_signed_cert_creates_valid_pair(tmp_path):
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert_path, key_path, common_name="localhost", days=30)

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    # The pair matches (the certificate vouches for exactly this key).
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    # SANs cover local names, so clients can verify the connection.
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert x509.DNSName("localhost") in sans
    assert x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")) in sans
    # Validity window covers now (with skew margin) and the requested span.
    now = datetime.now(timezone.utc)
    assert cert.not_valid_before_utc <= now
    assert timedelta(days=29) < cert.not_valid_after_utc - now <= timedelta(days=30)
    # The private key is the secret: readable by owner only.
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_ensure_self_signed_generates_only_when_missing(tmp_path):
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    assert ensure_self_signed(cert_path, key_path) is True  # generated
    before = cert_path.read_bytes(), key_path.read_bytes()
    assert ensure_self_signed(cert_path, key_path) is False  # reused, untouched
    assert (cert_path.read_bytes(), key_path.read_bytes()) == before


def test_ensure_self_signed_regenerates_half_pair(tmp_path):
    """A lone surviving file of the pair self-heals instead of serving
    a certificate that matches no key."""
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    ensure_self_signed(cert_path, key_path)
    key_path.unlink()
    assert ensure_self_signed(cert_path, key_path) is True
    assert cert_path.is_file() and key_path.is_file()


# ----------------------------------------------------------------------
# Let's Encrypt path resolution
# ----------------------------------------------------------------------


def test_letsencrypt_paths_resolves_certbot_layout(tmp_path):
    (tmp_path / "api.example.com").mkdir()
    cert = tmp_path / "api.example.com" / "fullchain.pem"
    key = tmp_path / "api.example.com" / "privkey.pem"
    cert.write_text("cert")
    key.write_text("key")
    assert letsencrypt_paths("api.example.com", base=tmp_path) == (cert, key)


def test_letsencrypt_paths_missing_certificate(tmp_path):
    with pytest.raises(TlsConfigError) as excinfo:
        letsencrypt_paths("missing.example.com", base=tmp_path)
    assert "certbot" in str(excinfo.value)


# ----------------------------------------------------------------------
# SSL resolution: flags, env fallback, precedence, validation
# ----------------------------------------------------------------------


def test_resolve_ssl_plain_http_when_unconfigured():
    assert resolve_ssl(_args([]), Settings()) is None


def test_resolve_ssl_requires_cert_and_key_together(tmp_path):
    cert = tmp_path / "cert.pem"
    generate_self_signed_cert(cert, tmp_path / "key.pem")
    with pytest.raises(TlsConfigError, match="together"):
        resolve_ssl(_args(["--ssl-cert", str(cert)]), Settings())


def test_resolve_ssl_missing_file(tmp_path):
    with pytest.raises(TlsConfigError, match="not found"):
        resolve_ssl(
            _args(["--ssl-cert", str(tmp_path / "a.pem"), "--ssl-key", str(tmp_path / "b.pem")]),
            Settings(),
        )


def test_resolve_ssl_env_fallback(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)
    resolved = resolve_ssl(_args([]), Settings(ssl_cert=str(cert), ssl_key=str(key)))
    assert resolved == (cert, key)


def test_resolve_ssl_flag_beats_env(tmp_path):
    env_cert, env_key = tmp_path / "env.pem", tmp_path / "env-key.pem"
    flag_cert, flag_key = tmp_path / "flag.pem", tmp_path / "flag-key.pem"
    generate_self_signed_cert(env_cert, env_key)
    generate_self_signed_cert(flag_cert, flag_key)
    resolved = resolve_ssl(
        _args(["--ssl-cert", str(flag_cert), "--ssl-key", str(flag_key)]),
        Settings(ssl_cert=str(env_cert), ssl_key=str(env_key)),
    )
    assert resolved == (flag_cert, flag_key)


def test_resolve_ssl_self_signed_dir(tmp_path):
    resolved = resolve_ssl(_args(["--self-signed", str(tmp_path)]), Settings())
    assert resolved == (tmp_path / "gateway-dev.pem", tmp_path / "gateway-dev-key.pem")
    assert (tmp_path / "gateway-dev-key.pem").stat().st_mode & 0o777 == 0o600


def test_resolve_ssl_conflicting_modes(tmp_path):
    with pytest.raises(TlsConfigError, match="one certificate mode"):
        resolve_ssl(
            _args(["--self-signed", str(tmp_path), "--ssl-cert", "x.pem"]), Settings()
        )


def test_flag_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--self-signed", "--letsencrypt", "x.example.com"])


# ----------------------------------------------------------------------
# The real thing: uvicorn serving the gateway over verified TLS
# ----------------------------------------------------------------------


async def test_gateway_serves_https_end_to_end(tmp_path):
    """A full round trip over TLS: uvicorn binds HTTPS with the
    generated pair, the client verifies the certificate (real trust
    chain, not ``verify=False``), and a chat request runs the whole
    stack — auth, pipeline, mock provider, log row — over the wire."""
    reset_engine_for_tests()
    db_url = f"sqlite+aiosqlite:///{tmp_path}/tls.db"
    mock = MockUpstream()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    app = create_app(
        settings=Settings(admin_key=ADMIN_KEY, database_url=db_url),
        config=make_config(),
        http_client=http_client,
    )
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,  # OS-assigned: no port-collision flakes
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):  # <= 10s for startup (lifespan incl. DB setup)
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]

        # The client trusts the self-signed pair as its own CA — exactly
        # what an operator distributes to internal SDK clients.
        ctx = ssl.create_default_context(cafile=str(cert))
        async with httpx.AsyncClient(
            verify=ctx, base_url=f"https://127.0.0.1:{port}", timeout=10.0
        ) as client:
            assert (await client.get("/readyz")).status_code == 200

            virtual_key = (await create_key(client, "tls-e2e"))["key"]
            resp = await client.post(
                "/v1/chat/completions",
                json=chat_body(content="hello over tls"),
                headers=auth(virtual_key),
            )
            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"]

            # And plain HTTP against the TLS port is refused (the
            # endpoint really is HTTPS, not just reachable via it).
            # The server drops the plaintext handshake; httpx surfaces
            # that as either error depending on the socket state.
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as plain:
                with pytest.raises((httpx.ConnectError, httpx.RemoteProtocolError)):
                    await plain.get("/readyz")
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        reset_engine_for_tests()
