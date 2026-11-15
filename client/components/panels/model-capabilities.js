(function (global) {
  'use strict';
  const names = { imageInput: '图片输入', toolCalling: '工具调用', streaming: '流式输出' };
  const states = { unknown: '未确认', supported: '支持', unsupported: '不支持' };
  const statusNames = { passed: '检测通过', failed: '检测失败', unsupported: '不支持', inconclusive: '未能确认', running: '正在检测', cancelled: '已取消', skipped: '未执行' };
  const sources = { configured: '手动配置', 'legacy-native': '已有配置', verified: '实际检测', unknown: '未确认' };
  const diagnostics = {
    timeout: '服务响应超时，可稍后重试', temporarily_unavailable: '服务暂时不可用，可稍后重试',
    credentials_or_access: '请检查凭据及模型访问权限', model_or_endpoint_missing: '请检查模型 ID 和接口地址',
    capability_not_supported: '服务明确返回不支持此能力', request_not_accepted: '服务未接受检测请求，请检查协议配置',
    expected_output_missing: '未收到预期结果，暂不能确认能力', invalid_response: '服务响应格式无法识别',
    protocol_not_tested: '此协议暂无法自动检测', parameter_not_supported: '检测参数不被接受，能力仍未确认',
    cancelled: '检测已取消，已有证据仍保留', connection_blocked: '连接检查未通过，此项未执行',
    legacy_recheck_required: '旧版检测结果需要重新验证',
    billing_unavailable: '模型服务计费额度不可用，请检查供应商账户',
    budget_exhausted: '检测预算已用尽，能力仍未确认', response_too_large: '检测响应超过大小限制',
    response_limit: '检测响应超过大小限制', stream_interrupted: '流式响应未正常完成', stream_incomplete: '流式响应未正常完成'
  };
  // The workspace's esc is a lexical binding, not a window property. Keep this
  // shared component independent of how its host exposes formatting helpers.
  const e = value => String(value ?? '').replace(/[&<>"']/g,
    char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  function select(key, label, options, value) {
    return '<label class="md-cap-field"><span>' + label + '</span><select class="md-in" data-mdfield="' + key + '">' +
      Object.entries(options).map(([id, text]) => '<option value="' + id + '"' + (id === value ? ' selected' : '') + '>' + text + '</option>').join('') + '</select></label>';
  }
  function profile(model) {
    const config = model.config || {}, profile = model.capabilityProfile || {};
    return Object.fromEntries(Object.keys(names).map(key => {
      const explicit = config.capabilities?.[key];
      const legacy = key === 'imageInput' && config.imageRouting === 'native';
      return [key, explicit && explicit !== 'unknown' ? { ...profile[key], state: explicit, source: 'configured' }
        : legacy ? { ...profile[key], state: 'supported', source: 'legacy-native' }
        : profile[key] || { state: 'unknown', source: 'unknown' }];
    }));
  }
  function badges(model) {
    if (model.modality && model.modality !== 'chat') return '';
    return '<div class="md-cap-badges">' + Object.entries(profile(model)).map(([key, value]) =>
      '<span class="md-cap-badge is-' + (value.state === 'supported' ? 'supported' : 'neutral') + '" title="' + e(sources[value.source] || '未确认') + '">' +
      names[key] + '<b>' + (states[value.state] || states.unknown) + '</b>' + (value.conflict ? '<small title="配置与最近检测结果不一致">配置冲突</small>' : '') + '</span>').join('') + '</div>';
  }
  function fields(model) {
    const config = model.config || {};
    return '<div class="md-cap-fields" data-mdchat><div class="md-cap-intro"><b>模型能力</b><span>按服务实际支持的能力设置，未知项可检测确认。</span></div>' +
      '<div class="md-cap-grid">' + Object.entries(names).map(([key, name]) => select(key, name, states, config.capabilities?.[key] || 'unknown')).join('') + '</div>' +
      '<details class="md-cap-advanced"><summary>高级设置</summary><div class="md-cap-grid">' +
      select('imageRouting', '图片处理方式', { auto: '自动选择', native: '模型原生识图', 'vision-fallback': '独立视觉服务' }, config.imageRouting || 'auto') +
      '<label class="md-cap-field"><span>上下文窗口</span><input class="md-in" data-mdfield="ctxWindow" type="number" min="1024" max="10000000" step="1" placeholder="自动" value="' +
        e(model.contextWindowKnown === false ? '' : model.ctxWindow || model.contextWindow || '') + '"></label>' +
      '<label class="md-cap-field"><span>最大输出 Token</span><input class="md-in" data-mdfield="maxOutputTokens" type="number" min="1024" max="262144" step="1" placeholder="16384" value="' + e(config.maxOutputTokens || '') + '"></label>' +
      select('thinkingFormat', '思考协议格式', { '': '自动识别（按模型名）', openai: 'OpenAI reasoning_effort',
        deepseek: 'DeepSeek thinking', qwen: 'Qwen enable_thinking', 'qwen-chat-template': 'Qwen chat_template_kwargs',
        'chat-template': '自定义 chat_template_kwargs', zai: 'Z.ai thinking', openrouter: 'OpenRouter reasoning',
        together: 'Together reasoning', baseten: 'Baseten reasoning', 'ant-ling': 'Ant Ling thinking', 'string-thinking': '字符串 thinking' }, config.thinkingFormat || '') +
      select('supportsReasoningEffort', '思考强度参数', { auto: '自动识别', yes: '支持', no: '不支持' },
        typeof config.supportsReasoningEffort === 'boolean' ? config.supportsReasoningEffort ? 'yes' : 'no' : 'auto') +
      '</div><p>自动选择优先使用已确认的原生视觉能力；兼容识图需要配置独立视觉服务。</p></details></div>';
  }
  function read(container, model) {
    const config = { ...(model?.config || {}) }, capabilities = { ...config.capabilities };
    const value = key => container.querySelector('[data-mdfield="' + key + '"]')?.value || '';
    for (const key of Object.keys(names)) capabilities[key] = value(key) || 'unknown';
    config.capabilities = capabilities;
    const format = value('thinkingFormat'), effort = value('supportsReasoningEffort');
    if (format) config.thinkingFormat = format; else delete config.thinkingFormat;
    if (effort === 'yes' || effort === 'no') config.supportsReasoningEffort = effort === 'yes'; else delete config.supportsReasoningEffort;
    config.imageRouting = value('imageRouting') || 'auto';
    if (config.imageRouting === 'native' && capabilities.imageInput === 'unsupported') throw Error('原生识图与不支持图片输入的配置冲突');
    const number = (key, max) => {
      const raw = value(key);
      if (!raw) return 0;
      const num = Number(raw);
      if (!Number.isInteger(num) || num < 1024 || num > max) throw Error('Token 上限必须填写允许范围内的整数');
      return num;
    };
    const output = number('maxOutputTokens', 262144), context = number('ctxWindow', 10000000);
    if (context && output > context) throw Error('最大输出不能超过上下文窗口');
    if (output) config.maxOutputTokens = output; else delete config.maxOutputTokens;
    return { config, ctxWindow: context };
  }
  function report(result) {
    if (!result) return '<span class="md-cap-unchecked">能力尚未检测</span>';
    if (result.pending) return '<div role="status" class="md-cap-unchecked"><span>正在检测能力…</span>' +
      Object.entries(result.checks || {}).map(([key, check]) => '<div><span>' + (names[key] || '服务连接') + '</span> · <span>' +
        (statusNames[check.status] || statusNames.inconclusive) + '</span></div>').join('') + '</div>';
    if (result.error) return '<span role="alert" class="md-cap-error">' + e(result.error) + '</span>';
    const rows = Object.entries(result.checks || {}).filter(([key]) => key === 'connection' || names[key]);
    const date = result.checkedAt ? new Date(result.checkedAt) : null;
    const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleString(global.SoduWorkI18n?.getLocale?.() || undefined) : '';
    const history = (key, check) => {
      const previous = result.verifiedChecks?.[key];
      if (!previous || previous.status === check.status && previous.checkedAt === check.checkedAt) return '';
      return '<small><span>' + (previous.status === 'passed' ? '上次验证：支持' : '上次验证：不支持') + '</span>' +
        (previous.checkedAt ? '<time data-user-content data-md-checked-at="' + e(previous.checkedAt) + '">' + e(new Date(previous.checkedAt).toLocaleString(global.SoduWorkI18n?.getLocale?.() || undefined)) + '</time>' : '') + '</small>';
    };
    return '<details class="md-cap-report"><summary>检测结果' + (time ? '<time data-user-content data-md-checked-at="' + e(result.checkedAt) + '">' + e(time) + '</time>' : '') + '</summary><dl>' +
      rows.map(([key, check]) => '<div><dt>' + (names[key] || '服务连接') + '</dt><dd class="is-' + e(check.status) + '">' +
        (statusNames[check.status] || statusNames.inconclusive) + '</dd>' +
        (diagnostics[check.code] ? '<small>' + diagnostics[check.code] + '</small>' : '') + history(key, check) + '</div>').join('') +
      '</dl><p>检测结果仅适用于当前服务和模型配置；超时或网络异常不代表模型不支持。</p></details>';
  }
  function evidenceWarning(model) {
    return model.capabilityEvidenceError ? '<p role="alert" class="md-cap-error">检测记录暂不可用，请重新检测</p>' : '';
  }
  global.ModelCapabilitiesUI = { names, states, profile, badges, fields, read, report, evidenceWarning };
  global.addEventListener?.('soduwork-locale-change', () => {
    global.document?.querySelectorAll('[data-md-checked-at]').forEach(element => {
      const date = new Date(element.dataset.mdCheckedAt);
      if (!Number.isNaN(date.getTime())) element.textContent = date.toLocaleString(global.SoduWorkI18n?.getLocale?.() || undefined);
    });
  });
})(globalThis);
