"""
forwarder/test_forwarder.py
===========================
Unit tests for the OTLP Trace Forwarder.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from forwarder.forwarder import OtlpForwarder, build_otlp_payload, count_tokens, decode_otlp_protobuf
from forwarder.lambda_function import lambda_handler


class TestOtlpForwarder(unittest.TestCase):

    def setUp(self):
        self.agent_map = {
            "default": {
                "wxo_agent_id": "default-uuid-1111",
                "wxo_display_name": "Default Agent",
            },
            "support_bot": {
                "wxo_agent_id": "support-uuid-2222",
                "wxo_display_name": "Customer Support Bot",
            },
            "claude-3-7-sonnet": {
                "wxo_agent_id": "claude-uuid-3333",
                "wxo_tenant_id": "tenant-override-123",
            },
        }
        self.forwarder = OtlpForwarder(
            agent_map=self.agent_map,
            default_tenant_id="global-tenant",
            default_export_url="https://api.test/traces",
            default_api_key="test-api-key",
        )

    def test_token_counter(self):
        self.assertEqual(count_tokens("Hello world!"), 3)
        self.assertEqual(count_tokens(""), 0)

    def test_agent_resolution_exact_key(self):
        routing = self.forwarder.resolve_wxo_agent(["support_bot"])
        self.assertEqual(routing["agent_id"], "support-uuid-2222")
        self.assertEqual(routing["display_name"], "Customer Support Bot")
        self.assertEqual(routing["tenant_id"], "global-tenant")

    def test_agent_resolution_per_entry_override(self):
        routing = self.forwarder.resolve_wxo_agent(["claude-3-7-sonnet"])
        self.assertEqual(routing["agent_id"], "claude-uuid-3333")
        self.assertEqual(routing["tenant_id"], "tenant-override-123")

    def test_agent_resolution_direct_uuid(self):
        direct_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        routing = self.forwarder.resolve_wxo_agent([direct_uuid])
        self.assertEqual(routing["agent_id"], direct_uuid)

    def test_agent_resolution_default_fallback(self):
        routing = self.forwarder.resolve_wxo_agent(["unknown-agent"])
        self.assertEqual(routing["agent_id"], "default-uuid-1111")

    def test_build_otlp_payload_root_and_tools(self):
        payload = build_otlp_payload(
            agent_id="test-id",
            agent_name="my_agent",
            tenant_id="my-tenant",
            prompt="Find order status",
            response="Order #1234 is shipped",
            start_ns=1000,
            end_ns=2000,
            tool_calls=[{
                "name": "lookup_order",
                "input": '{"order_id": "1234"}',
                "output": '{"status": "shipped"}',
                "start_ns": 1100,
                "end_ns": 1500,
                "is_error": False,
            }],
        )

        self.assertIn("resourceSpans", payload)
        rs = payload["resourceSpans"][0]
        spans = rs["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 5)
        root = spans[0]
        tool = spans[3]

        self.assertEqual(root["name"], "invoke_agent my_agent")
        self.assertEqual(tool["name"], "lookup_order")
        self.assertEqual(tool["parentSpanId"], spans[2]["spanId"])

    @patch("forwarder.forwarder.urlopen")
    def test_process_payload_json(self, mock_urlopen):
        # Mock IAM Token response and WXO ingest response
        mock_resp_iam = MagicMock()
        mock_resp_iam.read.return_value = json.dumps({"token": "mock-token", "expires_in": 3600}).encode()
        mock_resp_iam.__enter__.return_value = mock_resp_iam

        mock_resp_wxo = MagicMock()
        mock_resp_wxo.status = 200
        mock_resp_wxo.read.return_value = json.dumps({"data": {"id": "JOB-12345"}}).encode()
        mock_resp_wxo.__enter__.return_value = mock_resp_wxo

        mock_urlopen.side_effect = [mock_resp_iam, mock_resp_wxo]

        sample_otlp = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "support_bot"}}]},
                "scopeSpans": [{
                    "spans": [{
                        "traceId": "11112222333344445555666677778888",
                        "spanId": "1234567812345678",
                        "name": "invoke_agent",
                        "startTimeUnixNano": 1000000,
                        "endTimeUnixNano": 2000000,
                        "attributes": [
                            {"key": "gen_ai.prompt", "value": {"stringValue": "Help me reset password"}},
                            {"key": "gen_ai.completion", "value": {"stringValue": "Follow the link sent to your email"}},
                        ],
                    }]
                }]
            }]
        }

        job_ids = self.forwarder.process_payload(
            body_bytes=json.dumps(sample_otlp).encode(),
            content_type="application/json",
        )
        self.assertEqual(job_ids, ["JOB-12345"])

    def test_unmapped_agent_is_not_routed_to_first_entry(self):
        del self.forwarder.agent_map["default"]  # no default => unmapped agents must resolve to nothing
        routing = self.forwarder.resolve_wxo_agent(["harness_unknown-XXXX"])
        self.assertEqual(routing["agent_id"], "")


    def test_aws_runtime_id_routes_to_mapped_wxo_agent(self):
        self.forwarder.agent_map["harness_wxoTracingTest-DEqjmm77sf"] = {"wxo_agent_id": "wxo-y-uuid"}
        sample = {"resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "support_bot"}},  # would match a different entry
                {"key": "aws.agent.runtime.id", "value": {"stringValue": "harness_wxoTracingTest-DEqjmm77sf"}},
            ]},
            "scopeSpans": [{"spans": [{"spanId": "1234567812345678", "name": "invoke_agent"}]}],
        }]}
        with patch.object(self.forwarder, "resolve_wxo_agent", wraps=self.forwarder.resolve_wxo_agent) as spy, \
             patch("forwarder.forwarder.get_bearer_token", return_value=""):
            self.forwarder.process_payload(json.dumps(sample).encode())
        keys = spy.call_args.kwargs["candidate_keys"]
        self.assertEqual(self.forwarder.resolve_wxo_agent(keys)["agent_id"], "wxo-y-uuid")


    def test_explicit_agent_hint_from_path_header_and_query(self):
        body = json.dumps({"resourceSpans": [{"resource": {"attributes": []},
                           "scopeSpans": [{"spans": [{"spanId": "a", "name": "n"}]}]}]}).encode()
        seen = []

        def fake_resolve(candidate_keys=None, default_name=""):
            seen.append(candidate_keys[0])
            return {"agent_id": "", "agent_name": "x", "tenant_id": "", "export_url": "",
                    "api_key": "", "token_url": "", "environment": ""}

        with patch.object(self.forwarder, "resolve_wxo_agent", side_effect=fake_resolve):
            self.forwarder.process_payload(body, path_agent_id="from-path")
            self.forwarder.process_payload(body, headers={"x-wxo-agent-id": "from-header"})
            self.forwarder.process_payload(body, query_params={"agent_id": ["from-query"]})
        self.assertEqual(seen, ["from-path", "from-header", "from-query"])


    def test_auth_token(self):
        self.assertTrue(self.forwarder.is_authorized({}))  # no token configured = open
        self.forwarder.auth_token = "s3cret"
        self.assertTrue(self.forwarder.is_authorized({"Authorization": "Bearer s3cret"}))
        self.assertTrue(self.forwarder.is_authorized({"X-Api-Key": "s3cret"}))
        self.assertFalse(self.forwarder.is_authorized({"Authorization": "Bearer wrong"}))
        self.assertFalse(self.forwarder.is_authorized({}))

    def test_handle_auth_off_by_default(self):
        """No token configured: requests are processed without any auth check."""
        with patch.object(OtlpForwarder, "refresh"), \
             patch.object(OtlpForwarder, "process_payload", return_value=["job-1"]):
            status, body = self.forwarder.handle(b"{}", headers={})
        self.assertEqual((status, body), (200, {"status": "ok", "job_ids": ["job-1"]}))

    def test_handle_auth_on(self):
        """Token configured: wrong/missing -> 401 and nothing is processed; right token -> 200."""
        self.forwarder.auth_token = "s3cret"
        with patch.object(OtlpForwarder, "refresh"), \
             patch.object(OtlpForwarder, "process_payload", return_value=["job-1"]) as proc:
            self.assertEqual(self.forwarder.handle(b"{}", headers={})[0], 401)
            self.assertEqual(self.forwarder.handle(b"{}", headers={"authorization": "Bearer nope"})[0], 401)
            proc.assert_not_called()
            self.assertEqual(self.forwarder.handle(b"{}", headers={"authorization": "Bearer s3cret"})[0], 200)
            self.assertEqual(self.forwarder.handle(b"{}", headers={"x-api-key": "s3cret"})[0], 200)

    def test_handle_returns_500_on_error(self):
        with patch.object(OtlpForwarder, "refresh"), \
             patch.object(OtlpForwarder, "process_payload", side_effect=RuntimeError("boom")):
            status, body = self.forwarder.handle(b"{}")
        self.assertEqual(status, 500)
        self.assertIn("boom", body["error"])

    def test_handle_refreshes_before_processing(self):
        calls = []
        with patch.object(OtlpForwarder, "refresh", side_effect=lambda: calls.append("refresh")), \
             patch.object(OtlpForwarder, "process_payload", side_effect=lambda **k: calls.append("process") or []):
            self.forwarder.handle(b"{}")
        self.assertEqual(calls, ["refresh", "process"])

    def test_lambda_handler_rejects_missing_token(self):
        from forwarder import lambda_function
        with patch.object(lambda_function.forwarder, "refresh"), \
             patch.object(lambda_function.forwarder, "auth_token", "s3cret"):
            res = lambda_handler({"rawPath": "/v1/traces", "body": "{}"}, None)
        self.assertEqual(res["statusCode"], 401)

    def test_refresh_applies_settings(self):
        settings = {"tenant_id": "t-new", "otel_export_url": "https://new/traces",
                    "environment": "live"}
        with patch.object(OtlpForwarder, "get_settings", return_value=settings), \
             patch.object(OtlpForwarder, "get_agent_map", return_value={"a": {"wxo_agent_id": "x"}}):
            self.forwarder.refresh()
        self.assertEqual(self.forwarder.default_tenant_id, "t-new")
        self.assertEqual(self.forwarder.default_environment, "live")
        self.assertIn("a", self.forwarder.agent_map)

    def test_lambda_handler_health(self):
        res = lambda_handler({"rawPath": "/health"}, None)
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "healthy")

    def test_sql_mapping_loading(self):
        import sqlite3
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE wxo_agent_mappings (
                    agent_name VARCHAR(255) PRIMARY KEY,
                    wxo_agent_id VARCHAR(255) NOT NULL,
                    wxo_tenant_id VARCHAR(255),
                    wxo_otel_export_url TEXT,
                    wxo_display_name VARCHAR(255),
                    environment VARCHAR(50)
                );
            """)
            cur.execute("""
                INSERT INTO wxo_agent_mappings VALUES (
                    'sql-bot', 'sql-uuid-999', 'tenant-sql', 'https://api.test/sql/traces', 'SQL Bot', 'live'
                );
            """)
            conn.commit()
            conn.close()

            res = OtlpForwarder._load_from_sql(f"sqlite:///{tmp.name}")
            self.assertIn("sql-bot", res)
            self.assertEqual(res["sql-bot"]["wxo_agent_id"], "sql-uuid-999")
            self.assertEqual(res["sql-bot"]["wxo_tenant_id"], "tenant-sql")


    def _make_anthropic_otlp(self, trace_id, session_turn_span_id,
                             model_request_span_id, input_tokens, output_tokens):
        """Build two-span Anthropic OTLP payloads (one span each, as the OTel
        collector sends them — separate HTTP requests)."""
        model_req_batch = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-platform"}}
                ]},
                "scopeSpans": [{
                    "spans": [{
                        "traceId": trace_id,
                        "spanId": model_request_span_id,
                        "parentSpanId": session_turn_span_id,
                        "name": "anthropic.model_request",
                        "startTimeUnixNano": 1100000,
                        "endTimeUnixNano": 1900000,
                        "attributes": [
                            {"key": "gen_ai.request.model",       "value": {"stringValue": "claude-opus-5-5"}},
                            {"key": "gen_ai.usage.input_tokens",  "value": {"intValue": input_tokens}},
                            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": output_tokens}},
                            {"key": "gen_ai.provider.name",       "value": {"stringValue": "anthropic"}},
                            {"key": "gen_ai.operation.name",      "value": {"stringValue": "chat"}},
                        ],
                    }]
                }]
            }]
        }
        session_turn_batch = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-platform"}}
                ]},
                "scopeSpans": [{
                    "spans": [{
                        "traceId": trace_id,
                        "spanId": session_turn_span_id,
                        "parentSpanId": "733c9f7b452b3eee",  # parent outside this payload
                        "name": "anthropic.session.turn",
                        "startTimeUnixNano": 1000000,
                        "endTimeUnixNano": 2000000,
                        "attributes": [
                            {"key": "gen_ai.agent.id",        "value": {"stringValue": "default"}},
                            {"key": "gen_ai.agent.name",      "value": {"stringValue": "TestAgent"}},
                            {"key": "gen_ai.provider.name",   "value": {"stringValue": "anthropic"}},
                            {"key": "gen_ai.operation.name",  "value": {"stringValue": "invoke_agent"}},
                            {"key": "anthropic.session.id",   "value": {"stringValue": "sesn_test"}},
                            {"key": "gen_ai.input.messages",  "value": {"stringValue": '[{"role":"user","parts":[{"type":"text","content":"hello"}]}]'}},
                            {"key": "gen_ai.output.messages", "value": {"stringValue": '[{"role":"assistant","parts":[{"type":"text","content":"Hi!"}]}]'}},
                        ],
                    }]
                }]
            }]
        }
        return model_req_batch, session_turn_batch

    def test_anthropic_session_turn_no_token_doubling(self):
        """Each Anthropic span arrives in its own HTTP request (one span per batch).
        anthropic.model_request must never produce a WXO trace; its tokens must be
        picked up by the subsequent anthropic.session.turn — no doubling."""
        trace_id = "fc361a0b48bb2825706d381595101e0d"
        session_turn_span_id = "7ab545c4abf700a5"
        model_request_span_id = "ca5ffc8bb87c604c"

        model_req_batch, session_turn_batch = self._make_anthropic_otlp(
            trace_id, session_turn_span_id, model_request_span_id,
            input_tokens=6679, output_tokens=843,
        )

        calls = []

        def fake_post(payload, bearer_token, export_url, agent_id, tenant_id, logger=None):
            calls.append(payload)
            return "JOB-TEST"

        with patch("forwarder.forwarder.get_bearer_token", return_value="mock-token"), \
             patch("forwarder.forwarder.post_to_wxo", side_effect=fake_post):
            # Simulate two separate HTTP requests, as the OTel collector sends them
            ids1 = self.forwarder.process_payload(
                body_bytes=json.dumps(model_req_batch).encode(),
                content_type="application/json",
            )
            ids2 = self.forwarder.process_payload(
                body_bytes=json.dumps(session_turn_batch).encode(),
                content_type="application/json",
            )

        # model_request must produce no WXO trace
        self.assertEqual(ids1, [], "anthropic.model_request must not be forwarded")
        # session.turn must produce exactly one WXO trace
        self.assertEqual(len(ids2), 1, "Expected exactly one forwarded trace from session.turn")
        self.assertEqual(len(calls), 1, "post_to_wxo must be called exactly once total")

        # Token counts must not be doubled
        fwd_spans = calls[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        root_span = fwd_spans[0]
        attrs = {a["key"]: (a["value"].get("intValue") or a["value"].get("stringValue") or 0)
                 for a in root_span.get("attributes", [])}
        self.assertEqual(attrs.get("gen_ai.usage.input_tokens"), 6679)
        self.assertEqual(attrs.get("gen_ai.usage.output_tokens"), 843)

    def test_anthropic_model_request_alone_never_forwarded(self):
        """anthropic.model_request arriving with no prior session.turn in the same
        payload must never be forwarded as a WXO trace on its own."""
        trace_id = "aabbccddeeff00112233445566778899"
        model_req_batch, _ = self._make_anthropic_otlp(
            trace_id, "parentXXX", "childYYY",
            input_tokens=100, output_tokens=50,
        )

        calls = []
        with patch("forwarder.forwarder.get_bearer_token", return_value="mock-token"), \
             patch("forwarder.forwarder.post_to_wxo", side_effect=lambda **kw: calls.append(kw) or "J"):
            ids = self.forwarder.process_payload(
                body_bytes=json.dumps(model_req_batch).encode(),
                content_type="application/json",
            )

        self.assertEqual(ids, [], "anthropic.model_request must never be forwarded standalone")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
