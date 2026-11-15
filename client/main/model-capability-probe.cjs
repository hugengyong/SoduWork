'use strict';
const crypto = require('node:crypto');
const { CHECKS, API_FAMILY, compatibility } = require('./model-capability-contract.cjs');
const request = require('./model-capability-request.cjs');
const image = require('./model-capability-image.cjs');
const stamp = () => new Date().toISOString();
const nonce = () => crypto.randomBytes(8).toString('hex');
const tools = [{ type: 'function', function: { name: 'soduwork_capability_echo',
  description: 'Return a supplied capability-test value without side effects.', parameters: { type: 'object',
    properties: { value: { type: 'string' } }, required: ['value'], additionalProperties: false } } }];

function boundedFetch(fetchImpl, conf, maxBytes, connectMs) {
  const endpoint = new URL(conf.baseUrl.replace(/\/+$/, '') + '/chat/completions').href;
  return async (url, options = {}) => {
    if (String(url) !== endpoint) throw Error('Unexpected model endpoint');
    const headers = new Headers(options.headers);
    if (conf.authMode === 'none' || !conf.key) headers.delete('Authorization');
    const connected = new AbortController();
    const timer = setTimeout(() => connected.abort(new DOMException('Connection timeout', 'TimeoutError')), connectMs);
    let response;
    try { response = await fetchImpl(url, { ...options, headers, redirect: 'error',
      signal: AbortSignal.any([options.signal, connected.signal].filter(Boolean)) }); }
    finally { clearTimeout(timer); }
    if (!response.body) return response;
    let size = 0;
    return new Response(response.body.pipeThrough(new TransformStream({ transform(part, controller) {
      size += part.byteLength;
      if (size > maxBytes) throw Object.assign(Error('Response limit exceeded'), { code: 'BODY_LIMIT' });
      controller.enqueue(part);
    } })), { status: response.status, statusText: response.statusText, headers: response.headers });
  };
}
async function collect(data, stream) {
  if (!stream) {
    const choice = data?.choices?.[0];
    if (!choice?.message) throw Error('Invalid response');
    return { message: choice.message, finish: choice.finish_reason, reasoning: Boolean(choice.message.reasoning_content) };
  }
  let content = '', finish = '', reasoning = false;
  const calls = new Map();
  try {
    for await (const event of data) {
      if (event.error) throw Error('Stream error');
      for (const choice of event.choices || []) {
        if (choice.finish_reason) finish = choice.finish_reason;
        const delta = choice.delta || {};
        if (typeof delta.content === 'string') content += delta.content;
        reasoning ||= Boolean(delta.reasoning_content || delta.reasoning || delta.thinking);
        for (const part of delta.tool_calls || []) {
          const index = part.index ?? 0, call = calls.get(index) || { id: '', type: 'function', function: { name: '', arguments: '' } };
          if (part.id && part.id !== call.id) call.id += part.id;
          if (part.type && part.type !== 'function') throw Error('Invalid tool call');
          call.function.name += part.function?.name || '';
          call.function.arguments += part.function?.arguments || '';
          calls.set(index, call);
        }
      }
    }
  } finally { data.controller?.abort(); }
  if (!finish) throw Object.assign(Error('Incomplete stream'), { code: 'INCOMPLETE_STREAM' });
  return { message: { role: 'assistant', content: content || null, ...(calls.size ? { tool_calls: [...calls.values()] } : {}) }, finish, reasoning };
}
async function run(conf, requested = CHECKS, options = {}) {
  if (!Array.isArray(requested) || !requested.length || requested.some(key => !CHECKS.includes(key))) throw Error('无效的能力检测项目');
  const checks = {}, state = request.policy(conf), selected = [...new Set(requested)];
  const confirmedCompatibility = compatibility(conf.compatibility);
  const total = AbortSignal.timeout(options.totalMs || 120000);
  const signal = AbortSignal.any([total, options.signal].filter(Boolean));
  let calls = 0, negotiations = 0, reasoningStreamReported = false, blocked = false;
  let client;
  async function execute(messages, stream = false, withTools = false) {
    while (true) {
      if (calls >= 7) throw Object.assign(Error('Request budget exhausted'), { code: 'REQUEST_BUDGET' });
      signal.throwIfAborted(); calls++;
      const operationSignal = AbortSignal.any([signal, AbortSignal.timeout(options.timeoutMs || 30000)]);
      try {
        const data = await client.chat.completions.create(request.payload(conf, state, messages, stream, withTools ? tools : undefined),
          { signal: operationSignal });
        const response = await collect(data, stream);
        operationSignal.throwIfAborted();
        // Publish only parameters accepted by a completed exchange. A cold
        // timeout must not replace a working runtime with probe defaults.
        confirmedCompatibility.maxTokensField = state.compatibility.maxTokensField;
        if (stream) confirmedCompatibility.streamOptions = state.compatibility.streamOptions;
        if (withTools) confirmedCompatibility.toolChoice = state.compatibility.toolChoice;
        reasoningStreamReported ||= stream && response.reasoning;
        return response;
      } catch (error) {
        if (operationSignal.aborted) throw operationSignal.reason;
        if (negotiations < 2 && request.negotiate(error, state)) { negotiations++; continue; }
        throw error;
      }
    }
  }
  if ((conf.kind || 'openai-compat') === 'openai-compat') {
    const OpenAI = require('openai');
    client = new OpenAI({ apiKey: conf.key || 'soduwork-local-no-key', baseURL: conf.baseUrl,
      maxRetries: 0, timeout: options.timeoutMs || 30000,
      fetch: boundedFetch(options.fetchImpl || global.fetch, conf, options.maxBytes || 256 * 1024, options.connectMs || 10000) });
  }
  for (const check of selected) {
    const checkedAt = stamp(); let result;
    if (!client) result = { status: 'inconclusive', code: 'protocol_not_tested' };
    else if (blocked) result = { status: 'skipped', code: 'connection_blocked' };
    else {
      options.progress?.({ check, status: 'running' });
      try {
        let expected = nonce(), content = 'Reply only with ' + expected + '.';
        if (check === 'imageInput') {
          const challenge = (options.challenge || image.challenge)(); expected = challenge.expected;
          content = [{ type: 'text', text: 'Read the six digits in this image. Reply only with those digits.' },
            { type: 'image_url', image_url: { url: challenge.url } }];
        } else if (check === 'toolCalling') content = 'Call soduwork_capability_echo with value ' + expected + '. This is a capability check.';
        const messages = [{ role: 'user', content }];
        const response = await execute(messages, check === 'streaming' || check === 'toolCalling', check === 'toolCalling');
        let passed = typeof response.message.content === 'string' && response.message.content.trim() === expected;
        if (check === 'toolCalling') {
          const call = response.message.tool_calls?.length === 1 ? response.message.tool_calls[0] : null;
          let argumentsValue;
          try { argumentsValue = JSON.parse(call?.function.arguments || '{}'); } catch { /* Unconfirmed output. */ }
          passed = Boolean(response.finish === 'tool_calls' && call?.id && call.function?.name === 'soduwork_capability_echo' &&
            argumentsValue?.value === expected && Object.keys(argumentsValue).length === 1);
          if (passed) {
            const value = nonce();
            const followup = await execute([...messages, response.message, { role: 'tool', tool_call_id: call.id, content: value },
              { role: 'user', content: 'Reply only with the tool result value.' }]);
            passed = followup.message.content?.trim() === value && followup.finish === 'stop';
          }
        }
        if (response.finish === 'length') result = { status: 'inconclusive', code: 'budget_exhausted' };
        else if (!['stop', 'tool_calls'].includes(response.finish)) result = { status: 'inconclusive', code: 'invalid_response' };
        else result = { status: passed ? 'passed' : 'inconclusive', code: passed ? 'verified' : 'expected_output_missing' };
      } catch (error) {
        result = request.failure(error, check, signal);
        if (error.code === 'REQUEST_BUDGET') result = { status: 'inconclusive', code: 'budget_exhausted' };
        blocked = ['credentials_or_access', 'model_or_endpoint_missing', 'billing_unavailable'].includes(result.code);
      }
    }
    checks[check] = { ...result, checkedAt };
    options.progress?.({ check, ...checks[check] });
  }
  return { reportVersion: 2, apiFamily: API_FAMILY, checks, checkedAt: stamp(), reasoningStreamReported,
    compatibility: confirmedCompatibility, requestCount: calls };
}
module.exports = { run };
