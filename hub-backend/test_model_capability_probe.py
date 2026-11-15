"""Bounded protocol regressions with synthetic responses and no external inference."""
import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import model_capabilities as caps, model_capability_probe as probe
from app.services.model_errors import UpstreamError


CONF = {"base": "https://fixture.invalid/v1", "key": "synthetic", "model": "opaque",
        "kind": "openai-compat", "config": {"maxOutputTokens": 4096}}


class Response:
    def __init__(self, data, *, content_type="application/json", chunks=None):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.parts = chunks if chunks is not None else [self.data]
        self.headers = {"content-type": content_type}
        self.closed = False

    async def read(self, cap):
        if len(self.data) > cap:
            raise ValueError("bounded fixture read")
        return self.data

    async def chunks(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        self.closed = True


def message(content="OK", **extra):
    return Response({"choices": [{"finish_reason": "stop", "message": {"content": content, **extra}}]})


def streamed_tool(value, *, name="soduwork_capability_echo", arguments=None, extra=False,
                  finish="tool_calls", done=True, error=False, reasoning=False):
    arguments = json.dumps({"value": value}) if arguments is None else arguments
    encoded = lambda delta, reason=None: "data: " + json.dumps({"choices": [
        {"index": 0, "delta": delta, "finish_reason": reason}]}) + "\r\n\r\n"
    split = len(arguments) // 2
    data = ": heartbeat\r\n\r\n"
    if reasoning:
        data += encoded({"reasoning_content": "synthetic thought"})
    data += encoded({"tool_calls": [{"index": 0, "id": "call-", "type": "function", "function": {
        "name": name[:8], "arguments": arguments[:split]}}]})
    data += encoded({"tool_calls": [{"index": 0, "id": "fixture", "function": {
        "name": name[8:], "arguments": arguments[split:]}}]})
    if extra:
        data += encoded({"tool_calls": [{"index": 1, "id": "other-call", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"value": value})}}]})
    if finish is not None:
        data += encoded({}, finish)
    if error:
        data += 'event: error\r\ndata: {"error":{"message":"private fixture error"}}\r\n\r\n'
    if done:
        data += "data: [DONE]\r\n\r\n"
    raw = data.encode()
    return Response(raw, content_type="text/event-stream", chunks=[raw[index:index + 3] for index in range(0, len(raw), 3)])


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, checks, handler, conf=None):
        self.requests, self.responses = [], []
        async def opened(target, path, encoded, **kwargs):
            body = json.loads(encoded)
            self.requests.append(body)
            response = handler(body)
            if asyncio.iscoroutine(response):
                response = await response
            self.responses.append(response)
            return response
        with patch.object(probe.model_transport, "open_response", side_effect=opened):
            result = await probe.run(conf or CONF, checks)
        self.assertTrue(all(response.closed for response in self.responses))
        return result

    async def test_tool_choice_rejection_negotiates_auto_and_confirms_synthetic_result(self):
        def handler(body):
            if isinstance(body.get("tool_choice"), dict):
                raise UpstreamError(400, json.dumps({"error": {"code": "unsupported_parameter",
                    "param": "tool_choice", "message": "named tool_choice is not supported"}}))
            if body["messages"][-1]["role"] == "tool":
                receipt = json.loads(body["messages"][-1]["content"])["receipt"]
                return message(receipt)
            value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
            return streamed_tool(value)
        result = await self.run_probe(["toolCalling"], handler)
        self.assertEqual(result["checks"]["toolCalling"]["status"], "passed")
        self.assertEqual(result["compatibility"]["toolChoice"], "auto")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1]["messages"][-1]["role"], "tool")
        self.assertEqual(result["reportVersion"], 2)
        self.assertEqual(result["apiFamily"], "chat-completions")

    async def test_json_tool_support_does_not_prove_streamed_tool_support(self):
        def handler(body):
            if body.get("stream") and body.get("tools"):
                raise UpstreamError(400, '{"error":{"message":"streamed tools request not accepted"}}')
            if body["messages"][-1]["role"] == "tool":
                return message(json.loads(body["messages"][-1]["content"])["receipt"])
            value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
            return message(None, tool_calls=[{"id": "json-only", "type": "function", "function": {
                "name": "soduwork_capability_echo", "arguments": json.dumps({"value": value})}}])
        result = await self.run_probe(["toolCalling"], handler)
        self.assertTrue(self.requests[0]["stream"])
        self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")
        self.assertEqual(len(self.requests), 1)

    async def test_streamed_tool_fragments_assemble_id_name_arguments_and_safe_receipt(self):
        def handler(body):
            if body["messages"][-1]["role"] == "tool":
                return message(json.loads(body["messages"][-1]["content"])["receipt"])
            value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
            return streamed_tool(value, reasoning=True)
        result = await self.run_probe(["toolCalling"], handler)
        self.assertEqual(result["checks"]["toolCalling"]["status"], "passed")
        self.assertTrue(result["reasoningStreamReported"])
        self.assertTrue(self.requests[0]["stream"])
        self.assertFalse(self.requests[-1]["stream"])
        replay = self.requests[-1]["messages"][-2]
        self.assertEqual(replay["tool_calls"][0]["id"], "call-fixture")
        self.assertEqual(replay["tool_calls"][0]["function"]["name"], "soduwork_capability_echo")
        self.assertEqual(replay["reasoning_content"], "synthetic thought")
        self.assertEqual(len(self.requests), 2)

    async def test_streamed_tool_wrong_multiple_and_incomplete_calls_do_not_receive_results(self):
        fixtures = [{"name": "unrequested_function"}, {"extra": True}, {"arguments": '{"value":'},
                    {"arguments": '{"value":"wrong"}'}, {"finish": "stop"}, {"finish": None}]
        for options in fixtures:
            def handler(body):
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                return streamed_tool(value, **options)
            result = await self.run_probe(["toolCalling"], handler)
            self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")
            self.assertEqual(len(self.requests), 1)

    async def test_streamed_tool_truncation_in_stream_error_and_byte_limit_are_inconclusive(self):
        fixtures = [({"finish": "length"}, "response_truncated"), ({"error": True}, "stream_error"),
                    ({"finish": None, "done": False}, "stream_incomplete")]
        for options, code in fixtures:
            def handler(body):
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                return streamed_tool(value, **options)
            result = await self.run_probe(["toolCalling"], handler)
            self.assertEqual(result["checks"]["toolCalling"]["code"], code)
            self.assertEqual(len(self.requests), 1)
        with patch.object(probe, "MAX_BYTES", 32):
            result = await self.run_probe(["toolCalling"], lambda body: streamed_tool("ignored"))
        self.assertEqual(result["checks"]["toolCalling"]["code"], "response_limit")

    async def test_streamed_tool_requires_one_zero_index_call_and_stop_receipt(self):
        for index in (1, -1, True, 16):
            def handler(body):
                if body["messages"][-1]["role"] == "tool":
                    return message(json.loads(body["messages"][-1]["content"])["receipt"])
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                data = streamed_tool(value).data.replace(b'"index": 0, "id"',
                    ('"index": ' + json.dumps(index) + ', "id"').encode())
                return Response(data, content_type="text/event-stream")
            result = await self.run_probe(["toolCalling"], handler)
            self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")
            self.assertEqual(len(self.requests), 1)
        for finish in (None, "tool_calls"):
            def handler(body):
                if body["messages"][-1]["role"] == "tool":
                    receipt = json.loads(body["messages"][-1]["content"])["receipt"]
                    return Response({"choices": [{"message": {"content": receipt}, "finish_reason": finish}]})
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                return streamed_tool(value)
            result = await self.run_probe(["toolCalling"], handler)
            self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")
            self.assertEqual(len(self.requests), 2)

    async def test_streamed_tool_timeout_never_sends_receipt_or_publishes_success(self):
        class DeadlineResponse(Response):
            async def chunks(self):
                yield self.data
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    return
        def handler(body):
            value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
            return DeadlineResponse(streamed_tool(value).data, content_type="text/event-stream")
        with patch.object(probe, "TIMEOUT", .01):
            result = await self.run_probe(["toolCalling"], handler)
        self.assertEqual(result["checks"]["toolCalling"]["code"], "timeout")
        self.assertEqual(len(self.requests), 1)

    async def test_complete_run_with_streamed_tools_and_two_parameter_retries_uses_seven_calls(self):
        def handler(body):
            if isinstance(body.get("tool_choice"), dict):
                raise UpstreamError(400, '{"error":{"code":"unsupported_parameter","param":"tool_choice"}}')
            if body.get("stream_options"):
                raise UpstreamError(400, '{"error":{"code":"unsupported_parameter","param":"stream_options"}}')
            if body["messages"][-1]["role"] == "tool":
                return message(json.loads(body["messages"][-1]["content"])["receipt"])
            if body.get("tools"):
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                return streamed_tool(value)
            if body.get("stream"):
                return Response(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n',
                                content_type="text/event-stream")
            return message("123456" if isinstance(body["messages"][0]["content"], list) else "OK")
        with patch.object(probe, "image_challenge", return_value=("123456", "data:image/png;base64,fixture")):
            result = await self.run_probe(list(probe.CHECKS), handler)
        self.assertTrue(all(check["status"] == "passed" for check in result["checks"].values()), result)
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(result["compatibility"]["toolChoice"], "auto")
        self.assertIs(result["compatibility"]["streamOptions"], False)

    async def test_stream_options_rejection_is_not_streaming_unsupported(self):
        def handler(body):
            if "stream_options" in body:
                raise UpstreamError(400, json.dumps({"error": {"code": "unsupported_parameter",
                    "param": "stream_options", "message": "stream_options is not supported"}}))
            return Response(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n',
                            content_type="text/event-stream")
        result = await self.run_probe(["streaming"], handler)
        self.assertEqual(result["checks"]["streaming"]["status"], "passed")
        self.assertIs(result["compatibility"]["streamOptions"], False)

    async def test_text_only_parameter_refusal_does_not_mark_feature_unsupported(self):
        for check, text in (("toolCalling", "tool_choice is unsupported"),
                            ("streaming", "stream_options is not supported")):
            self.assertEqual(probe._failure(UpstreamError(400, text), check)[0], "inconclusive")

    async def test_generic_bad_request_never_negotiates_or_claims_unsupported(self):
        def reject(body):
            raise UpstreamError(400, '{"error":{"message":"invalid model request"}}')
        result = await self.run_probe(["streaming"], reject)
        self.assertEqual(result["checks"]["streaming"]["status"], "inconclusive")
        self.assertEqual(len(self.requests), 1)

    async def test_saved_thinking_output_and_working_compatibility_shape_requests(self):
        conf = {**CONF, "config": {"thinkingMode": "always", "reasoning": True,
            "thinkingFormat": "openai", "supportsReasoningEffort": True,
            "thinkingLevelMap": {"off": None, "low": "low"}, "maxOutputTokens": 1024},
            "compatibility": {"maxTokensField": "max_completion_tokens", "streamOptions": False}}
        result = await self.run_probe(["connection"], lambda body: message(), conf)
        self.assertEqual(result["checks"]["connection"]["status"], "passed")
        self.assertEqual(self.requests[0].get("reasoning_effort"), "low")
        self.assertNotIn("max_tokens", self.requests[0])
        self.assertLessEqual(self.requests[0]["max_completion_tokens"], 1024)

    async def test_output_parameter_failure_can_negotiate_only_once_per_variant(self):
        def handler(body):
            if "max_tokens" in body:
                raise UpstreamError(400, json.dumps({"error": {"code": "unsupported_parameter",
                    "param": "max_tokens", "message": "Use max_completion_tokens instead"}}))
            return message()
        result = await self.run_probe(["connection"], handler)
        self.assertEqual(result["checks"]["connection"]["status"], "passed")
        self.assertEqual(result["compatibility"]["maxTokensField"], "max_completion_tokens")
        self.assertEqual(len(self.requests), 2)

    async def test_two_parameter_negotiation_limit_is_global_to_run(self):
        def reject(body):
            param = "max_tokens" if "max_tokens" in body else "max_completion_tokens"
            raise UpstreamError(400, json.dumps({"error": {"code": "unsupported_parameter", "param": param}}))
        result = await self.run_probe(list(probe.CHECKS), reject)
        self.assertLessEqual(len(self.requests), 6)
        self.assertTrue(all(item["status"] == "inconclusive" for item in result["checks"].values()))

    async def test_stream_decodes_split_utf8_multiline_data_crlf_and_heartbeats(self):
        data = (': heartbeat\r\nevent: message\r\ndata: {"choices":[\r\n'
                'data: {"delta":{"content":"你好"},"finish_reason":null}]}\r\n\r\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\r').encode()
        response = Response(data, content_type="text/event-stream", chunks=[bytes([byte]) for byte in data])
        result = await self.run_probe(["streaming"], lambda body: response)
        self.assertEqual(result["checks"]["streaming"]["status"], "passed")

    async def test_stream_needs_terminal_evidence_and_nonempty_meaningful_delta(self):
        fixtures = [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"  "},"finish_reason":"stop"}]}\n\n',
                    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n']
        for data in fixtures:
            result = await self.run_probe(["streaming"], lambda body: Response(data, content_type="text/event-stream"))
            self.assertEqual(result["checks"]["streaming"]["status"], "inconclusive")

    async def test_stream_error_truncation_invalid_utf8_and_limit_never_pass(self):
        start = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        fixtures = [(start + b'event: error\ndata: {"error":{"message":"private fixture"}}\n\n', "stream_error"),
                    (start + b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n', "response_truncated"),
                    (start + b'\xff\n\ndata: [DONE]\n\n', "invalid_response"),
                    (start + b': ' + b'x' * probe.MAX_BYTES, "response_limit")]
        for data, code in fixtures:
            result = await self.run_probe(["streaming"], lambda body: Response(data, content_type="text/event-stream"))
            self.assertEqual(result["checks"]["streaming"]["code"], code)
            self.assertEqual(result["checks"]["streaming"]["status"], "inconclusive")

    async def test_json_response_to_stream_request_is_not_stream_verification(self):
        result = await self.run_probe(["streaming"], lambda body: message())
        self.assertEqual(result["checks"]["streaming"]["code"], "stream_not_observed")

    async def test_named_sse_heartbeats_do_not_count_as_model_deltas(self):
        data = (b'event: ping\ndata: {"type":"ping"}\n\n'
                b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
        result = await self.run_probe(["streaming"], lambda body: Response(data, content_type="text/event-stream"))
        self.assertEqual(result["checks"]["streaming"]["status"], "passed")

    async def test_delta_after_finish_is_an_invalid_stream(self):
        data = (b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"orphan partial"}}]}\n\n')
        result = await self.run_probe(["streaming"], lambda body: Response(data, content_type="text/event-stream"))
        self.assertEqual(result["checks"]["streaming"]["code"], "invalid_response")

    async def test_tool_arguments_wrong_type_extra_fields_and_missing_receipt_are_inconclusive(self):
        for mutation in (lambda value: {"value": [value]}, lambda value: {"value": value, "execute": True},
                         lambda value: {"value": value}):
            def handler(body):
                if body["messages"][-1]["role"] == "tool":
                    return message("ignored tool result")
                value = re.search(r"value ([a-f0-9]+)", body["messages"][0]["content"])[1]
                return streamed_tool(value, arguments=json.dumps(mutation(value)))
            result = await self.run_probe(["toolCalling"], handler)
            self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")

    async def test_non_chat_completions_protocol_is_explicitly_not_tested(self):
        result = await self.run_probe(["connection"], lambda body: self.fail("must not call upstream"),
                                      {**CONF, "kind": "anthropic"})
        self.assertEqual(result["checks"]["connection"]["code"], "protocol_not_tested")

    async def test_controllable_and_baseten_thinking_follow_saved_dialect(self):
        fixtures = [("controllable", "qwen", "enable_thinking", False),
                    ("always", "baseten", "chat_template_kwargs", {"enable_thinking": True}),
                    ("always", "zai", "thinking", {"type": "enabled", "clear_thinking": False})]
        for mode, dialect, field, value in fixtures:
            conf = {**CONF, "config": {"thinkingMode": mode, "thinkingFormat": dialect, "reasoning": True}}
            await self.run_probe(["connection"], lambda body: message(), conf)
            self.assertEqual(self.requests[0].get(field), value)

    async def test_always_thinking_budget_has_headroom_and_obeys_saved_context(self):
        conf = {**CONF, "contextWindow": 1536, "config": {"thinkingMode": "always", "maxOutputTokens": 4096}}
        await self.run_probe(["connection"], lambda body: message(), conf)
        self.assertEqual(self.requests[0]["max_tokens"], 1536)

    async def test_run_deadline_bounds_all_requests_not_only_individual_idle_time(self):
        async def slow(body):
            await asyncio.sleep(1)
            return message()
        with patch.object(probe, "TOTAL_TIMEOUT", .04), patch.object(probe, "TIMEOUT", .03):
            result = await self.run_probe(list(probe.CHECKS), slow)
        self.assertLessEqual(len(self.requests), 2)
        self.assertEqual(result["checks"]["streaming"]["code"], "budget_exhausted")

    async def test_transport_suppressing_timeout_cannot_publish_stream_success(self):
        class DeadlineResponse(Response):
            async def chunks(self):
                yield self.data
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    return
        data = b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
        with patch.object(probe, "TIMEOUT", .01):
            result = await self.run_probe(["streaming"], lambda body: DeadlineResponse(data, content_type="text/event-stream"))
        self.assertEqual(result["checks"]["streaming"]["status"], "inconclusive")
        self.assertEqual(result["checks"]["streaming"]["code"], "timeout")

    async def test_billing_and_known_structured_capability_refusals_are_safe(self):
        for check, code in (("imageInput", "unsupported_image_input"),
                            ("toolCalling", "unsupported_tools"), ("streaming", "streaming_not_supported")):
            result = probe._failure(UpstreamError(400, json.dumps({"error": {"code": code,
                "message": "private fixture text"}})), check)
            self.assertEqual(result, ("unsupported", "capability_not_supported"))
        self.assertEqual(probe._failure(UpstreamError(400, '{"error":{"code":"insufficient_quota"}}'),
                                       "imageInput"), ("failed", "billing_unavailable"))


