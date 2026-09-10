"""Google Analytics 4 MCP Server.

Provides GA4 reporting tools via the Google Analytics Data API v1.
Authentication: service account JSON (GOOGLE_APPLICATION_CREDENTIALS env var).
"""

import functools
import os
import json
import logging
import random
import re
import threading
import time
from typing import Any, Callable, List, Optional

# v1alpha, not v1beta: AccessBinding management (create_access_binding,
# list_access_bindings) is only exposed on the v1alpha admin client as of
# google-analytics-admin 0.30.1 -- v1beta has KeyEvent support but no
# AccessBinding type or methods at all. v1alpha also has KeyEvent, so one
# client covers both instead of splitting across two admin client versions.
from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient
from google.analytics.admin_v1alpha.types import AccessBinding, KeyEvent
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
    RunRealtimeReportRequest,
)
from google.api_core import exceptions as google_exceptions
from google.oauth2 import service_account

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both names are probed so this server runs under either major version; the
# API used below (tool decorator, run(transport=...)) is identical in both.
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer

from mcp.types import ToolAnnotations

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

_LOGGER = logging.getLogger("ga4_server")

mcp = MCPServer("ga4-server")

# Every tool here is a read against the Google Analytics Data API, so they all
# share one annotation vector. Declaring it explicitly matters: an MCP tool with
# no annotations inherits the spec defaults (readOnlyHint=False,
# destructiveHint=True, idempotentHint=False, openWorldHint=True), the
# least-safe combination, which would block auto-approval of a pure read.
_READ_REMOTE = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# Admin API writes below are non-destructive in the sense that they never
# delete or overwrite existing GA4 configuration: marking a key event only
# adds a flag (and is a documented no-op if already set -- see
# mark_key_event), and granting access only adds a new access binding. Both
# are idempotent for the same inputs, so this shares one annotation vector
# distinct from _READ_REMOTE only in readOnlyHint.
_WRITE_REMOTE_SAFE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

try:
    from rate_limiter import check_rate_limit
    _RATE_LIMITER_AVAILABLE = True
except ImportError:
    _RATE_LIMITER_AVAILABLE = False

    def check_rate_limit(client_id="default", bucket="tool_calls"):
        """Fallback used when ``rate_limiter`` cannot be imported.

        Always reports the call as allowed, so a missing vendored module
        fails open rather than crashing every tool call.

        Args:
            client_id: Identifier for the caller. Unused in the fallback.
            bucket: Name of the rate limit bucket. Unused in the fallback.

        Returns:
            dict with ``allowed`` always ``True``.
        """
        return {"allowed": True}

_RATE_LIMIT_UNAVAILABLE_WARNED = threading.Event()


def _rate_limit_verdict(bucket: str) -> dict:
    """Consume one token from ``bucket`` and report whether a call may run.

    Enforcement is opt-in through ``ENABLE_RATE_LIMITING``: with it unset,
    ``check_rate_limit`` returns allowed without creating any bucket state,
    so this costs one environment lookup and nothing else. If limiting is
    switched on but ``rate_limiter`` could not be imported, this warns once
    on stderr rather than failing open silently -- an operator who set the
    variable is entitled to know it is doing nothing.

    Args:
        bucket: Name of the token bucket to draw from.

    Returns:
        The limiter verdict dict, always containing ``allowed``.
    """
    if not _RATE_LIMITER_AVAILABLE:
        if (os.environ.get("ENABLE_RATE_LIMITING") == "1"
                and not _RATE_LIMIT_UNAVAILABLE_WARNED.is_set()):
            _RATE_LIMIT_UNAVAILABLE_WARNED.set()
            _LOGGER.warning(
                "rate_limiting_enabled_but_limiter_unavailable",
                extra={"detail": "ENABLE_RATE_LIMITING=1 has no effect; "
                                  "rate_limiter is not importable"},
            )
        return {"allowed": True}
    return check_rate_limit(bucket=bucket)


