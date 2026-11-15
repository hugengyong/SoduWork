// Public runtime facts augment DSH's own prompt and never replace native guidance.
export const name = 'soduwork-execution-identity';
export const inject = ['systemPrompt'];

export function apply(ctx) {
  const text = String(process.env.SODUWORK_EXECUTION_IDENTITY || '').trim();
  if (!text) return;
  const contribution = { name: 'soduwork:execution-identity', order: -900,
    text, interpolate: false };
  // Presets own their persona prefix/suffix. A separate section survives those
  // overrides, while required context also reaches complete-prompt presets.
  ctx.systemPrompt.section(contribution);
  ctx.systemPrompt.context({ ...contribution, required: true });
}
