"""Native provider contracts, independent of paid services or installed CLIs."""
import io
import json
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from app.services import harness2_provider as provider, harness2_streaming as streaming
from app.services.harness2_errors import StructuredFailure, failure_message


MODEL = {'base': 'https://fixture.invalid/v1', 'key': 'private-key', 'model': 'fixture'}
MESSAGES = [{'role': 'system', 'content': 'Use current permissions'}, {'role': 'user', 'content': 'Test'}]


class Response(io.BytesIO):
    def __init__(self, payload, content_type='application/json'):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.headers = {'Content-Type': content_type}


def event(value):
    return 'data: ' + json.dumps(value) + '\n\n'


class NativeProviderTests(unittest.TestCase):
    def test_native_long_stream_uses_a_direct_route_and_bounded_idle_timeout(self):
        from app.services.harness2_native import Peer

        now = 0.0
        deadlines = []
        packets = iter([
            (60.0, b': keep-alive\n\n'),
            (130.0, event({'choices': [{'delta': {'reasoning_content': 'thinking'}}]}).encode()),
            (150.0, (event({'choices': [{'finish_reason': 'stop', 'delta': {'content': 'done'}}]})
                     + 'data: [DONE]\n\n').encode()),
        ])

        class SlowResponse(Response):
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=deadlines.append)))

            def read1(self, _size):
                nonlocal now
                now, chunk = next(packets)
                return chunk

        peer = Peer(MODEL, None, {}, output=lambda _event: None)
        peer.session = SimpleNamespace(id='fixture')
        urllib = provider.urllib.request
        with patch('time.monotonic', side_effect=lambda: now), patch.object(
                urllib, 'build_opener', wraps=urllib.build_opener) as opener, patch.object(
                urllib.OpenerDirector, 'open', return_value=SlowResponse(b'', 'text/event-stream')) as transport:
            message, _ = peer._inference(MESSAGES)
        self.assertEqual(message['content'], 'done')
        self.assertEqual(opener.call_args.args[0].proxies, {})
        self.assertEqual(transport.call_args.kwargs['timeout'], 120.0)
        self.assertEqual(deadlines, [120.0, 120.0, 120.0])

    def test_known_unsupported_streaming_rejects_before_contacting_network(self):
        payload = {'choices': [{'message': {'content': 'verified'}, 'finish_reason': 'stop'}]}
        model = {**MODEL, 'capabilityProfile': {'streaming': {'state': 'unsupported'}}}
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)) as transport:
            with self.assertRaises(StructuredFailure) as caught:
                provider.request(model, MESSAGES, [])
        transport.assert_not_called()
        self.assertEqual(caught.exception.code, 'model_streaming_unsupported')
        diagnostic = failure_message(caught.exception, 'session/prompt')
        self.assertIn('不支持流式输出', diagnostic)
        self.assertNotIn(MODEL['key'], diagnostic)

    def test_known_unsupported_required_tools_rejects_before_contacting_network(self):
        payload = {'choices': [{'message': {'content': 'verified'}, 'finish_reason': 'stop'}]}
        model = {**MODEL, 'capabilityProfile': {'toolCalling': {'state': 'unsupported'}}}
        tools = [{'name': 'fixture_tool', 'parameters': {'type': 'object', 'properties': {}}}]
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)) as transport:
            with self.assertRaises(StructuredFailure) as caught:
                provider.request(model, MESSAGES, tools)
        transport.assert_not_called()
        self.assertEqual(caught.exception.code, 'model_tools_unsupported')
        self.assertIn('不支持工具调用', failure_message(caught.exception, 'session/prompt'))

    def test_unknown_capabilities_and_tool_free_planning_remain_usable(self):
        payload = {'choices': [{'message': {'content': 'verified'}, 'finish_reason': 'stop'}]}
        cases = [({'streaming': {'state': 'unknown'}, 'toolCalling': {'state': 'unknown'}},
                  [{'name': 'fixture_tool', 'parameters': {'type': 'object', 'properties': {}}}], False),
                 ({'streaming': {'state': 'supported'}, 'toolCalling': {'state': 'unsupported'}}, [], True)]
        for profile, tools, planning in cases:
            with self.subTest(profile=profile), patch.object(provider.urllib.request.OpenerDirector, 'open',
                    return_value=Response(payload)) as transport:
                message, _ = provider.request({**MODEL, 'capabilityProfile': profile}, MESSAGES, tools, planning=planning)
                self.assertEqual(message['content'], 'verified')
                transport.assert_called_once()

    def test_keyless_local_model_sends_no_authentication_header(self):
        payload = {'choices': [{'message': {'content': 'verified'}, 'finish_reason': 'stop'}]}
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)) as transport:
            provider.request({**MODEL, 'key': '', 'localOnly': True}, MESSAGES, [])
        self.assertIsNone(transport.call_args.args[0].get_header('Authorization'))

    def test_provider_usage_keeps_cache_and_reasoning_without_double_counting(self):
        tokens = provider.normalize_usage({'prompt_tokens': 100, 'completion_tokens': 20,
            'prompt_tokens_details': {'cached_tokens': 80},
            'completion_tokens_details': {'reasoning_tokens': 12}})
        self.assertEqual((tokens['input'], tokens['output'], tokens['cacheRead'], tokens['reasoning']),
                         (100, 20, 80, 12))
        self.assertEqual(tokens['totalTokens'], 120)
        anthropic = provider.normalize_usage({'input_tokens': 20, 'output_tokens': 5,
            'cache_read_input_tokens': 80, 'cache_creation_input_tokens': 10})
        self.assertEqual((anthropic['input'], anthropic['cacheRead'], anthropic['cacheWrite']), (110, 80, 10))
        self.assertEqual(anthropic['totalTokens'], 115)
        self.assertEqual(provider.normalize_usage({'prompt_tokens': 4, 'prompt_tokens_details': None})['input'], 4)

    def test_refinement_and_execution_keep_the_same_provider_usage_details(self):
        from app.services import harness2_bridge
        payload = {'choices': [{'message': {'content': 'verified'}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
                             'prompt_tokens_details': {'cached_tokens': 80},
                             'completion_tokens_details': {'reasoning_tokens': 12}}}
        observed = []
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)):
            self.assertEqual(harness2_bridge.complete(MODEL, MESSAGES, observed.append), 'verified')
        self.assertEqual(observed, [{'input_tokens': 100, 'output_tokens': 20,
                                    'cacheRead': 80, 'inputIncludesCache': True, 'reasoning': 12}])

    def test_truncated_response_preserves_real_usage_without_false_completion(self):
        value = {'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}],
                 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}
        usages = []
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(value)):
            with self.assertRaises(StructuredFailure) as caught:
                provider.request(MODEL, MESSAGES, [], on_usage=usages.append)
        self.assertEqual(caught.exception.code, 'response_truncated')
        self.assertEqual(len(usages), 1)
        self.assertEqual((usages[0]['input'], usages[0]['output']), (11, 7))

    def test_only_explicit_tool_free_planning_limit_allows_recovery(self):
        from app.services.harness2_errors import PlanningOutputLimit
        from app.services.harness2_budget import RefinementBudget
        call = {'id': 'partial', 'type': 'function',
                'function': {'name': 'write_file', 'arguments': '{'}}
        for planning, calls, recoverable in [(True, [], True), (False, [], False), (True, [call], False)]:
            payload = {'choices': [{'message': {'content': 'partial', 'tool_calls': calls},
                                    'finish_reason': 'length'}]}
            with self.subTest(planning=planning, calls=calls), patch.object(
                    provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)):
                with self.assertRaises(StructuredFailure) as caught:
                    provider.request(MODEL, MESSAGES, [], planning=planning)
                self.assertEqual(isinstance(caught.exception, PlanningOutputLimit), recoverable)
        anthropic = {'content': [{'type': 'text', 'text': 'partial'}], 'stop_reason': 'max_tokens'}
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(anthropic)):
            with self.assertRaises(PlanningOutputLimit):
                provider.request({**MODEL, 'kind': 'anthropic'}, MESSAGES, [], planning=True)
        # EOF without the terminal event has no reliable token-limit proof.
        raw = event({'choices': [{'delta': {'content': 'partial'}, 'finish_reason': 'length'}],
                     'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}).encode()
        from app.services import harness2_bridge
        usage = []
        with patch.object(provider.urllib.request.OpenerDirector, 'open',
                          return_value=Response(raw, 'text/event-stream')) as transport:
            budget = RefinementBudget()
            with self.assertRaises(StructuredFailure) as caught:
                budget.run(MODEL, MESSAGES, harness2_bridge.complete, usage.append)
            self.assertNotIsInstance(caught.exception, PlanningOutputLimit)
            self.assertEqual(caught.exception.code, 'response_truncated')
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(budget.retries, 0)
            self.assertEqual(usage, [{'input_tokens': 3, 'output_tokens': 2}])

    def test_broken_stream_keeps_reported_usage_but_never_completes(self):
        raw = event({'choices': [{'delta': {'content': 'partial'}, 'finish_reason': None}],
                     'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})
        usages, texts = [], []
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(raw.encode(), 'text/event-stream')):
            with self.assertRaises(StructuredFailure) as caught:
                provider.request(MODEL, MESSAGES, [], on_usage=usages.append, on_text=texts.append)
        self.assertEqual(caught.exception.code, 'response_truncated')
        self.assertEqual(texts, ['partial'])
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]['output'], 2)

    def test_stream_with_large_framing_overhead_preserves_a_complete_tool_call(self):
        arguments = json.dumps({'path': 'art.html', 'content': '<span>test</span>\n' * 2300})
        fragments = [arguments[i:i + 8] for i in range(0, len(arguments), 8)]
        rows = []
        for i, fragment in enumerate(fragments):
            part = {'index': 0, 'function': {'arguments': fragment}}
            if i == 0:
                part.update(id='write-art', type='function')
                part['function']['name'] = 'write_file'
            rows.append({'id': 'chatcmpl-' + 'x' * 64, 'object': 'chat.completion.chunk',
                         'created': 1794708000, 'model': 'deepseek-flash',
                         'system_fingerprint': 'fp_' + 'a' * 64, 'usage': None,
                         'choices': [{'index': 0, 'finish_reason': None, 'delta': {
                             'content': None, 'reasoning_content': None, 'tool_calls': [part]}}]})
        rows.extend([{'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'delta': {}}]},
                     {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 6500}}])
        raw = (''.join(event(row) for row in rows) + 'data: [DONE]\n\n').encode()
        self.assertGreater(len(raw), 2_000_000)
        with patch.object(provider.urllib.request.OpenerDirector, 'open',
                          return_value=Response(raw, 'text/event-stream')):
            message, tokens = provider.request(MODEL, MESSAGES, [])
        self.assertEqual(message['tool_calls'][0]['function']['arguments'], arguments)
        self.assertEqual(tokens['output'], 6500)

    def test_stream_wire_and_assembled_output_limits_keep_their_diagnosis(self):
        delta = event({'choices': [{'delta': {'content': '测试' * 20}}]})
        raw = (delta * 8 + 'data: [DONE]\n\n').encode()
        for limit in ('MAX_WIRE_BYTES', 'MAX_OUTPUT_BYTES'):
            with self.subTest(limit=limit), patch.object(streaming, limit, 512), patch.object(
                    provider.urllib.request.OpenerDirector, 'open',
                    return_value=Response(raw, 'text/event-stream')):
                with self.assertRaises(StructuredFailure) as caught:
                    provider.request(MODEL, MESSAGES, [])
                self.assertEqual(caught.exception.code, 'response_limit')

    def test_safe_http_diagnosis_preserves_context_recovery_and_no_raw_secrets(self):
        for status, code, expected in [(400, 'context_length_exceeded', 'context_overflow'),
                                       (401, 'private-key', 'model_http'), (429, 'quota', 'model_http')]:
            with self.subTest(status=status):
                error = urllib.error.HTTPError(MODEL['base'], status, 'private-key', {},
                    io.BytesIO(json.dumps({'error': {'code': code, 'message': 'private-key confidential'}}).encode()))
                with patch.object(provider.urllib.request.OpenerDirector, 'open', side_effect=error):
                    with self.assertRaises(StructuredFailure) as caught:
                        provider.request(MODEL, MESSAGES, [])
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn('private-key', failure_message(caught.exception, 'session/prompt'))
                self.assertNotIn('confidential', failure_message(caught.exception, 'session/prompt'))

    def test_anthropic_sse_retains_signed_thinking_tool_arguments_and_images(self):
        rows = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'inspect'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'signed'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'read-1', 'name': 'read_file', 'input': {}}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"/fixture"}'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}},
            {'type': 'message_stop'},
        ]
        usages = []
        raw = ''.join(event(row) for row in rows).encode()
        with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(raw, 'text/event-stream')):
            message, tokens = provider.request({**MODEL, 'kind': 'anthropic'}, MESSAGES, [], on_usage=usages.append)
        self.assertEqual(len(usages), 1)
        self.assertEqual(tokens['output'], 5)
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), {'path': '/fixture'})
        image = {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}}]}
        translated = provider._anthropic_messages([image, message,
            {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'verified'}])
        self.assertEqual(translated[0]['content'][0]['source']['media_type'], 'image/png')
        self.assertEqual(translated[1]['content'][0], {'type': 'thinking', 'thinking': 'inspect', 'signature': 'signed'})
        self.assertEqual(translated[2]['content'][0]['tool_use_id'], 'read-1')

    def test_protocol_violations_never_become_successful_tool_calls(self):
        call = {'id': 'duplicate', 'type': 'function', 'function': {'name': 'write_file', 'arguments': '{}'}}
        for message, reason in [({'content': ''}, 'stop'), ({'content': 'x'}, 'content_filter'),
                                ({'tool_calls': [call, call]}, 'tool_calls'),
                                ({'tool_calls': [{**call, 'function': {'name': 'write_file', 'arguments': '[]'}}]}, 'tool_calls')]:
            with self.subTest(message=message, reason=reason):
                payload = {'choices': [{'message': message, 'finish_reason': reason}]}
                with patch.object(provider.urllib.request.OpenerDirector, 'open', return_value=Response(payload)):
                    with self.assertRaises(StructuredFailure):
                        provider.request(MODEL, MESSAGES, [])
        with self.assertRaises(StructuredFailure):
            streaming.read(io.BytesIO(b'data: [DONE]\n\n'), time.monotonic() + 2, True, None)

    def test_invalid_endpoint_is_rejected_before_contacting_network(self):
        for base in ['https://user:private@fixture.invalid', 'https://fixture.invalid?key=private', 'file:///tmp/model']:
            with self.subTest(base=base), patch.object(provider.urllib.request.OpenerDirector, 'open') as request:
                with self.assertRaises(StructuredFailure):
                    provider.request({**MODEL, 'base': base}, MESSAGES, [])
                request.assert_not_called()
