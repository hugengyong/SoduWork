'use strict';

const catalog = require('../agent-runtime/runtime-catalog');

// Runtime identity is catalog metadata, never a claim inferred from model output.
function identity(agentId) {
  const runtime = typeof agentId === 'string' && catalog.runtime(agentId);
  return runtime ? { agentId: runtime.id, harness: runtime.displayName } : null;
}

function contract(agentId) {
  const current = identity(agentId);
  if (!current) return '';
  const facts = JSON.stringify(current).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026');
  return [
    '<soduwork-current-harness>',
    facts,
    'This is the active Agent harness for this turn, not the underlying model or its provider.',
    'When asked which Agent or harness is active, use this recorded harness identity. Historical assistant self-descriptions belong to their recorded source Agent; they do not change your current identity.',
    'Report an underlying model family or provider only when independently verified; otherwise state that it is unverified. Do not infer it from the harness name or an internal model route.',
    '</soduwork-current-harness>',
  ].join('\n');
}

function historicalSpeaker(message) {
  if (message?.role !== 'assistant') return 'user';
  const source = identity(message.sourceAgent?.agentId);
  return source ? `assistant [historical Agent: ${source.harness}; runtime ID: ${source.agentId}]`
    : 'assistant [historical Agent: unrecorded]';
}

module.exports = { identity, contract, historicalSpeaker };
