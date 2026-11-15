'use strict';
const thinking = require('./conversation/thinking-capabilities');
const { compatibility } = require('./model-capability-contract.cjs');

function policy(conf) {
  const config = { ...conf, ...conf.config, id: conf.name };
  const profile = thinking.profile(config), always = config.thinkingMode === 'always' || config.thinkingAlwaysOn === true;
  return { compatibility: { maxTokensField: 'max_tokens', toolChoice: 'auto', streamOptions: true,
    ...compatibility(conf.compatibility), ...compatibility(conf.config?.compatibility) },
  config, profile, always, budget: Math.min(always ? 2048 : 512, Number(config.maxOutputTokens) || 65536, Number(conf.ctxWindow || conf.contextWindow) || 128000) };
}
function thinkingParams(state) {
  // A disabled thinking profile omits its UI format, but a known wire model
  // still needs its provider's explicit off parameter. Generic aliases stay unknown.
  const format = state.config.thinkingFormat || state.profile.format ||
    (state.config.thinkingMode === 'none' ? thinking.profile({ id: state.config.id }).format : '');
  const enabled = state.always;
  if (format === 'qwen') return { enable_thinking: enabled };
  if (format === 'qwen-chat-template') return { chat_template_kwargs: { enable_thinking: enabled, preserve_thinking: true } };
  if (format === 'deepseek') return { thinking: { type: enabled ? 'enabled' : 'disabled' } };
  if (format === 'zai') return { thinking: { type: enabled ? 'enabled' : 'disabled', clear_thinking: false } };
  if (format === 'openrouter') return { reasoning: { enabled, ...(enabled ? { effort: 'low' } : {}) } };
  if (format === 'together') return { reasoning: { enabled } };
  if (format === 'baseten') return { chat_template_kwargs: { enable_thinking: enabled } };
  if (format === 'string-thinking') return { thinking: enabled ? 'enabled' : 'disabled' };
  return enabled && state.profile.supportsReasoningEffort ? { reasoning_effort: 'low' } : {};
}
function payload(conf, state, messages, stream, tools) {
  return { model: conf.name, messages, stream, [state.compatibility.maxTokensField]: state.budget,
    ...thinkingParams(state),
    ...(stream && state.compatibility.streamOptions ? { stream_options: { include_usage: true } } : {}),
    ...(tools ? { tools, ...(state.compatibility.toolChoice === 'named'
      ? { tool_choice: { type: 'function', function: { name: 'soduwork_capability_echo' } } } : { tool_choice: 'auto' }) } : {}) };
}
function negotiate(error, state) {
  if (![400, 422].includes(error?.status)) return false;
  const problem = error.error || error.body?.error || {}, message = String(problem.message || error.message || '').toLowerCase();
  const param = String(problem.param || '').toLowerCase();
  const explicit = ['unsupported_parameter', 'unsupported_value', 'unknown_parameter', 'unrecognized_parameter'].includes(problem.code || problem.type);
  const rejected = name => explicit && param === name || new RegExp('(?:unsupported|unknown|unrecognized|not support).{0,40}' + name + '|' + name + '.{0,40}(?:not support|unsupported)').test(message);
  if (rejected('tool_choice') && state.compatibility.toolChoice !== 'auto') { state.compatibility.toolChoice = 'auto'; return true; }
  if (rejected('stream_options') && state.compatibility.streamOptions) { state.compatibility.streamOptions = false; return true; }
  const current = state.compatibility.maxTokensField;
  if (rejected(current)) { state.compatibility.maxTokensField = current === 'max_tokens' ? 'max_completion_tokens' : 'max_tokens'; return true; }
  return false;
}
function failure(error, check, signal) {
  const problem = error?.error || error?.body?.error || {}, code = String(problem.code || '');
  if (signal?.aborted) return { status: signal.reason?.name === 'TimeoutError' ? 'inconclusive' : 'cancelled', code: signal.reason?.name === 'TimeoutError' ? 'timeout' : 'cancelled' };
  // SDK transport errors retain a cause. Classify only finite error names and
  // codes; upstream messages stay private and cannot declare a capability.
  for (let cause = error, depth = 0; cause && depth < 5; cause = cause.cause, depth++) {
    if (cause.code === 'BODY_LIMIT') return { status: 'inconclusive', code: 'response_too_large' };
    if (['APIConnectionTimeoutError', 'TimeoutError'].includes(cause.name)) return { status: 'inconclusive', code: 'timeout' };
  }
  if (['insufficient_quota', 'insufficient_balance', 'billing_hard_limit_reached', '1113'].includes(code)) return { status: 'failed', code: 'billing_unavailable' };
  if ([401, 403].includes(error?.status)) return { status: 'failed', code: 'credentials_or_access' };
  if (error?.status === 404) return { status: 'failed', code: 'model_or_endpoint_missing' };
  if (error?.name === 'APIConnectionError') return { status: 'inconclusive', code: 'temporarily_unavailable' };
  if ([408, 429, 500, 502, 503, 504].includes(error?.status)) return { status: 'inconclusive', code: [408, 504].includes(error.status) ? 'timeout' : 'temporarily_unavailable' };
  const codes = { imageInput: ['unsupported_image_input', 'image_input_not_supported'], toolCalling: ['unsupported_tools', 'tool_calling_not_supported'], streaming: ['streaming_not_supported'] };
  if (codes[check]?.includes(code)) return { status: 'unsupported', code: 'capability_not_supported' };
  return { status: 'inconclusive', code: error?.code === 'INCOMPLETE_STREAM' ? 'stream_interrupted' : error?.status ? 'request_not_accepted' : 'invalid_response' };
}
module.exports = { policy, payload, negotiate, failure, thinkingParams };