class EvidenceTests(unittest.TestCase):
    def test_legacy_tool_and_stream_passes_require_recheck_but_preserve_other_evidence_and_declarations(self):
        legacy = {"reportVersion": 1, "checks": {
            "toolCalling": {"status": "passed", "checkedAt": "old"},
            "streaming": {"status": "passed", "checkedAt": "old"},
            "imageInput": {"status": "passed", "checkedAt": "old"},
            "connection": {"status": "passed", "checkedAt": "old"}}}
        profile = caps.profile({}, legacy)
        self.assertEqual(profile["toolCalling"]["state"], "unknown")
        self.assertEqual(profile["toolCalling"]["checkStatus"], "inconclusive")
        self.assertEqual(caps.public_evidence(legacy)["checks"]["toolCalling"]["code"], "legacy_recheck_required")
        self.assertEqual(profile["streaming"]["state"], "unknown")
        self.assertEqual(caps.public_evidence(legacy)["checks"]["streaming"]["code"], "legacy_recheck_required")
        self.assertEqual(legacy["checks"]["toolCalling"]["status"], "passed")
        self.assertEqual(profile["imageInput"]["state"], "supported")
        self.assertEqual(caps.evidence_checks(legacy)["connection"]["status"], "passed")
        declared = caps.profile({"capabilities": {"toolCalling": "supported"}}, legacy)
        self.assertEqual(declared["toolCalling"]["source"], "configured")
        unsupported = {"reportVersion": 1, "checks": {"toolCalling": {
            "status": "unsupported", "code": "capability_not_supported", "checkedAt": "old"}}}
        self.assertEqual(caps.profile({}, unsupported)["toolCalling"]["state"], "unknown")
        self.assertEqual(caps.profile({}, {**unsupported, "reportVersion": 2})["toolCalling"]["state"], "unsupported")

    def test_connection_only_v2_merge_cannot_promote_legacy_json_tool_pass(self):
        legacy = {"reportVersion": 1, "configRevision": "same", "checks": {
            "toolCalling": {"status": "passed", "checkedAt": "old"},
            "imageInput": {"status": "passed", "checkedAt": "old"}}}
        result = caps.merge_evidence(legacy, {"reportVersion": 2, "apiFamily": "chat-completions",
            "configRevision": "same", "checks": {"connection": {"status": "passed", "checkedAt": "new"}}})
        self.assertEqual(result["checks"]["toolCalling"]["status"], "inconclusive")
        self.assertNotIn("toolCalling", result["verifiedChecks"])
        self.assertEqual(result["verifiedChecks"]["imageInput"]["status"], "passed")
        self.assertEqual(caps.profile({}, result)["toolCalling"]["state"], "unknown")

    def test_v1_optional_control_false_negatives_cannot_block_or_be_promoted_by_new_merge(self):
        legacy = {"reportVersion": 1, "configRevision": "same", "checks": {
            key: {"status": "unsupported", "code": "capability_not_supported", "checkedAt": "old"}
            for key in ("toolCalling", "streaming")}}
        result = caps.merge_evidence(legacy, {"reportVersion": 2, "apiFamily": "chat-completions",
            "configRevision": "same", "checks": {"connection": {"status": "passed", "checkedAt": "new"}}})
        for key in ("toolCalling", "streaming"):
            self.assertEqual(result["checks"][key]["status"], "inconclusive")
            self.assertEqual(result["checks"][key]["code"], "legacy_recheck_required")
            self.assertNotIn(key, result["verifiedChecks"])
            self.assertEqual(caps.profile({}, result)[key]["state"], "unknown")

    def test_shared_desktop_and_hub_capability_vectors(self):
        fixture = Path(__file__).resolve().parents[1] / "tools/contracts/model-capabilities-v2.json"
        for case in json.loads(fixture.read_text(encoding="utf-8"))["cases"]:
            with self.subTest(case["name"]):
                actual = caps.runtime_fields(case["config"], case["evidence"])
                for key, expected in case["expected"]["imageInput"].items():
                    self.assertEqual(actual["capabilityProfile"]["imageInput"].get(key), expected)
                self.assertEqual(actual["imageRouting"], case["expected"]["imageRouting"])
                self.assertEqual(actual.get("input"), case["expected"]["input"])

    def test_malformed_evidence_does_not_break_catalog_projection(self):
        for evidence in ({"checks": [], "verifiedChecks": [], "compatibility": None},
                         {"checks": [1], "verifiedChecks": [1], "compatibility": []},
                         {"checks": {"imageInput": "passed"}},
                         {"verifiedChecks": {"streaming": None}}):
            try:
                result = caps.runtime_fields({}, evidence)
            except (TypeError, AttributeError) as error:
                self.fail("malformed stored evidence broke projection: " + type(error).__name__)
            self.assertEqual(result["capabilityProfile"]["imageInput"]["state"], "unknown")

    def test_transient_latest_check_retains_last_conclusive_capability(self):
        evidence = {"reportVersion": 2, "apiFamily": "chat-completions",
            "checks": {"imageInput": {"status": "inconclusive", "checkedAt": "later"}},
            "verifiedChecks": {"imageInput": {"status": "passed", "checkedAt": "earlier"}}}
        fields = caps.runtime_fields({}, evidence)
        self.assertEqual(fields["capabilityProfile"]["imageInput"]["state"], "supported")
        self.assertEqual(fields["capabilityProfile"]["imageInput"]["checkStatus"], "inconclusive")
        self.assertEqual(fields["imageRouting"], "native")

    def test_runtime_revision_ignores_probe_dates_and_diagnostics(self):
        one = caps.runtime_fields({}, {"checks": {"imageInput": {"status": "passed", "checkedAt": "one"}}})
        two = caps.runtime_fields({}, {"checks": {"imageInput": {"status": "passed", "checkedAt": "two", "code": "verified"}}})
        self.assertEqual(caps.runtime_revision("target", one), caps.runtime_revision("target", two))

    def test_manual_declaration_wins_but_conflicting_verified_evidence_is_visible(self):
        profile = caps.profile({"capabilities": {"toolCalling": "supported"}},
            {"reportVersion": 2, "checks": {"toolCalling": {"status": "unsupported", "checkedAt": "now"}}})
        self.assertEqual(profile["toolCalling"]["source"], "configured")
        self.assertTrue(profile["toolCalling"].get("conflict"))

    def test_other_protocol_evidence_cannot_enable_chat_completions_runtime(self):
        fields = caps.runtime_fields({}, {"apiFamily": "responses", "reasoningStreamReported": True,
            "checks": {"imageInput": {"status": "passed"}}, "compatibility": {"streamOptions": False}})
        self.assertEqual(fields["capabilityProfile"]["imageInput"]["state"], "unknown")
        self.assertEqual(fields["capabilityProfile"]["reasoningStream"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
