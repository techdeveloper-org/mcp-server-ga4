# mcp-server-ga4

Google Analytics 4 MCP Server — provides GA4 reporting tools via the Google Analytics Data API
v1beta, plus a small set of GA4 Admin API (v1alpha) tools for configuration changes that have
no other API surface (marking key events, managing property access).

## Tools

### Reporting (Data API, requires Viewer role)

| Tool | Description |
|------|-------------|
| `list_properties` | List the domains/property IDs currently configured (call this first) |
| `get_ga4_report` | Run a custom GA4 report with any dimensions and metrics |
| `get_top_pages` | Top pages by pageviews |
| `get_traffic_sources` | Traffic source/medium breakdown |
| `get_user_metrics` | Aggregate user metrics (sessions, users, bounce rate) |
| `get_realtime_users` | Current realtime active users |
| `get_conversion_events` | Conversion events breakdown |

Every tool above `list_properties` takes an optional `property_id` argument. Pass a numeric
GA4 property ID, a fully-qualified `properties/<id>` resource name, or (if configured, see
below) a bare domain like `"example.com"`. Omit it to use the configured default.

### Admin (Admin API, requires Editor or Administrator role — see per-tool notes)

| Tool | Description | Service account role required |
|------|-------------|-------------------------------|
| `mark_key_event` | Mark a GA4 event as a key event (conversion) | Editor |
| `grant_property_access` | Grant a Google account access to a property | Administrator |
| `list_access_bindings` | List who has access to a property, and their roles | Editor |

These exist because neither the GA4 UI action "mark as key event" nor "grant property access"
has any other API — the Data API this server otherwise uses is read-only by design. Both write
tools are idempotent: re-marking an already-marked event or re-granting an already-held role
succeeds without creating a duplicate.

**A Viewer-role service account (the minimum this server otherwise needs) cannot call the Admin
tools** — Google's own permission model requires Editor for `mark_key_event` and Administrator
for the other two, regardless of what scopes the service account's credentials request. If a
call fails with `PERMISSION_DENIED`, raise that property's role for the service account in
GA4 Admin → Property Access Management, rather than treating it as a bug here.

Also requires the **Google Analytics Admin API** enabled on the same Google Cloud project as
the service account (separate from the Data API used by the reporting tools above) — enable it
at `console.cloud.google.com/apis/library/analyticsadmin.googleapis.com`.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Enable **Google Analytics Data API** (reporting tools) and, if you plan to use the Admin
   tools above, **Google Analytics Admin API** too
3. Create a Service Account → download JSON key
4. In GA4, for **every** property this server should read: Admin → Property Access Management →
   add the service account's email with **Viewer** role (or **Editor**/**Administrator** if you
   need the Admin tools on that property — see the table above). A property the service account
   isn't added to will fail with a permission error even if it's listed in
   `properties.local.json` below — the file only controls ID *resolution*, not GA4 access.

### 3. Register the server with Claude Code

Run `claude mcp add` (or your MCP host's equivalent) rather than hand-editing a config file —
**this server's env config is registered in `~/.claude.json` (the user-level config Claude Code
itself manages), not in `~/.claude/settings.json`.** `settings.json` holds permissions/hooks and
is easy to mistake for the MCP registry since both live under `~/.claude/`, but editing it has no
effect on which env vars this server receives. If you do need to edit the registration directly,
edit the `mcpServers.google-analytics-ga4` block (and, for a per-project registration, the
matching entry under `projects.<path>.mcpServers`) in `~/.claude.json`:

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

**Important:** an MCP host typically reads a server's env config once, when the session that
first launches it starts, and does not notice edits made to that config file while the session
is still running or being resumed — `/mcp` reconnect restarts the subprocess but does not
re-read the file, and even a full app restart can end up resuming the same session rather than
starting fresh. If a config edit doesn't seem to take effect, don't assume it's this server's
bug: use `list_properties` to check what the running process actually has, and if it disagrees
with the file, the host hasn't picked up the change yet (a genuinely new session, not a resumed
one, is the reliable fix; see the multi-property config below for a way to sidestep this
entirely for property IDs).

### 4. Get your GA4 Property ID

GA4 Console → Admin → Property → Property ID (numeric, e.g. `123456789`)

## Multi-property setup (recommended if you manage more than one site)

`GA4_PROPERTY_ID` only sets a single default, and per the caveat above, changing it means
re-registering the server. For anything beyond one property, use a local config file instead —
edits to it take effect on the **very next tool call**, no reconnect or restart needed at all:

1. Copy `properties.local.example.json` to `properties.local.json` (same directory as
   `server.py`; this filename is gitignored so real property IDs never get committed).
2. Fill in your domain → property ID map:

   ```json
   {
     "default": "123456789",
     "properties": {
       "example.com": "123456789",
       "another-site.com": "987654321"
     }
   }
   ```
3. Call `list_properties()` to confirm what's loaded, then pass a domain as `property_id` on
   any tool, e.g. `get_top_pages(property_id="example.com")`.

`GA4_PROPERTY_MAP` (a JSON object in the env, same shape as the `"properties"` key above) and
`GA4_PROPERTY_ID` still work as a fallback for anything the file doesn't supply, so an
env-var-only setup keeps working unchanged if you don't want the file.

## Usage

Once configured, tools are available in Claude Code:

```
list_properties()
get_top_pages(start_date="7daysAgo", end_date="today", limit=10, property_id="example.com")
get_traffic_sources(start_date="30daysAgo")
get_realtime_users()
```

## Part of techdeveloper-org MCP Suite

This server is part of a 13-server MCP suite. See [techdeveloper-org](https://github.com/orgs/techdeveloper-org/repositories) for all servers.