def rate_limited(bucket: str):
    """Decorator that gates a sync MCP tool behind the token-bucket limiter.

    Applied directly below ``@mcp.tool(...)`` so FastMCP's signature
    introspection (which follows ``__wrapped__`` via ``functools.wraps``)
    still sees the original tool's parameters for schema generation. A
    denied call returns a structured JSON string describing the denial
    without ever invoking the wrapped tool body, so a throttled call never
    reaches the Google Analytics Data API the bucket protects. Tools that
    should not draw from any budget -- thin wrappers that delegate their
    entire body to another already-gated tool -- simply omit this decorator
    rather than passing a bucket of ``None``.

    Args:
        bucket: Name of the token bucket this tool draws from.

    Returns:
        The decorated tool function.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> str:
            """Rate-limit gate wrapping the original sync tool function."""
            verdict = _rate_limit_verdict(bucket)
            if not verdict.get("allowed", True):
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Rate limit exceeded for bucket '{bucket}'. "
                        f"Retry in {verdict.get('retry_after')} seconds."
                    ),
                    "error_type": "RateLimitExceeded",
                    "bucket": bucket,
                    "retry_after": verdict.get("retry_after"),
                })
            return fn(*args, **kwargs)
        return wrapper
    return decorator


PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "service_account.json"),
)

# Domain -> property-ID mappings live in a local JSON file rather than only an
# env var, and that file is re-read on every call instead of once at process
# startup. An MCP host typically caches a server's configured env at the time
# it first launches the subprocess and does not re-inject edits into an
# already-running process, so an env-var-only mapping would need the whole
# host restarted to pick up a config change. Re-reading a small local file per
# call costs a stat + parse, negligible next to the network round trip every
# tool here makes to the GA4 Data API.
_PROPERTY_CONFIG_FILE = os.environ.get(
    "GA4_PROPERTY_CONFIG_FILE",
    os.path.join(os.path.dirname(__file__), "properties.local.json"),
)


def _normalize_domain(domain: str) -> str:
    """Lowercase a domain and strip a leading "www." for map lookups.

    Args:
        domain: Domain string as written by a caller or config file.

    Returns:
        Normalized domain suitable as a PROPERTY_MAP key.
    """
    d = domain.strip().lower()
    return d[4:] if d.startswith("www.") else d


def _parse_property_map_json(raw: str, source: str) -> dict:
    """Parse a JSON object of domain -> numeric property ID, normalizing keys.

    Shared between the env var and config file loaders so both apply the same
    validation and normalization. A malformed source yields an empty map
    rather than raising, since the numeric-ID path (property_id passed
    directly, or the default) must keep working even when no map is usable.

    Args:
        raw: JSON object text, e.g. '{"example.com": "123456789"}'.
        source: Human-readable origin of ``raw``, used in warning logs.

    Returns:
        Dict from normalized domain to numeric property ID string.
    """
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning(
            "ga4_property_map_invalid_json",
            extra={"detail": f"{source} is not valid JSON; ignoring it"},
        )
        return {}
    if not isinstance(parsed, dict):
        _LOGGER.warning(
            "ga4_property_map_not_object",
            extra={"detail": f"{source} must be a JSON object; ignoring it"},
        )
        return {}
    return {_normalize_domain(k): str(v).strip() for k, v in parsed.items()}


def _load_property_config() -> tuple:
    """Load the current domain -> property-ID map and default property ID.

    Reads fresh on every call, checked in this order:

    1. ``GA4_PROPERTY_CONFIG_FILE`` (default: ``properties.local.json`` next
       to this script) -- a JSON object with optional ``"default"`` (numeric
       property ID) and ``"properties"`` (domain -> numeric property ID)
       keys. Meant to hold real per-deployment values and kept out of version
       control (see .gitignore) since it is never meant to be committed.
    2. ``GA4_PROPERTY_MAP`` / ``GA4_PROPERTY_ID`` env vars, used only for
       whatever the file did not supply -- this keeps existing env-var-only
       deployments working unchanged.

    Returns:
        Tuple of (property_map: dict, default_property_id: str).
    """
    property_map: dict = {}
    default_id = PROPERTY_ID

    if os.path.exists(_PROPERTY_CONFIG_FILE):
        try:
            with open(_PROPERTY_CONFIG_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            if isinstance(file_data, dict):
                file_properties = file_data.get("properties", {})
                if isinstance(file_properties, dict):
                    property_map.update(
                        {
                            _normalize_domain(k): str(v).strip()
                            for k, v in file_properties.items()
                        }
                    )
                file_default = file_data.get("default")
                if file_default:
                    default_id = str(file_default).strip()
            else:
                _LOGGER.warning(
                    "ga4_property_config_file_not_object",
                    extra={"detail": f"{_PROPERTY_CONFIG_FILE} must contain a JSON object; ignoring it"},
                )
        except (OSError, json.JSONDecodeError) as exc:
            _LOGGER.warning(
                "ga4_property_config_file_unreadable",
                extra={"detail": f"{_PROPERTY_CONFIG_FILE}: {exc}; falling back to env vars"},
            )

    env_map = _parse_property_map_json(os.environ.get("GA4_PROPERTY_MAP", ""), "GA4_PROPERTY_MAP")
    for domain, pid in env_map.items():
        property_map.setdefault(domain, pid)

    return property_map, default_id

# GA4 Data API row-limit bounds (runReport accepts 1..250000).
_MIN_LIMIT = 1
_MAX_LIMIT = 250000

# Bounded retry policy for transient Data API failures. Bounded on purpose:
# GA4 enforces per-property token quotas, so an unbounded retry loop against a
# 429 would burn the remaining quota instead of surfacing the limit.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
_RETRYABLE_EXCEPTIONS = (
    google_exceptions.TooManyRequests,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.Aborted,
)

# GA4 dimension/metric API names are alphanumeric with optional underscores and
# colons (e.g. customEvent:my_event). Validated before the request is built so a
# malformed name fails locally rather than consuming a quota token.
_API_NAME_RE = re.compile(r"^[A-Za-z0-9_:]{1,128}$")

_client = None
_client_lock = threading.Lock()


def _get_client() -> BetaAnalyticsDataClient:
    """Return a cached, authenticated GA4 Data API client.

    The client is built once and reused. Constructing one per tool call would
    re-read and re-parse the service account file and open a fresh gRPC channel
    that is never closed, leaking a channel per invocation.

    Returns:
        Authenticated BetaAnalyticsDataClient.

    Raises:
        FileNotFoundError: If the service account JSON file cannot be found at
            the configured path.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client
        if not os.path.exists(CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Credentials file not found: {CREDENTIALS_PATH}. "
                "Set the GOOGLE_APPLICATION_CREDENTIALS env var to the absolute "
                "path of your GA4 service account JSON key."
            )
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        _client = BetaAnalyticsDataClient(credentials=creds)
        return _client


