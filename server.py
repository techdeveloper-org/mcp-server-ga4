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


def _load_property_map() -> dict:
    """Parse GA4_PROPERTY_MAP into a domain -> numeric-property-id lookup.

    The env var holds a JSON object, e.g. '{"example.com": "123456789"}'.
    Keys are normalized (lowercased, stripped of a leading "www.") so callers
    can pass a bare domain in any of its common written forms. A missing or
    malformed env var yields an empty map rather than raising, since the
    numeric-ID path (GA4_PROPERTY_ID / explicit property_id) must keep
    working even when no map has been configured.

    Returns:
        Dict from normalized domain to numeric property ID string.
    """
    raw = os.environ.get("GA4_PROPERTY_MAP", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning(
            "ga4_property_map_invalid_json",
            extra={"detail": "GA4_PROPERTY_MAP is not valid JSON; ignoring it"},
        )
        return {}
    if not isinstance(parsed, dict):
        _LOGGER.warning(
            "ga4_property_map_not_object",
            extra={"detail": "GA4_PROPERTY_MAP must be a JSON object; ignoring it"},
        )
        return {}

    def _normalize_domain(domain: str) -> str:
        d = domain.strip().lower()
        return d[4:] if d.startswith("www.") else d

    return {_normalize_domain(k): str(v).strip() for k, v in parsed.items()}


PROPERTY_MAP = _load_property_map()

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
    """Resolve a property ID or site domain, falling back to env var.

    Accepts three forms for ``property_id``: a bare numeric ID
    (``"123456789"``), a fully-qualified resource name
    (``"properties/123456789"``), or a domain registered in
    ``GA4_PROPERTY_MAP`` (``"example.com"``, ``"www.example.com"``). Domain
    lookup is tried first since digits-only is unambiguous but a domain
    string is not itself a valid property ID, so there is no case where the
    two forms collide.

    Args:
        property_id: Explicit GA4 property ID or domain, or None to use
            GA4_PROPERTY_ID.

    Returns:
        Fully-qualified resource name in the form ``properties/<id>``.

    Raises:
        ValueError: If no property ID is available, or the resolved value is
            neither a known domain nor a bare numeric ID (optionally already
            prefixed with ``properties/``).
    """
    pid = (property_id or PROPERTY_ID).strip()
    if not pid:
        available = ", ".join(sorted(PROPERTY_MAP)) or "none configured"
        raise ValueError(
            "GA4 property ID required. Pass property_id (numeric ID or a "
            f"domain from GA4_PROPERTY_MAP: {available}), or set "
            "GA4_PROPERTY_ID env var."
        )

    domain_key = pid.lower()
    if domain_key.startswith("www."):
        domain_key = domain_key[4:]
    if domain_key in PROPERTY_MAP:
        bare = PROPERTY_MAP[domain_key]
    else:
        bare = pid[len("properties/"):] if pid.startswith("properties/") else pid

    if not bare.isdigit():
        available = ", ".join(sorted(PROPERTY_MAP)) or "none configured"
        raise ValueError(
            f"Invalid GA4 property ID or domain {pid!r}. Expected a numeric "
            "property ID (e.g. '123456789' or 'properties/123456789') or one "
            f"of the domains in GA4_PROPERTY_MAP: {available}."
        )
    return f"properties/{bare}"


@mcp.tool(annotations=_READ_REMOTE)
def list_properties() -> str:
    """List the GA4 properties registered in GA4_PROPERTY_MAP, plus the default.

    Call this first when working with a site whose property ID you don't
    already know, or when unsure which property a bare property_id argument
    would resolve to. The map is configured once in the server's environment
    (GA4_PROPERTY_MAP as a JSON object of domain -> numeric property ID) --
    this tool only reports what's there, it does not query the Analytics
    Admin API, so an unregistered property must still be added to the env var
    to be resolvable by domain name.

    Returns:
        JSON string with ``properties`` (list of {domain, property_id}) and
        ``default_property_id`` (from GA4_PROPERTY_ID, or null if unset).
    """
    return json.dumps(
        {
            "properties": [
                {"domain": domain, "property_id": pid}
                for domain, pid in sorted(PROPERTY_MAP.items())
            ],
            "default_property_id": PROPERTY_ID or None,
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


if __name__ == "__main__":
    mcp.run()
