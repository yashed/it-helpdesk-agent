"""AgentID credential handling for the IT helpdesk agent.

Reads the AMP_AGENTID_* environment variables the platform injects
automatically into internal (platform-hosted) agents' pods, mints an OAuth2
client_credentials token from the agent's own per-environment Thunder
instance, and caches/refreshes it.

This module exists purely to make credential injection OBSERVABLE at runtime
(structured logs + the /identity endpoint in app.py) for manually verifying
the AgentID "Gateway Binding" feature end to end: that the right credentials
land in the pod, that they differ per environment, that promotion doesn't
leak one environment's credentials into another, and that rotation/revoke
propagate. This agent does not yet call anything that validates AgentID
scopes — there is nothing on the other end enforcing them yet.

If the platform hasn't injected these vars (external agent, or running
locally outside the platform), identity features are disabled and clearly
reported as such via status() — this must NEVER crash agent startup.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("it-helpdesk.identity")

CLIENT_ID_ENV = "AMP_AGENTID_CLIENT_ID"
CLIENT_SECRET_ENV = "AMP_AGENTID_CLIENT_SECRET"  # noqa: S105 — env var NAME, not a secret value
TOKEN_ENDPOINT_ENV = "AMP_AGENTID_TOKEN_ENDPOINT"
SCOPES_ENV = "AMP_AGENTID_SCOPES"
TEST_MCP_URL_ENV = "TEST_MCP_URL"

# Refresh a bit before actual expiry so a slow refresh never leaves a gap.
REFRESH_AT_FRACTION = 0.75
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
MINT_TIMEOUT_SECONDS = 10


@dataclass
class _TokenState:
    access_token: str | None = None
    minted_at: float = 0.0
    expires_at: float = 0.0
    last_error: str | None = None
    mint_count: int = 0


class AgentIdentity:
    """Reads injected AgentID env vars and mints/caches/refreshes a token.

    Never raises out of __init__ — a missing/partial injection just disables
    the feature (self.available = False) so agent startup is never blocked
    by an identity misconfiguration (e.g. this same code running as an
    external agent, or on a developer's laptop).
    """

    def __init__(self) -> None:
        self.client_id = os.environ.get(CLIENT_ID_ENV, "")
        self.client_secret = os.environ.get(CLIENT_SECRET_ENV, "")
        self.token_endpoint = os.environ.get(TOKEN_ENDPOINT_ENV, "")
        self.scopes = os.environ.get(SCOPES_ENV, "")
        self.test_mcp_url = os.environ.get(TEST_MCP_URL_ENV, "")

        self.available = bool(self.client_id and self.client_secret and self.token_endpoint)
        self._lock = threading.Lock()
        self._state = _TokenState()

        # Deliberately logged at INFO, unconditionally, once at startup: this
        # is the single line that answers "did injection work?" when reading
        # `kubectl logs` right after a deploy, promote, or rotation. Never
        # logs client_secret.
        if self.available:
            log.info(
                "AGENTID_STATUS available=true client_id=%s token_endpoint=%s scopes=%r test_mcp_url=%s",
                self.client_id, self.token_endpoint, self.scopes, self.test_mcp_url,
            )
        else:
            missing = [
                name
                for name, val in (
                    (CLIENT_ID_ENV, self.client_id),
                    (CLIENT_SECRET_ENV, self.client_secret),
                    (TOKEN_ENDPOINT_ENV, self.token_endpoint),
                )
                if not val
            ]
            log.warning(
                "AGENTID_STATUS available=false missing_env_vars=%s test_mcp_url=%s "
                "(expected for external agents or local runs outside the platform)",
                missing, self.test_mcp_url,
            )

    def _mint_locked(self) -> None:
        """Mints a fresh token via client_credentials. Caller must hold self._lock."""
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": self.scopes}).encode()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(  # noqa: S310 — token_endpoint is platform-injected, not user input
            self.token_endpoint,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=MINT_TIMEOUT_SECONDS) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            self._state.last_error = f"HTTP {exc.code}: {detail[:300]}"
            log.error("AGENTID_TOKEN_MINT_FAILED client_id=%s error=%s", self.client_id, self._state.last_error)
            raise
        except Exception as exc:  # noqa: BLE001
            self._state.last_error = str(exc)
            log.error("AGENTID_TOKEN_MINT_FAILED client_id=%s error=%s", self.client_id, exc)
            raise

        access_token = payload.get("access_token")
        if not access_token:
            self._state.last_error = f"token endpoint response had no access_token: {payload!r}"
            log.error("AGENTID_TOKEN_MINT_FAILED client_id=%s error=%s", self.client_id, self._state.last_error)
            raise RuntimeError(self._state.last_error)

        lifetime = payload.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)
        now = time.time()
        self._state.access_token = access_token
        self._state.minted_at = now
        self._state.expires_at = now + lifetime * REFRESH_AT_FRACTION
        self._state.last_error = None
        self._state.mint_count += 1

        granted_scope = payload.get("scope", "")
        log.info(
            "AGENTID_TOKEN_MINTED client_id=%s requested_scope=%r granted_scope=%r "
            "expires_in=%s mint_count=%d token=%s",
            self.client_id, self.scopes, granted_scope, lifetime, self._state.mint_count, self._state.access_token,
        )
        if granted_scope != self.scopes:
            log.warning(
                "AGENTID_SCOPE_MISMATCH requested=%r granted=%r — Thunder filters requested "
                "scopes down to what this agent's role assignments actually grant in Thunder; "
                "this is expected until the agent is assigned a role covering the requested scope",
                self.scopes, granted_scope,
            )

    def get_token(self, force_refresh: bool = False) -> str:
        """Returns a valid access token, minting or refreshing as needed.

        Raises RuntimeError if identity is unavailable, or the mint call
        fails — never silently returns a stale or empty token.
        """
        if not self.available:
            raise RuntimeError("AgentID identity not available in this environment")
        with self._lock:
            if force_refresh or self._state.access_token is None or time.time() > self._state.expires_at:
                self._mint_locked()
            return self._state.access_token

    def status(self) -> dict:
        """Non-secret status snapshot for the /identity endpoint.

        Never includes the client_secret or the access token itself —
        client_id is safe (Thunder's own model treats it as public, like a
        username), everything else here is metadata about token state.
        """
        with self._lock:
            if not self.available:
                return {
                    "available": False,
                    "reason": "AgentID env vars not injected (external agent, or a local run outside the platform)",
                    "test_mcp_url": self.test_mcp_url,
                }
            if self._state.last_error:
                token_status = "error"
            elif self._state.access_token is None:
                token_status = "never-minted"
            elif time.time() <= self._state.expires_at:
                token_status = "valid"
            else:
                token_status = "stale-in-cache"
            return {
                "available": True,
                "client_id": self.client_id,
                "token_endpoint": self.token_endpoint,
                "scopes_requested": self.scopes,
                "test_mcp_url": self.test_mcp_url,
                "token_status": token_status,
                "mint_count": self._state.mint_count,
                "minted_at": self._state.minted_at or None,
                "refresh_due_at": self._state.expires_at or None,
                "last_error": self._state.last_error,
            }


# Module-level singleton — mirrors how config.py/agent.py are wired into
# app.py (one instance built at import time, read env once at startup).
AGENT_IDENTITY = AgentIdentity()
