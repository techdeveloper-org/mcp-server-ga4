"""Tests for server.py.

All Google Analytics Data API calls are mocked -- no real credentials are
needed to run these tests.
Run with: pytest test_server.py -v
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


def _load_module(env_overrides=None):
    """Import server.py with a fresh environment.

    Args:
        env_overrides: Extra environment variables to apply for the duration
            of the import, merged over a minimal working default.

    Returns:
        The freshly imported ``server`` module.
    """
    env = {
        "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/service_account.json",
        "GA4_PROPERTY_ID": "123456789",
        **(env_overrides or {}),
    }
    with patch.dict(os.environ, env, clear=False):
        if "server" in sys.modules:
            del sys.modules["server"]
        import server as mod
    return mod


def _make_report_response():
    """Return a MagicMock standing in for a GA4 RunReportResponse with no rows."""
    response = MagicMock()
    response.rows = []
    response.dimension_headers = []
    response.metric_headers = []
    response.row_count = 0
    return response


def _make_realtime_response():
    """Return a MagicMock standing in for a GA4 RunRealtimeReportResponse with no rows."""
    response = MagicMock()
    response.rows = []
    response.row_count = 0
    return response


class TestGetGa4Report(unittest.TestCase):

    def test_returns_json_with_property_and_empty_rows(self):
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch("server._get_client", return_value=client):
            result = mod.get_ga4_report(
                dimensions="sessionSource", metrics="sessions", property_id="123456789"
            )
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["total_rows"], 0)
        self.assertFalse(data["truncated"])

    def test_invalid_dimension_name_raises_value_error(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.get_ga4_report(
                    dimensions="not valid!", metrics="sessions", property_id="123456789"
                )


class TestGetRealtimeUsers(unittest.TestCase):

    def test_returns_json_with_breakdown(self):
        mod = _load_module()
        client = MagicMock()
        client.run_realtime_report.return_value = _make_realtime_response()
        with patch("server._get_client", return_value=client):
            result = mod.get_realtime_users(property_id="123456789")
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")
        self.assertEqual(data["active_users_in_breakdown"], 0)


class TestDelegatingWrapperTools(unittest.TestCase):
    """get_top_pages / get_traffic_sources / get_user_metrics / get_conversion_events
    all delegate their entire body to get_ga4_report(...).
    """

    def test_get_top_pages_delegates_and_returns_valid_json(self):
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch("server._get_client", return_value=client):
            result = mod.get_top_pages(property_id="123456789")
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")

    def test_get_conversion_events_delegates_and_returns_valid_json(self):
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch("server._get_client", return_value=client):
            result = mod.get_conversion_events(property_id="123456789")
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")


# ---------------------------------------------------------------------------
# TestAdminTools
# ---------------------------------------------------------------------------

class TestAdminTools(unittest.TestCase):
    """mark_key_event / grant_property_access / list_access_bindings.

    All three go through _get_admin_client(), a separate cached client from
    the Data API's _get_client(), so each test patches that one specifically.
    """

    def test_mark_key_event_returns_new_key_event_name(self):
        # MagicMock(name=...) sets the mock's own repr, not a `.name`
        # attribute -- that attribute must be assigned after construction.
        mod = _load_module()
        client = MagicMock()
        key_event = MagicMock()
        key_event.name = "properties/123456789/keyEvents/1"
        client.create_key_event.return_value = key_event
        with patch("server._get_admin_client", return_value=client):
            result = mod.mark_key_event("generate_lead", property_id="123456789")
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")
        self.assertEqual(data["event_name"], "generate_lead")
        self.assertFalse(data["already_marked"])
        self.assertEqual(data["key_event_name"], "properties/123456789/keyEvents/1")

    def test_mark_key_event_already_marked_is_not_an_error(self):
        """AlreadyExists means the caller's intent is already satisfied."""
        from google.api_core import exceptions as google_exceptions
        mod = _load_module()
        client = MagicMock()
        client.create_key_event.side_effect = google_exceptions.AlreadyExists("dup")
        with patch("server._get_admin_client", return_value=client):
            result = mod.mark_key_event("generate_lead", property_id="123456789")
        data = json.loads(result)
        self.assertTrue(data["already_marked"])
        self.assertNotIn("key_event_name", data)

    def test_mark_key_event_requires_non_empty_event_name(self):
        mod = _load_module()
        with patch("server._get_admin_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.mark_key_event("   ", property_id="123456789")

    def test_grant_property_access_returns_binding_name(self):
        mod = _load_module()
        client = MagicMock()
        binding_result = MagicMock()
        binding_result.name = "properties/123456789/accessBindings/1"
        client.create_access_binding.return_value = binding_result
        with patch("server._get_admin_client", return_value=client):
            result = mod.grant_property_access(
                "someone@example.com", property_id="123456789"
            )
        data = json.loads(result)
        self.assertEqual(data["user_email"], "someone@example.com")
        self.assertEqual(data["role"], "predefinedRoles/viewer")
        self.assertEqual(data["access_binding_name"], "properties/123456789/accessBindings/1")

    def test_grant_property_access_rejects_invalid_role(self):
        mod = _load_module()
        with patch("server._get_admin_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.grant_property_access(
                    "someone@example.com", role="predefinedRoles/superadmin",
                    property_id="123456789",
                )

    def test_grant_property_access_rejects_malformed_email(self):
        mod = _load_module()
        with patch("server._get_admin_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.grant_property_access("not-an-email", property_id="123456789")

    def test_list_access_bindings_returns_user_and_roles(self):
        mod = _load_module()
        client = MagicMock()
        binding = MagicMock(user="someone@example.com", roles=["predefinedRoles/viewer"])
        client.list_access_bindings.return_value = [binding]
        with patch("server._get_admin_client", return_value=client):
            result = mod.list_access_bindings(property_id="123456789")
        data = json.loads(result)
        self.assertEqual(data["property"], "properties/123456789")
        self.assertEqual(
            data["bindings"],
            [{"user_email": "someone@example.com", "roles": ["predefinedRoles/viewer"]}],
        )


# ---------------------------------------------------------------------------
# TestRateLimiting
# ---------------------------------------------------------------------------

class TestRateLimiting(unittest.TestCase):
    """Exercises the local ``rate_limited`` gate wired into server.py.

    Mirrors mcp-base/tests/test_rate_limit_enforcement.py. The defect these
    guard against is not a wrong limit but an absent one: a tool decorated
    with ``@rate_limited(...)`` that never actually calls ``check_rate_limit``
    would still pass every functional test above this class.

    ``ENABLE_RATE_LIMITING`` is read fresh on every call inside
    ``rate_limiter.check_rate_limit``, not cached at import time, so each test
    that wants enforcement wraps its own call loop in ``patch.dict`` rather
    than relying on ``_load_module``'s import-time-only environment override.
    """

    def setUp(self):
        import rate_limiter
        self._rate_limiter = rate_limiter
        with rate_limiter._buckets_lock:
            rate_limiter._buckets.clear()

    def tearDown(self):
        with self._rate_limiter._buckets_lock:
            self._rate_limiter._buckets.clear()

    def _call_many(self, fn, times, **kwargs):
        """Invoke ``fn`` ``times`` times and tally allowed vs. denied calls.

        A call counts as denied only when its JSON body carries
        ``error_type == "RateLimitExceeded"``; any other JSON payload counts
        as allowed.
        """
        allowed, denied, last_denial = 0, 0, None
        for _ in range(times):
            result = fn(**kwargs)
            try:
                payload = json.loads(result)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get("error_type") == "RateLimitExceeded":
                denied += 1
                last_denial = payload
            else:
                allowed += 1
        return allowed, denied, last_denial

    def test_no_limiting_without_the_env_var(self):
        """The default (unset) must not change behaviour for anyone."""
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch("server._get_client", return_value=client):
            allowed, denied, _ = self._call_many(
                mod.get_ga4_report, 250, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
        self.assertEqual((allowed, denied), (250, 0))

    def test_no_bucket_state_is_created_when_disabled(self):
        """Disabled means no work, not merely no denial."""
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch("server._get_client", return_value=client):
            self._call_many(
                mod.get_ga4_report, 50, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
        self.assertEqual(self._rate_limiter._buckets, {})

    def test_calls_are_denied_once_the_tool_calls_bucket_drains(self):
        """The tool_calls bucket holds 100 tokens."""
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            allowed, denied, _ = self._call_many(
                mod.get_ga4_report, 130, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
        self.assertEqual(allowed, 100)
        self.assertEqual(denied, 30)

    def test_denial_is_a_structured_result_not_an_exception(self):
        """A rate limit is an expected operational condition, not a crash."""
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            _, _, denial = self._call_many(
                mod.get_ga4_report, 130, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
        self.assertIsNotNone(denial)
        self.assertFalse(denial["success"])
        self.assertEqual(denial["error_type"], "RateLimitExceeded")
        self.assertEqual(denial["bucket"], "tool_calls")
        self.assertGreater(denial["retry_after"], 0)

    def test_denied_call_does_not_reach_the_google_api(self):
        """A throttled call must never reach Google."""
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            self._call_many(
                mod.get_ga4_report, 130, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
        self.assertEqual(client.run_report.call_count, 100)

    def test_get_realtime_users_shares_the_tool_calls_bucket(self):
        """get_realtime_users hits a different API method but the same bucket,
        so it must be governed by the same 100-token budget as get_ga4_report.
        """
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        client.run_realtime_report.return_value = _make_realtime_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            first_allowed, first_denied, _ = self._call_many(
                mod.get_ga4_report, 60, dimensions="sessionSource",
                metrics="sessions", property_id="123456789",
            )
            second_allowed, second_denied, _ = self._call_many(
                mod.get_realtime_users, 70, property_id="123456789",
            )
        self.assertEqual((first_allowed, first_denied), (60, 0))
        self.assertEqual(second_allowed, 40)
        self.assertEqual(second_denied, 30)

    def test_delegating_wrapper_consumes_exactly_one_token_per_call(self):
        """get_top_pages carries no gate of its own; it must draw exactly one
        token per call from get_ga4_report's bucket, not zero and not two.
        """
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            allowed, denied, denial = self._call_many(
                mod.get_top_pages, 130, property_id="123456789",
            )
        self.assertEqual(allowed, 100)
        self.assertEqual(denied, 30)
        self.assertEqual(denial["bucket"], "tool_calls")

    def test_denied_delegating_wrapper_call_does_not_reach_the_google_api(self):
        """The delegate's gate must stop the report call before it runs, even
        when reached indirectly through the unwrapped convenience tool.
        """
        mod = _load_module()
        client = MagicMock()
        client.run_report.return_value = _make_report_response()
        with patch.dict(os.environ, {"ENABLE_RATE_LIMITING": "1"}), \
             patch("server._get_client", return_value=client):
            self._call_many(mod.get_top_pages, 130, property_id="123456789")
        self.assertEqual(client.run_report.call_count, 100)


if __name__ == "__main__":
    unittest.main()
