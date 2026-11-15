'use strict';

const contract = require('../model-capability-contract.cjs');

// Model changes apply at a settled turn boundary. Keep the SDK's real tool
// loadout aligned with a text-only provider, then restore it for later turns.
function prepare(session, model, toolsRequired) {
  contract.assertRequired(model, ['streaming', ...(toolsRequired ? ['toolCalling'] : [])]);
  if (model?.capabilityProfile?.toolCalling?.state !== 'unsupported') return () => {};
  const previous = session.getActiveToolNames();
  session.setActiveToolsByName([]);
  let restored = false;
  return () => {
    if (restored) return;
    restored = true;
    session.setActiveToolsByName(previous);
  };
}

module.exports = { prepare };
