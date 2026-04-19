# mcp-server-ga4

Google Analytics 4 MCP Server — provides GA4 reporting tools via the Google Analytics Data API v1beta.

## Tools

| Tool | Description |
|------|-------------|
| `get_ga4_report` | Run a custom GA4 report with any dimensions and metrics |
| `get_top_pages` | Top pages by pageviews |
| `get_traffic_sources` | Traffic source/medium breakdown |
| `get_user_metrics` | Aggregate user metrics (sessions, users, bounce rate) |
| `get_realtime_users` | Current realtime active users |
| `get_conversion_events` | Conversion events breakdown |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Enable **Google Analytics Data API**
3. Create a Service Account → download JSON key
4. In GA4: Admin → Property Access Management → add the service account email with **Viewer** role

### 3. Configure Claude Code (`~/.claude/settings.json`)

```json
{
  "mcpServers": {
    "google-analytics-ga4": {
      "command": "python",
      "args": ["C:/path/to/mcp-server-ga4/server.py"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "C:/path/to/service_account.json",
        "GA4_PROPERTY_ID": "YOUR_NUMERIC_PROPERTY_ID"
      }
    }
  }
}
```

### 4. Get your GA4 Property ID

GA4 Console → Admin → Property → Property ID (numeric, e.g. `123456789`)

## Usage

Once configured, tools are available in Claude Code:

```
get_top_pages(start_date="7daysAgo", end_date="today", limit=10)
get_traffic_sources(start_date="30daysAgo")
get_realtime_users()
```

## Part of techdeveloper-org MCP Suite

This server is part of a 13-server MCP suite. See [techdeveloper-org](https://github.com/orgs/techdeveloper-org/repositories) for all servers.
