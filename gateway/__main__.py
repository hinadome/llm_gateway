"""``python -m gateway`` — run the gateway (HTTP or HTTPS).

See ``gateway/serve.py`` for the certificate modes (self-signed,
private PEM pair, Let's Encrypt) and ``scripts/gateway.sh`` for the
convenience wrapper.
"""

from __future__ import annotations

from .serve import main

if __name__ == "__main__":
    main()