_admin_client = None
_admin_client_lock = threading.Lock()


def _get_admin_client() -> AnalyticsAdminServiceClient:
    """Return a cached, authenticated GA4 Admin API client.

    Kept separate from _get_client() because the Admin API needs broader
    scopes (edit + manage.users) than the read-only Data API client above --
    requesting them together would widen every read tool's token to include
    write scopes it never uses. The service account itself must additionally
    hold Editor (for mark_key_event) or Administrator (for
    grant_property_access / list_access_bindings) role on the target
    property; holding these OAuth scopes is necessary but not sufficient --
    a Viewer-role service account will still get PERMISSION_DENIED from
    Google, surfaced as-is by the tools below rather than pre-checked here.

    Returns:
        Authenticated AnalyticsAdminServiceClient.

    Raises:
        FileNotFoundError: If the service account JSON file cannot be found at
            the configured path.
    """
    global _admin_client
    if _admin_client is not None:
        return _admin_client

    with _admin_client_lock:
        if _admin_client is not None:
            return _admin_client
        if not os.path.exists(CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Credentials file not found: {CREDENTIALS_PATH}. "
                "Set the GOOGLE_APPLICATION_CREDENTIALS env var to the absolute "
                "path of your GA4 service account JSON key."
            )
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=[
                "https://www.googleapis.com/auth/analytics.edit",
                "https://www.googleapis.com/auth/analytics.manage.users",
            ],
        )
        _admin_client = AnalyticsAdminServiceClient(credentials=creds)
        return _admin_client


