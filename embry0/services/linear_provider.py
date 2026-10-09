"""Build a ``LinearSyncService`` from whatever credentials are current (#157).

``app.state.linear_sync`` used to be constructed once during startup from
``config.linear_api_key``, and left as ``None`` when that env var was empty.
Now that Linear credentials are editable in Settings → Integrations, that is
not good enough: an operator who configures Linear in the dashboard would keep
getting "linear integration not configured" until someone restarted the
orchestrator.

This provider resolves credentials per delivery (DB row over env) and rebuilds
the service only when they actually change, keyed on a fingerprint of the
inputs. Rebuilding is cheap — the service holds an API key, a parsed repo map,
and an optional HTTP client — but doing it unconditionally on every webhook
would throw away the client each time.
"""

from typing import Any

import structlog

from embry0.services.linear_sync import LinearSyncService, parse_repo_map

logger = structlog.get_logger(__name__)


class LinearSyncProvider:
    """Resolves the current ``LinearSyncService`` and webhook secret."""

    def __init__(self, integration_repo: Any) -> None:
        self._repo = integration_repo
        self._fingerprint: tuple[str, str] | None = None
        self._service: LinearSyncService | None = None

    async def resolve(self) -> tuple[LinearSyncService | None, str]:
        """Return ``(service_or_None, webhook_secret)`` for this delivery.

        The service is ``None`` when no API key is configured on either
        surface — the caller reports the integration as unconfigured. The
        secret is returned regardless, because signature verification has to
        happen before we know whether the integration is usable.
        """
        api_key, webhook_secret, repo_map_raw = await self._repo.get_linear_credentials()
        if not api_key:
            self._fingerprint = None
            self._service = None
            return None, webhook_secret

        fingerprint = (api_key, repo_map_raw)
        if self._service is not None and self._fingerprint == fingerprint:
            return self._service, webhook_secret

        self._service = LinearSyncService(api_key, parse_repo_map(repo_map_raw))
        self._fingerprint = fingerprint
        logger.info("linear_sync_rebuilt", has_repo_map=bool(repo_map_raw))
        return self._service, webhook_secret
