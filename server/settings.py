"""Server configuration, read from the environment once at import.

Two rules this module exists to enforce:

* **Nothing here is ever sent to a client.** The one function that produces
  client-visible configuration is `public_config()`, and it is a hand-written
  allowlist rather than a filter over the settings object. A field added to
  `Settings` is private until somebody deliberately adds it there, which is the
  right way round -- the opposite arrangement leaks a secret the first time
  someone names a field something the filter did not anticipate.

* **A missing secret is a startup failure, not a runtime surprise.** The app
  refuses to boot without the values it needs rather than serving traffic and
  falling over on whichever request first touches the gap. `validate()` runs
  from the application factory.

The desktop client never sees the service-role key, the JWT secret, the token
encryption key, or the Google client secret. It only ever holds a user access
token minted by Supabase, which is scoped to that one user and expires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse

from dotenv import load_dotenv

# Loaded from the process environment in production (systemd, Fly, Render,
# whatever) and from a .env in a checkout. `override=False` so a real
# environment variable always beats a stale file.
load_dotenv(override=False)


class ConfigError(RuntimeError):
    """The server is not configured well enough to start safely."""


def _text(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number (got {raw!r})") from exc


def _csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    # --- environment ---------------------------------------------------------
    environment: str = field(default_factory=lambda: _text("LUMEN_ENV", "development"))

    # --- Supabase ------------------------------------------------------------
    # The project URL, e.g. https://abcdefgh.supabase.co
    supabase_url: str = field(default_factory=lambda: _text("SUPABASE_URL").rstrip("/"))
    # The anon key. Safe to hold server-side; we do not ship it to the desktop
    # client either, because the client talks only to this API and never to
    # GoTrue directly -- one door is easier to rate limit and to audit.
    supabase_anon_key: str = field(default_factory=lambda: _text("SUPABASE_ANON_KEY"))
    # Full-privilege key. Bypasses RLS. Used only by server/db.py's
    # `service_connection` and the small admin surface in server/services/.
    supabase_service_key: str = field(
        default_factory=lambda: _text("SUPABASE_SERVICE_ROLE_KEY")
    )
    # Legacy HS256 signing secret. Optional: projects issuing asymmetric keys
    # verify through JWKS instead and can leave this unset.
    supabase_jwt_secret: str = field(default_factory=lambda: _text("SUPABASE_JWT_SECRET"))

    # --- database ------------------------------------------------------------
    # Direct Postgres connection. Must be a pooler URL in production.
    database_url: str = field(default_factory=lambda: _text("DATABASE_URL"))
    db_pool_min: int = field(default_factory=lambda: _int("DATABASE_POOL_MIN", 2))
    db_pool_max: int = field(default_factory=lambda: _int("DATABASE_POOL_MAX", 10))
    db_statement_timeout_ms: int = field(
        default_factory=lambda: _int("DATABASE_STATEMENT_TIMEOUT_MS", 15_000)
    )

    # --- token encryption ----------------------------------------------------
    # base64 of 32 random bytes. Wraps OAuth tokens before they touch Postgres.
    # Rotating means adding a new version and leaving the old one readable --
    # see LUMEN_TOKEN_KEYS_PREVIOUS.
    token_key: str = field(default_factory=lambda: _text("LUMEN_TOKEN_ENCRYPTION_KEY"))
    token_key_version: int = field(
        default_factory=lambda: _int("LUMEN_TOKEN_KEY_VERSION", 1)
    )
    # "2:base64key,1:base64key" -- older versions kept only for decryption.
    token_keys_previous: str = field(
        default_factory=lambda: _text("LUMEN_TOKEN_KEYS_PREVIOUS")
    )

    # --- Google OAuth (integrations, not sign-in) ----------------------------
    google_client_id: str = field(default_factory=lambda: _text("GOOGLE_OAUTH_CLIENT_ID"))
    google_client_secret: str = field(
        default_factory=lambda: _text("GOOGLE_OAUTH_CLIENT_SECRET")
    )

    # --- HTTP ----------------------------------------------------------------
    # Where this API is reachable. Used to build OAuth redirect URIs, which must
    # match what is registered at Google exactly.
    public_url: str = field(
        default_factory=lambda: _text("LUMEN_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    )
    # Where the app front end lives, for post-auth redirects and CORS.
    app_url: str = field(
        default_factory=lambda: _text("LUMEN_APP_URL", "http://127.0.0.1:8765").rstrip("/")
    )
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _csv("LUMEN_CORS_ORIGINS"))
    # Cookie domain for the refresh cookie. Empty means host-only, which is the
    # safer default and correct for a single-domain deployment.
    cookie_domain: str = field(default_factory=lambda: _text("LUMEN_COOKIE_DOMAIN"))

    # --- rate limits ---------------------------------------------------------
    # Per-window caps. Windows are in seconds. These are the defaults; each is
    # overridable so an operator can tighten them without a deploy.
    login_max_per_ip: int = field(default_factory=lambda: _int("LUMEN_RL_LOGIN_IP", 30))
    login_max_per_account: int = field(
        default_factory=lambda: _int("LUMEN_RL_LOGIN_ACCOUNT", 8)
    )
    signup_max_per_ip: int = field(default_factory=lambda: _int("LUMEN_RL_SIGNUP_IP", 10))
    reset_max_per_account: int = field(default_factory=lambda: _int("LUMEN_RL_RESET", 5))
    resend_max_per_account: int = field(default_factory=lambda: _int("LUMEN_RL_RESEND", 5))
    rate_window_seconds: int = field(default_factory=lambda: _int("LUMEN_RL_WINDOW", 900))

    # --- behaviour -----------------------------------------------------------
    # Refuse anything but HTTPS at the edge. Off in development so localhost
    # works; `validate()` insists on it in production.
    require_https: bool = field(default_factory=lambda: _bool("LUMEN_REQUIRE_HTTPS", False))
    # Block unverified accounts from the sensitive surface (integrations, agent
    # runs). Verification itself is always on; this decides how much it gates.
    require_verified_email: bool = field(
        default_factory=lambda: _bool("LUMEN_REQUIRE_VERIFIED_EMAIL", True)
    )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    @property
    def gotrue_url(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.public_url}/api/integrations/google/callback"

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Origins the browser client may call from.

        The app's own URL is always included, so a correct deployment needs no
        CORS configuration at all. Never `*`: these endpoints are credentialed.
        """
        origins = {self.app_url, *self.cors_origins}
        return tuple(sorted(o for o in origins if o))

    # ------------------------------------------------------------------ checks

    def validate(self) -> None:
        """Refuse to start rather than start insecurely."""
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_ANON_KEY", self.supabase_anon_key),
                ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_key),
                ("DATABASE_URL", self.database_url),
                ("LUMEN_TOKEN_ENCRYPTION_KEY", self.token_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". See .env.example for what each one is."
            )

        if not self.supabase_url.startswith("https://") and self.is_production:
            raise ConfigError("SUPABASE_URL must be https:// in production.")

        parsed = urlparse(self.public_url)
        if not parsed.scheme or not parsed.netloc:
            raise ConfigError(f"LUMEN_PUBLIC_URL is not a URL: {self.public_url!r}")

        if self.is_production:
            if not self.require_https:
                raise ConfigError(
                    "LUMEN_REQUIRE_HTTPS must be on in production. Cookies are "
                    "issued Secure and the refresh token must not cross plain HTTP."
                )
            if parsed.scheme != "https":
                raise ConfigError("LUMEN_PUBLIC_URL must be https:// in production.")
            if not self.app_url.startswith("https://"):
                raise ConfigError("LUMEN_APP_URL must be https:// in production.")

        if self.db_pool_min > self.db_pool_max:
            raise ConfigError("DATABASE_POOL_MIN cannot exceed DATABASE_POOL_MAX.")

        # Fails fast and loudly if the key is the wrong length or not base64,
        # rather than at the first integration connect.
        from .security import crypto

        crypto.keyring()  # raises ConfigError on a malformed key

    # ------------------------------------------------------------------ public

    def public_config(self) -> dict:
        """The only settings a client is ever handed.

        An allowlist, written out by hand. Nothing is derived from the dataclass
        fields, so adding a secret to this class cannot accidentally publish it.
        """
        return {
            "environment": self.environment,
            "require_verified_email": self.require_verified_email,
            # Which sign-in buttons to draw. Presence of a client id, not the
            # id itself -- the client posts to this API and never to Google.
            "providers": {
                "password": True,
                "google": bool(self.google_client_id),
                # Apple needs its own key material and an Apple Developer
                # account; see SETUP-AUTH.md. Off until that exists.
                "apple": bool(_text("APPLE_OAUTH_CLIENT_ID")),
            },
            "password_policy": {
                "min_length": 12,
                "requires": ["a lowercase letter", "an uppercase letter", "a number"],
            },
        }


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
