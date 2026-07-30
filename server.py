"""Google Analytics 4 MCP Server.

Provides GA4 reporting tools via the Google Analytics Data API v1.
Authentication: service account JSON (GOOGLE_APPLICATION_CREDENTIALS env var).
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
    RunRealtimeReportRequest,
)
from google.oauth2 import service_account

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both names are probed so this server runs under either major version; the
# API used below (tool decorator, run(transport=...)) is identical in both.
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

mcp = MCPServer("ga4-server")

PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "service_account.json"),
)


def _get_client():
    """Return authenticated GA4 client."""
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}. "
            "Set GOOGLE_APPLICATION_CREDENTIALS env var."
        )
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _resolve_property(property_id: Optional[str]) -> str:
    """Resolve property ID, falling back to env var."""
    pid = property_id or PROPERTY_ID
    if not pid:
        raise ValueError(
            "GA4 property ID required. Pass property_id or set GA4_PROPERTY_ID env var."
        )
    return f"properties/{pid}" if not pid.startswith("properties/") else pid


@mcp.tool()
def get_ga4_report(
    dimensions: str,
    metrics: str,
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Run a GA4 report.

    Args:
        dimensions: Comma-separated dimension names (e.g. 'sessionSource,sessionMedium').
        metrics: Comma-separated metric names (e.g. 'sessions,activeUsers').
        start_date: Start date string (e.g. '30daysAgo', '2024-01-01').
        end_date: End date string (e.g. 'today', '2024-01-31').
        property_id: GA4 property ID (numeric). Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max rows to return (default 10).

    Returns:
        JSON string with report rows.
    """
    client = _get_client()
    prop = _resolve_property(property_id)

    request = RunReportRequest(
        property=prop,
        dimensions=[Dimension(name=d.strip()) for d in dimensions.split(",")],
        metrics=[Metric(name=m.strip()) for m in metrics.split(",")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=limit,
    )
    response = client.run_report(request)

    rows = []
    for row in response.rows:
        record = {}
        for i, dim in enumerate(response.dimension_headers):
            record[dim.name] = row.dimension_values[i].value
        for i, met in enumerate(response.metric_headers):
            record[met.name] = row.metric_values[i].value
        rows.append(record)

    return json.dumps({"property": prop, "rows": rows, "row_count": len(rows)}, indent=2)


@mcp.tool()
def get_top_pages(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get top pages by pageviews.

    Args:
        start_date: Start date (default '30daysAgo').
        end_date: End date (default 'today').
        property_id: GA4 property ID. Uses GA4_PROPERTY_ID env var if omitted.
        limit: Max pages to return.

    Returns:
        JSON with top pages and their screenPageViews.
    """
    return get_ga4_report(
        dimensions="pagePath,pageTitle",
        metrics="screenPageViews,sessions,bounceRate",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


@mcp.tool()
def get_traffic_sources(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get traffic sources breakdown.

    Args:
        start_date: Start date.
        end_date: End date.
        property_id: GA4 property ID.
        limit: Max rows.

    Returns:
        JSON with source/medium breakdown.
    """
    return get_ga4_report(
        dimensions="sessionSource,sessionMedium",
        metrics="sessions,activeUsers,bounceRate",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=limit,
    )


@mcp.tool()
def get_user_metrics(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
) -> str:
    """Get overall user metrics (sessions, users, bounce rate, avg session duration).

    Args:
        start_date: Start date.
        end_date: End date.
        property_id: GA4 property ID.

    Returns:
        JSON with aggregate user metrics.
    """
    return get_ga4_report(
        dimensions="date",
        metrics="sessions,activeUsers,newUsers,bounceRate,averageSessionDuration",
        start_date=start_date,
        end_date=end_date,
        property_id=property_id,
        limit=90,
    )


@mcp.tool()
def get_realtime_users(property_id: Optional[str] = None) -> str:
    """Get current realtime active users.

    Args:
        property_id: GA4 property ID.

    Returns:
        JSON with realtime active user count and top pages.
    """
    client = _get_client()
    prop = _resolve_property(property_id)

    request = RunRealtimeReportRequest(
        property=prop,
        dimensions=[Dimension(name="pagePath"), Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        limit=10,
    )
    response = client.run_realtime_report(request)

    rows = []
    for row in response.rows:
        rows.append({
            "pagePath": row.dimension_values[0].value,
            "country": row.dimension_values[1].value,
            "activeUsers": row.metric_values[0].value,
        })

    total = sum(int(r["activeUsers"]) for r in rows)
    return json.dumps({"property": prop, "realtime_active_users": total, "breakdown": rows}, indent=2)


@mcp.tool()
def get_conversion_events(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    property_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Get conversion events breakdown.

    Args:
        start_date: Start date.
        end_date: End date.
        property_id: GA4 property ID.
        limit: Max rows.

    Returns:
        JSON with event name and conversion counts.
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
