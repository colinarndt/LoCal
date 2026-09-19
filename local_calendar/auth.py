"""Request identity for local installs and Cloudflare Access hosting.

Local/macOS installs remain zero-configuration. Hosted deployments opt in with
``LOCAL_CALENDAR_AUTH_MODE=cloudflare`` and validate the Access assertion at
the origin rather than trusting that every request arrived through the tunnel.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit


AUTH_MODE_ENV = "LOCAL_CALENDAR_AUTH_MODE"
TEAM_DOMAIN_ENV = "CLOUDFLARE_ACCESS_TEAM_DOMAIN"
AUDIENCE_ENV = "CLOUDFLARE_ACCESS_AUD"
LOCAL_EMAIL_ENV = "LOCAL_CALENDAR_LOCAL_EMAIL"
ACCESS_HEADER = "Cf-Access-Jwt-Assertion"


class AuthenticationError(Exception):
    """The request has no valid end-user identity."""


class AuthenticationConfigurationError(Exception):
    """Hosted authentication was enabled without safe configuration."""


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str
    mode: str


def _team_domain(value: str | None) -> str:
    domain = (value or "").strip().rstrip("/")
    parsed = urlsplit(domain)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise AuthenticationConfigurationError(
            f"{TEAM_DOMAIN_ENV} must be an https origin such as "
            "https://your-team.cloudflareaccess.com"
        )
    return domain


@lru_cache(maxsize=4)
def _jwk_client(team_domain: str):
    # Imported only in hosted mode so the existing macOS application can start
    # even before its environment has been updated with the new dependency.
    from jwt import PyJWKClient

    return PyJWKClient(f"{team_domain}/cdn-cgi/access/certs", cache_keys=True)


def _decode_access_token(token: str, team_domain: str, audience: str) -> dict:
    import jwt

    key = _jwk_client(team_domain).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=team_domain,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )


def authenticate(headers, environ: dict[str, str] | None = None) -> Identity:
    """Return the current identity or raise a fail-closed auth error."""
    env = environ if environ is not None else os.environ
    mode = (env.get(AUTH_MODE_ENV) or "local").strip().lower()

    if mode == "local":
        email = (env.get(LOCAL_EMAIL_ENV) or "local@localhost").strip().lower()
        return Identity(subject=email, email=email, mode=mode)

    if mode != "cloudflare":
        raise AuthenticationConfigurationError(
            f"{AUTH_MODE_ENV} must be 'local' or 'cloudflare'"
        )

    team_domain = _team_domain(env.get(TEAM_DOMAIN_ENV))
    audience = (env.get(AUDIENCE_ENV) or "").strip()
    if not audience:
        raise AuthenticationConfigurationError(f"{AUDIENCE_ENV} is required")

    token = (headers.get(ACCESS_HEADER) or "").strip()
    if not token or len(token) > 16_384:
        raise AuthenticationError("missing Cloudflare Access assertion")

    try:
        claims = _decode_access_token(token, team_domain, audience)
    except Exception as exc:
        # Do not echo JWT library details to the client. They can contain claim
        # values, key IDs, or fetch errors that are useful only in server logs.
        raise AuthenticationError("invalid Cloudflare Access assertion") from exc

    email = str(claims.get("email") or "").strip().lower()
    subject = str(claims.get("sub") or "").strip()
    if not email or not subject:
        # Access service tokens intentionally have no email identity and cannot
        # own a private LoCal calendar.
        raise AuthenticationError("Cloudflare Access identity has no user email")
    return Identity(subject=subject, email=email, mode=mode)