def _call_with_retry(operation: Callable[[], Any], operation_name: str) -> Any:
    """Run a read-only GA4 API call, retrying transient failures with backoff.

    Retries only the transient classes in _RETRYABLE_EXCEPTIONS, at most
    _MAX_RETRIES times, using capped exponential backoff with full jitter so
    concurrent callers do not retry in lockstep. Permanent errors (permission
    denied, invalid argument, not found) are raised immediately.

    Args:
        operation: Zero-argument callable performing the API request.
        operation_name: Short label used in retry log records.

    Returns:
        Whatever operation() returns.

    Raises:
        google.api_core.exceptions.GoogleAPIError: The final failure once the
            retry budget is exhausted, or immediately for non-transient errors.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return operation()
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt >= _MAX_RETRIES:
                raise
            ceiling = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)
            delay = random.uniform(0.0, ceiling)
            _LOGGER.warning(
                "ga4_api_retry",
                extra={
                    "operation": operation_name,
                    "error_type": type(exc).__name__,
                    "attempt": attempt + 1,
                    "max_attempts": _MAX_RETRIES + 1,
                    "delay_seconds": round(delay, 3),
                },
            )
            time.sleep(delay)

    raise RuntimeError(
        f"GA4 API call '{operation_name}' exhausted {_MAX_RETRIES + 1} attempts."
    )


def _resolve_property(property_id: Optional[str]) -> str:
    """Resolve a property ID or site domain, falling back to the configured default.

    Accepts three forms for ``property_id``: a bare numeric ID
    (``"123456789"``), a fully-qualified resource name
    (``"properties/123456789"``), or a domain registered via
    ``GA4_PROPERTY_CONFIG_FILE`` or ``GA4_PROPERTY_MAP`` (``"example.com"``,
    ``"www.example.com"``). Domain lookup is tried first since digits-only is
    unambiguous but a domain string is not itself a valid property ID, so
    there is no case where the two forms collide. The config file and env var
    are both re-read on every call (see _load_property_config), so an edit to
    the file takes effect on the very next tool call with no server restart.

    Args:
        property_id: Explicit GA4 property ID or domain, or None to use the
            configured default.

    Returns:
        Fully-qualified resource name in the form ``properties/<id>``.

    Raises:
        ValueError: If no property ID is available, or the resolved value is
            neither a known domain nor a bare numeric ID (optionally already
            prefixed with ``properties/``).
    """
    property_map, default_id = _load_property_config()
    pid = (property_id or default_id).strip()
    if not pid:
        available = ", ".join(sorted(property_map)) or "none configured"
        raise ValueError(
            "GA4 property ID required. Pass property_id (numeric ID or a "
            f"domain from list_properties: {available}), or configure a "
            "default in GA4_PROPERTY_CONFIG_FILE or GA4_PROPERTY_ID."
        )

    domain_key = pid.lower()
    if domain_key.startswith("www."):
        domain_key = domain_key[4:]
    if domain_key in property_map:
        bare = property_map[domain_key]
    else:
        bare = pid[len("properties/"):] if pid.startswith("properties/") else pid

    if not bare.isdigit():
        available = ", ".join(sorted(property_map)) or "none configured"
        raise ValueError(
            f"Invalid GA4 property ID or domain {pid!r}. Expected a numeric "
            "property ID (e.g. '123456789' or 'properties/123456789') or one "
            f"of the domains from list_properties: {available}."
        )
    return f"properties/{bare}"


@mcp.tool(annotations=_READ_REMOTE)
def list_properties() -> str:
    """List the configured GA4 properties, plus the default.

    Call this first when working with a site whose property ID you don't
    already know, or when unsure which property a bare property_id argument
    would resolve to. Reads GA4_PROPERTY_CONFIG_FILE (default:
    properties.local.json next to the server) and the GA4_PROPERTY_MAP /
    GA4_PROPERTY_ID env vars fresh on every call -- this tool only reports
    what's currently configured there, it does not query the Analytics Admin
    API, so an unregistered property must still be added to the config file
    or env var to be resolvable by domain name.

    Returns:
        JSON string with ``properties`` (list of {domain, property_id}) and
        ``default_property_id`` (or null if none configured).
    """
    property_map, default_id = _load_property_config()
    return json.dumps(
        {
            "properties": [
                {"domain": domain, "property_id": pid}
                for domain, pid in sorted(property_map.items())
            ],
            "default_property_id": default_id or None,
        },
        indent=2,
    )


def _parse_api_names(raw: str, field_name: str) -> List[str]:
    """Split a comma-separated GA4 dimension/metric list and validate each name.

    Args:
        raw: Comma-separated API names as supplied by the caller.
        field_name: Name of the parameter, used in error messages.

    Returns:
        List of validated, non-empty API name strings.

    Raises:
        ValueError: If the list is empty or any entry is not a valid API name.
    """
    names = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not names:
        raise ValueError(f"{field_name} must contain at least one API name.")

    invalid = [name for name in names if not _API_NAME_RE.match(name)]
    if invalid:
        raise ValueError(
            f"Invalid {field_name} name(s): {invalid}. Expected GA4 API names "
            "containing only letters, digits, underscores, or colons."
        )
    return names


def _validate_limit(limit: int) -> int:
    """Validate a requested row limit against the GA4 Data API bounds.

    Args:
        limit: Caller-supplied maximum row count.

    Returns:
        The validated limit.

    Raises:
        ValueError: If the limit falls outside the API's accepted range.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit must be an integer, got {type(limit).__name__}.")
    if not _MIN_LIMIT <= limit <= _MAX_LIMIT:
        raise ValueError(
            f"limit must be between {_MIN_LIMIT} and {_MAX_LIMIT}, got {limit}."
        )
    return limit


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def get_ga4_report(
    dimensions: str,
    metrics: str,
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Run a GA4 report.

    Returns at most ``limit`` rows and always reports the total number of rows
    the query matched, so a truncated result is never mistaken for a complete
    one. When ``truncated`` is true, re-run with a larger ``limit``.

    Args:
        dimensions: Comma-separated GA4 dimension API names
            (e.g. 'sessionSource,sessionMedium'). At least one is required.
        metrics: Comma-separated GA4 metric API names
            (e.g. 'sessions,activeUsers'). At least one is required.
        start_date: Inclusive start date, GA4 syntax ('30daysAgo', 'YYYY-MM-DD').
        end_date: Inclusive end date, GA4 syntax ('today', 'YYYY-MM-DD').
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max rows to return, 1-250000 (default 10).

    Returns:
        JSON string with keys: property, rows, returned_rows, total_rows,
        truncated, limit.

    Raises:
        ValueError: If dimensions, metrics, limit, or property_id are invalid.
    """
    dimension_names = _parse_api_names(dimensions, "dimensions")
    metric_names = _parse_api_names(metrics, "metrics")
    limit = _validate_limit(limit)

    client = _get_client()
    prop = _resolve_property(property_id)

    request = RunReportRequest(
        property=prop,
        dimensions=[Dimension(name=name) for name in dimension_names],
        metrics=[Metric(name=name) for name in metric_names],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=limit,
    )
    response = _call_with_retry(lambda: client.run_report(request), "run_report")

    rows = []
    for row in response.rows:
        record = {}
        for i, dim in enumerate(response.dimension_headers):
            record[dim.name] = row.dimension_values[i].value
        for i, met in enumerate(response.metric_headers):
            record[met.name] = row.metric_values[i].value
        rows.append(record)

    total_rows = int(getattr(response, "row_count", 0) or 0)
    return json.dumps(
        {
            "property": prop,
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": total_rows,
            "truncated": total_rows > len(rows),
            "limit": limit,
        },
        indent=2,
    )


# Not rate-limited here: the body below returns get_ga4_report(...) directly
# and does not call the Data API itself, so get_ga4_report's own gate is what
# protects the quota. Gating this wrapper too would consume two tokens for
# one logical operation and, on denial, report the wrong tool name back to
# the caller.
@mcp.tool(annotations=_READ_REMOTE)
def get_top_pages(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get top pages by pageviews.

    Args:
        start_date: Inclusive start date, GA4 syntax (default '30daysAgo').
        end_date: Inclusive end date, GA4 syntax (default 'today').
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max pages to return, 1-250000 (default 10).

    Returns:
        JSON with top pages and their screenPageViews, plus total_rows and a
        truncated flag indicating whether more matching pages exist.
    """
    return get_ga4_report(
        dimensions="pagePath,pageTitle",
        metrics="screenPageViews,sessions,bounceRate",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


# Not rate-limited: delegates entirely to get_ga4_report(...), same reasoning
# as get_top_pages above.
@mcp.tool(annotations=_READ_REMOTE)
def get_traffic_sources(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get traffic sources breakdown by session source and medium.

    Args:
        start_date: Inclusive start date, GA4 syntax (default '30daysAgo').
        end_date: Inclusive end date, GA4 syntax (default 'today').
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max rows to return, 1-250000 (default 10).

    Returns:
        JSON with source/medium breakdown, plus total_rows and a truncated flag
        indicating whether more matching rows exist.
    """
    return get_ga4_report(
        dimensions="sessionSource,sessionMedium",
        metrics="sessions,activeUsers,bounceRate",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


# Not rate-limited: delegates entirely to get_ga4_report(...), same reasoning
# as get_top_pages above.
@mcp.tool(annotations=_READ_REMOTE)
def get_user_metrics(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 400,
) -> str:
    """Get daily user metrics (sessions, users, bounce rate, avg session duration).

    One row per day in the requested range. ``limit`` therefore bounds the number
    of days returned; the response's total_rows and truncated fields reveal when
    the range was longer than the limit.

    Args:
        start_date: Inclusive start date, GA4 syntax (default '30daysAgo').
        end_date: Inclusive end date, GA4 syntax (default 'today').
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max days to return, 1-250000 (default 400, covers just over a year).

    Returns:
        JSON with one row per day, plus total_rows and a truncated flag.
    """
    return get_ga4_report(
        dimensions="date",
        metrics="sessions,activeUsers,newUsers,bounceRate,averageSessionDuration",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def get_realtime_users(
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get realtime active users broken down by page path and country.

    The breakdown is bounded by ``limit``, so its per-row activeUsers values do
    not necessarily sum to the property's true realtime total. The summed value
    is therefore reported as ``active_users_in_breakdown``, alongside total_rows
    and a truncated flag, rather than as a property-wide total.

    Args:
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max breakdown rows to return, 1-250000 (default 10).

    Returns:
        JSON with the page/country breakdown, active_users_in_breakdown,
        returned_rows, total_rows, and truncated.

    Raises:
        ValueError: If limit or property_id are invalid.
    """
    limit = _validate_limit(limit)

    client = _get_client()
    prop = _resolve_property(property_id)

    request = RunRealtimeReportRequest(
        property=prop,
        dimensions=[Dimension(name="pagePath"), Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        limit=limit,
    )
    response = _call_with_retry(
        lambda: client.run_realtime_report(request), "run_realtime_report"
    )

    rows = []
    for row in response.rows:
        rows.append({
            "pagePath": row.dimension_values[0].value,
            "country": row.dimension_values[1].value,
            "activeUsers": row.metric_values[0].value,
        })

    breakdown_total = sum(int(r["activeUsers"] or 0) for r in rows)
    total_rows = int(getattr(response, "row_count", 0) or 0)

    return json.dumps(
        {
            "property": prop,
            "active_users_in_breakdown": breakdown_total,
            "breakdown": rows,
            "returned_rows": len(rows),
            "total_rows": total_rows,
            "truncated": total_rows > len(rows),
            "limit": limit,
        },
        indent=2,
    )


# Not rate-limited: delegates entirely to get_ga4_report(...), same reasoning
# as get_top_pages above.
@mcp.tool(annotations=_READ_REMOTE)
def get_conversion_events(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get conversion events breakdown by event name.

    Args:
        start_date: Inclusive start date, GA4 syntax (default '30daysAgo').
        end_date: Inclusive end date, GA4 syntax (default 'today').
        property_id: Numeric GA4 property ID, or a domain registered in GA4_PROPERTY_MAP
            (see list_properties). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max rows to return, 1-250000 (default 10).

    Returns:
        JSON with event name and conversion counts, plus total_rows and a
        truncated flag indicating whether more matching events exist.
    """
    return get_ga4_report(
        dimensions="eventName",
        metrics="eventCount,conversions",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


# GA4 Admin API predefined property roles (accessBindings.roles accepts these
# resource names, plus a small set of add-on roles not useful for a single
# grant call). Validated locally so a typo does not silently create a
# binding with an unintended role.
_PREDEFINED_ROLES = {
    "predefinedRoles/viewer",
    "predefinedRoles/analyst",
    "predefinedRoles/editor",
    "predefinedRoles/admin",
    "predefinedRoles/marketer",
}


@mcp.tool(annotations=_WRITE_REMOTE_SAFE)
def mark_key_event(event_name: str, property_id: Optional[str] = None) -> str:
    """Mark a GA4 event as a key event (conversion).

    Requires the configured service account to hold Editor or Administrator
    role on the target property -- Viewer is not sufficient and this call
    will fail with PERMISSION_DENIED under Viewer-only access.

    Idempotent: if event_name is already marked as a key event, this returns
    success with already_marked=true rather than an error, since the caller's
    intent ("make sure this is a key event") is already satisfied.

    Args:
        event_name: GA4 event name to mark, e.g. 'generate_lead'.
        property_id: Numeric GA4 property ID, or a domain registered in
            GA4_PROPERTY_MAP (see list_properties). Uses GA4_PROPERTY_ID env
            var if omitted.

    Returns:
        JSON string with property, event_name, already_marked, and (when
        newly created) the key event's resource name.

    Raises:
        ValueError: If property_id is invalid.
        google.api_core.exceptions.GoogleAPIError: On permission or API
            errors other than AlreadyExists.
    """
    if not event_name or not event_name.strip():
        raise ValueError("event_name is required.")
    event_name = event_name.strip()

    client = _get_admin_client()
    prop = _resolve_property(property_id)

    try:
        result = client.create_key_event(
            parent=prop,
            key_event=KeyEvent(
                event_name=event_name,
                counting_method=KeyEvent.CountingMethod.ONCE_PER_EVENT,
            ),
        )
    except google_exceptions.AlreadyExists:
        return json.dumps({
            "property": prop,
            "event_name": event_name,
            "already_marked": True,
        }, indent=2)

    return json.dumps({
        "property": prop,
        "event_name": event_name,
        "already_marked": False,
        "key_event_name": result.name,
    }, indent=2)


@mcp.tool(annotations=_WRITE_REMOTE_SAFE)
def grant_property_access(
    user_email: str,
    role: str = "predefinedRoles/viewer",
    property_id: Optional[str] = None,
) -> str:
    """Grant a Google account access to a GA4 property.

    Requires the configured service account to hold Administrator role on
    the target property -- this is a higher bar than mark_key_event's Editor
    requirement, since managing other users' access is itself an admin-only
    action in GA4. A service account that only has Viewer or Editor access
    will get PERMISSION_DENIED here even though it can read reports or mark
    key events fine.

    Idempotent: granting a role the user already holds succeeds without
    creating a duplicate binding (the Admin API itself de-dupes on
    user+role).

    Args:
        user_email: Google account email to grant access to.
        role: One of predefinedRoles/viewer, predefinedRoles/analyst,
            predefinedRoles/editor, predefinedRoles/admin,
            predefinedRoles/marketer. Defaults to viewer (read-only), the
            least-privilege choice for a reporting integration.
        property_id: Numeric GA4 property ID, or a domain registered in
            GA4_PROPERTY_MAP (see list_properties). Uses GA4_PROPERTY_ID env
            var if omitted.

    Returns:
        JSON string with property, user_email, role, and the created access
        binding's resource name.

    Raises:
        ValueError: If user_email, role, or property_id are invalid.
        google.api_core.exceptions.GoogleAPIError: On permission or API
            errors.
    """
    if not user_email or "@" not in user_email:
        raise ValueError(f"user_email must be a valid email address, got {user_email!r}.")
    if role not in _PREDEFINED_ROLES:
        raise ValueError(
            f"Invalid role {role!r}. Expected one of: {sorted(_PREDEFINED_ROLES)}."
        )

    client = _get_admin_client()
    prop = _resolve_property(property_id)

    result = client.create_access_binding(
        parent=prop,
        access_binding=AccessBinding(user=user_email, roles=[role]),
    )

    return json.dumps({
        "property": prop,
        "user_email": user_email,
        "role": role,
        "access_binding_name": result.name,
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
def list_access_bindings(property_id: Optional[str] = None) -> str:
    """List Google accounts with access to a GA4 property, and their roles.

    Requires the configured service account to hold at least Editor role on
    the target property. Use this to verify a grant_property_access call
    took effect, or to audit who currently has access before granting more.

    Args:
        property_id: Numeric GA4 property ID, or a domain registered in
            GA4_PROPERTY_MAP (see list_properties). Uses GA4_PROPERTY_ID env
            var if omitted.

    Returns:
        JSON string with property and a list of {user_email, roles}.

    Raises:
        ValueError: If property_id is invalid.
        google.api_core.exceptions.GoogleAPIError: On permission or API
            errors.
    """
    client = _get_admin_client()
    prop = _resolve_property(property_id)

    bindings = client.list_access_bindings(parent=prop)
    return json.dumps({
        "property": prop,
        "bindings": [
            {"user_email": b.user, "roles": list(b.roles)}
            for b in bindings
        ],
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
