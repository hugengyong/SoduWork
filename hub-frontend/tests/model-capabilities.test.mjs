import assert from 'node:assert/strict'
import test from 'node:test'
import { createComponentHarness } from './helpers/react-harness.mjs'
import { setLocale, translateText } from '../src/i18n.js'
import messages from '../src/locales/model-capabilities.js'

test('capability checks prevent duplicate calls and retry network failures without declaring unsupported', async t => {
  let resolve, calls = 0, fail = false
  const responses = []
  const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
    exportName: 'ModelCapabilityStatus', props: { model: { providerId: 'org', name: 'vision' }, onChecked: data => responses.push(data) },
    dependencies: { '../api.js': { api: { checkModelCapabilities: body => {
      calls++; assert.deepEqual(body, { providerId: 'org', model: 'vision', checks: ['connection', 'imageInput', 'toolCalling', 'streaming'] })
      return fail ? Promise.reject(new Error('Network timeout')) : new Promise(done => { resolve = done })
    } } } },
  })
  t.after(() => page.unmount())
  const button = () => page.nodes(n => n.type === 'button')[0]
  const pending = button().props.onClick(); await button().props.onClick()
  assert.equal(calls, 1); assert.equal(button().props.disabled, true)
  const result = { checks: { imageInput: { status: 'passed', code: 'verified' } } }
  resolve(result); await pending; await page.flush()
  assert.equal(responses.length, 1); assert.equal(Boolean(button().props.disabled), false)
  fail = true; await button().props.onClick(); await page.flush()
  assert.equal(page.nodes(n => n.props.role === 'alert')[0].props.children, 'Network timeout')
  assert.equal(responses.length, 1)
  fail = false; const retry = button().props.onClick(); resolve(result); await retry; await page.flush()
  assert.equal(page.nodes(n => n.props.role === 'alert').length, 0)
})

test('old check responses cannot update a removed or changed model row', async () => {
  let done, applied = 0
  const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
    exportName: 'ModelCapabilityStatus', props: { model: { providerId: 'org', name: 'vision' }, onChecked: () => applied++ },
    dependencies: { '../api.js': { api: { checkModelCapabilities: () => new Promise(resolve => { done = resolve }) } } },
  })
  const pending = page.nodes(n => n.type === 'button')[0].props.onClick()
  page.unmount(); done({ checks: {} }); await pending
  assert.equal(applied, 0)
})

test('legacy native capability is configured, not falsely marked verified', async t => {
  const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
    exportName: 'CapabilityBadges', props: { model: { config: { imageRouting: 'native' } } }, dependencies: { '../api.js': { api: {} } },
  })
  t.after(() => { page.unmount(); setLocale('zh-CN') })
  setLocale('en-US'); page.render({ model: { config: { imageRouting: 'native' } } })
  const badges = page.nodes(n => n.type === 'span')
  assert.equal(badges[0].props.title, 'Existing configuration')
  assert.equal(badges[1].props.title, 'Unknown')
})

test('all authored capability copy has an English translation', () => {
  for (const [source, english] of Object.entries(messages)) assert.equal(translateText(source, [], 'en-US'), english)
})

test('manual declarations remain visible when an older host returns an unknown profile', async t => {
  setLocale('zh-CN')
  const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
    exportName: 'CapabilityBadges', props: { model: { config: { capabilities: { imageInput: 'supported' } },
      capabilityProfile: { imageInput: { state: 'unknown', source: 'unknown' } } } }, dependencies: { '../api.js': { api: {} } },
  })
  t.after(() => page.unmount())
  assert.equal(page.nodes(n => n.type === 'b')[0].props.children, '支持')
})

test('a transient report renders retained conclusive evidence separately', async t => {
  setLocale('zh-CN')
  const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
    exportName: 'CapabilityReport', props: { result: { checks: { imageInput: { status: 'inconclusive', code: 'timeout' } },
      verifiedChecks: { imageInput: { status: 'passed', checkedAt: '2026-11-15T00:00:00Z' } } } }, dependencies: { '../api.js': { api: {} } },
  })
  t.after(() => page.unmount())
  assert.ok(page.nodes(n => n.type === 'span').some(n => n.props.children === '上次验证：支持'))
})

for (const locale of ['en-US', 'ja-JP', 'ko-KR']) {
  test('new capability diagnostics have native ' + locale + ' translations', async () => {
    const { messagesByLocale } = await import('../src/locales/extra-languages.js');
    const owned = locale === 'en-US' ? messages : messagesByLocale[locale];
    for (const source of ["此协议暂无法自动检测","检测参数不被接受，能力仍未确认","检测已取消，已有证据仍保留","旧版检测结果需要重新验证","连接检查未通过，此项未执行","模型服务计费额度不可用，请检查供应商账户","检测预算已用尽，能力仍未确认","检测响应超过大小限制","流式响应未正常完成","上次验证：支持","上次验证：不支持","配置与最近检测结果不一致","配置冲突","已取消","未执行"]) {
      assert.ok(Object.hasOwn(owned, source), locale + ': ' + source);
      assert.equal(translateText(source, [], locale), owned[source]);
      if (locale !== 'en-US') assert.notEqual(owned[source], messages[source]);
    }
  });
}

for (const locale of ['zh-CN', 'en-US', 'ja-JP', 'ko-KR']) {
  test('admin capability report retains evidence and formats both timestamps in ' + locale, async t => {
    setLocale(locale);
    const current = '2026-11-15T13:24:56Z', previous = '2026-10-14T02:13:45Z';
    const result = { checkedAt: current, checks: { imageInput: { status: 'inconclusive', code: 'parameter_not_supported' } },
      verifiedChecks: { imageInput: { status: 'passed', checkedAt: previous } } };
    const before = JSON.stringify(result);
    const page = await createComponentHarness(new URL('../src/screens/ModelCapabilities.jsx', import.meta.url), {
      exportName: 'CapabilityReport', props: { result }, dependencies: { '../api.js': { api: {} } },
    });
    t.after(() => { page.unmount(); setLocale('zh-CN'); });
    const times = page.nodes(node => node.type === 'time');
    assert.deepEqual(times.map(node => node.props.children), [current, previous].map(value => new Date(value).toLocaleString(locale)));
    assert.ok(page.nodes(node => node.type === 'span').some(node => node.props.children === translateText('上次验证：支持', [], locale)));
    assert.equal(JSON.stringify(result), before);
  });
}
