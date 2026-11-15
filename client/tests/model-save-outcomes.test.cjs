'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const renderer = require('./renderer-source.cjs');
const store = require('../main/local-model-store.cjs');

async function fixture(t, { collision = false, managedPersonal = false, localKey = '' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-save-outcome-'));
  const timers = new Set();
  store.configure({ app: { getPath: () => root }, safeStorage: {
    isEncryptionAvailable: () => true, encryptString: text => Buffer.from(text),
    decryptString: bytes => bytes.toString(),
  } });
  t.after(() => { for (const timer of timers) clearTimeout(timer); store.configure(null); fs.rmSync(root, { recursive: true, force: true }); });
  store.save({ providers: [{ id: 'custom-private', name: 'Private service',
    baseUrl: 'http://127.0.0.1:1/v1', authMode: localKey ? 'key' : 'none' }],
  models: [{ providerId: 'custom-private', name: 'private-model' }],
  keys: localKey ? { 'custom-private': localKey } : {} });
  const managedId = collision ? 'custom-private' : 'managed';
  const managed = { revision: 'hub-1', providers: [{ id: managedId, name: 'Managed service',
    baseUrl: 'https://fixture.invalid/v1', modelNames: [], personal: managedPersonal }],
    myModels: collision ? [{ providerId: managedId, name: 'hub-model' }] : [],
    usable: collision ? [{ providerId: managedId, name: 'hub-model' }] : [] };
  const state = { failHubWrite: false, failHubRead: false, localInputs: [], hubInputs: [], probes: [], notices: [], gate: null };
  const values = new Map(), button = { disabled: false, textContent: '' }, field = { disabled: false };
  const body = { innerHTML: '', setAttribute() {}, removeAttribute() {}, querySelector: () => null,
    querySelectorAll: selector => selector === 'input,select,textarea,button' ? [field] : [] };
  const box = { URL, URLSearchParams, AbortController, Response, CustomEvent: class {}, structuredClone,
    __SODUWORK_TEST_HUB_CONFIG__: { allowSwitch: true },
    setTimeout: (...args) => { const timer = setTimeout(...args); timers.add(timer); return timer; },
    clearTimeout, setInterval: () => 0, navigator: { platform: 'test' },
    console: { log() {}, error() {} }, dispatchEvent() {}, addEventListener() {},
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)),
      removeItem: key => values.delete(key) },
    document: { getElementById: id => id === 'mdBody' ? body : id === 'mdSave' ? button : null },
    petSay: message => state.notices.push(message),
    esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    soduWorkAPI: { localModels: { get: async () => store.get(), save: async input => {
      state.localInputs.push(structuredClone(input));
      if (state.gate) await state.gate;
      return store.save(input);
    } } },
    fetch: async (url, options = {}) => {
      if (options.method === 'POST') {
        state.probes.push({ url, body: JSON.parse(options.body) });
        return new Response('{"ok":true}');
      }
      if (options.method === 'PUT') {
        const input = JSON.parse(options.body); state.hubInputs.push(input);
        if (state.failHubWrite) throw Error('Synthetic Hub write failure');
        assert.equal(input.expectedRevision, managed.revision);
        managed.providers = input.providers; managed.myModels = input.models;
        managed.revision = 'hub-' + (Number(managed.revision.split('-')[1]) + 1);
        return new Response('{"ok":true}');
      }
      if (state.failHubRead) throw Error('Synthetic Hub read failure');
      return new Response(JSON.stringify(managed));
    },
  };
  box.window = box; vm.createContext(box);
  const human = fs.readFileSync(path.join(__dirname, '../components/pet/pet-ui.js'), 'utf8');
  vm.runInContext(human.slice(human.indexOf('function _humanErr('), human.indexOf('/* Speech bubbles. */')), box);
  vm.runInContext(renderer.readFileSync(path.join(__dirname, '../api.js'), 'utf8'), box);
  for (const file of ['model-capabilities.js', 'model-catalog-editor.js', 'model-service-delete.js', 'panels-models.js'])
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../components/panels', file), 'utf8'), box, { filename: file });
  const evaluate = source => vm.runInContext(source, box);
  await evaluate('_mdLoad()');
  return { state, box, button, field, body, evaluate, snapshot: () => store.get(),
    touch: (id, patch) => evaluate(`_mdTouchProvider(${JSON.stringify(id)}, ${JSON.stringify(patch)})`),
    async removeProvider(id) {
      box.deletePlan = await evaluate(`_mdPrepareServiceDelete(${JSON.stringify(id)})`);
      return evaluate('_mdDeleteService(deletePlan)');
    } };
}

test('a partial mixed save retains Hub edits and retries without rewriting committed local models', async t => {
  const f = await fixture(t);
  f.touch('managed', { baseUrl: 'https://edited.fixture.invalid/v1' });
  f.touch('device:custom-private', { name: 'Saved local edit' });
  f.state.failHubWrite = true;
  assert.equal(await f.evaluate('_mdSave(false)'), false);
  assert.equal(f.snapshot().providers[0].name, 'Saved local edit');
  assert.equal(f.snapshot().revision, 2);
  assert.equal(f.evaluate('_mdData.localRevision'), 2);
  assert.equal(f.evaluate('_mdLocalDirty'), false);
  assert.equal(f.evaluate('_mdManagedDirty'), true);
  assert.equal(f.evaluate('_mdProvider("managed").baseUrl'), 'https://edited.fixture.invalid/v1');
  assert.ok(f.state.notices.includes('本地模型配置已保存，Hub 模型配置未保存，请重试。'));
  f.state.failHubWrite = false;
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.state.localInputs.length, 1, 'retry must not replay the already committed local revision');
  assert.equal(f.state.hubInputs.length, 2);
  assert.equal(f.evaluate('_mdManagedDirty'), false);
  assert.equal(JSON.stringify(f.state.hubInputs).includes('custom-private'), false);
});

test('same-ID local and Hub providers keep distinct catalog, edits, and deletions', async t => {
  const f = await fixture(t, { collision: true });
  const api = f.box.SoduWorkAPI, catalog = await api.myModels();
  assert.deepEqual(Array.from(catalog.providers, row => row.id), ['device:custom-private', 'custom-private']);
  assert.equal(catalog.providers[0].localProviderId, 'custom-private');
  assert.equal(catalog.usable.find(row => row.localOnly).providerId, 'device:custom-private');
  assert.match(catalog.usable.find(row => row.localOnly).name, /^local:model-[a-f0-9]{32}$/);
  assert.equal(catalog.usable.find(row => !row.localOnly).name, 'hub-model');
  await assert.rejects(api.saveMyModels({ keys: { 'custom-private': 'ambiguous-secret' } }), /来源不明确/);
  assert.equal(f.state.localInputs.length, 0); assert.equal(f.state.hubInputs.length, 0);
  f.touch('device:custom-private', { name: 'Private edit', baseUrl: 'http://127.0.0.1:2/v1' });
  f.touch('custom-private', { baseUrl: 'https://edited.fixture.invalid/v1' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers[0].id, 'custom-private');
  assert.equal(f.snapshot().providers[0].name, 'Private edit');
  assert.equal(f.state.hubInputs[0].providers[0].id, 'custom-private');
  assert.equal(f.state.hubInputs[0].models[0].name, 'hub-model');
  assert.doesNotMatch(JSON.stringify(f.state.hubInputs), /device:|local:model-|127\.0\.0\.1:2|Private edit|private-model/);
  assert.equal(await f.removeProvider('device:custom-private'), true);
  assert.equal(f.snapshot().providers.length, 0);
  const remaining = await api.myModels();
  assert.equal(remaining.providers.length, 1); assert.equal(remaining.providers[0].id, 'custom-private');
  assert.equal(remaining.myModels[0].name, 'hub-model');
});

test('same-ID provider probes and explicit key patches use their declared ownership', async t => {
  const f = await fixture(t, { collision: true }), api = f.box.SoduWorkAPI, localProbes = [];
  f.box.soduWorkAPI.localModels.test = async (providerId, model) => { localProbes.push({ providerId, model }); return { ok: true }; };
  f.box.soduWorkAPI.localModels.checkCapabilities = async input => { localProbes.push({ modelRef: input.modelRef }); return { ok: true }; };
  await assert.rejects(api.testMyModel('custom-private'), /来源不明确/);
  await assert.rejects(api.checkMyModelCapabilities({ providerId: 'custom-private', model: 'private-model' }), /来源不明确/);
  assert.equal(f.state.probes.length, 0); assert.equal(localProbes.length, 0);
  await api.testMyModel('device:custom-private');
  await api.checkMyModelCapabilities({ providerId: 'device:custom-private', model: 'private-model' });
  assert.equal(localProbes.length, 2); assert.equal(localProbes[0].providerId, 'custom-private');
  assert.equal(localProbes[1].modelRef, f.snapshot().myModels[0].id);
  assert.equal(f.state.probes.length, 0);
  await api.testMyModel('custom-private', { localOnly: false });
  await api.checkMyModelCapabilities({ providerId: 'custom-private', localOnly: false, model: 'hub-model' });
  assert.equal(f.state.probes.length, 2);
  assert.doesNotMatch(JSON.stringify(f.state.probes), /device:|private-model/);
  const catalog = await api.myModels();
  await api.saveMyModels({ providers: catalog.providers, models: catalog.myModels, expectedRevision: catalog.revision,
    localProviders: catalog.providers.filter(row => row.localOnly), localModels: catalog.myModels.filter(row => row.localOnly),
    keys: { 'custom-private': 'hub-secret' }, localKeys: { 'device:custom-private': 'device-secret' }, localRevision: catalog.localRevision });
  assert.equal(f.state.localInputs.at(-1).keys['custom-private'], 'device-secret');
  assert.equal(f.state.hubInputs.at(-1).keys['custom-private'], 'hub-secret');
  assert.doesNotMatch(JSON.stringify(f.state.hubInputs), /device-secret|device:|local:model-/);
});

test('deleting a Hub service with the same raw device ID preserves the device service and its key', async t => {
  const f = await fixture(t, { collision: true, managedPersonal: true, localKey: 'synthetic-device-key' });
  assert.equal(await f.removeProvider('custom-private'), true);
  assert.equal(f.state.localInputs.length, 0, 'a Hub deletion must not write the device catalog');
  assert.equal(f.snapshot().revision, 1);
  assert.equal(f.snapshot().providers.length, 1);
  assert.equal(f.snapshot().providers[0].id, 'custom-private');
  assert.equal(f.snapshot().providers[0].keySet, true);
  assert.equal(f.snapshot().myModels[0].name, 'private-model');
  assert.equal(f.state.hubInputs.length, 1);
  assert.deepEqual(f.state.hubInputs[0].providers, []);
  assert.deepEqual(f.state.hubInputs[0].models, []);
  assert.deepEqual(f.state.hubInputs[0].keys, {}, 'provider removal must not submit an ambiguous raw-ID key patch');
  assert.equal(f.state.hubInputs[0].expectedRevision, 'hub-1');
  assert.equal(f.evaluate('_mdProvider("custom-private")'), undefined);
  assert.equal(f.evaluate('_mdProvider("device:custom-private").myKeySet'), true);
  assert.equal(f.evaluate('_mdData.revision'), 'hub-2');
});

test('deleting the last local service never sends its removal key to a later managed save', async t => {
  const f = await fixture(t);
  f.touch('managed', { baseUrl: 'https://edited.fixture.invalid/v1' });
  assert.equal(await f.removeProvider('device:custom-private'), true);
  assert.equal(f.snapshot().providers.length, 0);
  assert.equal(f.state.localInputs.length, 1);
  assert.equal(f.state.hubInputs.length, 0, 'deleting a local service must not submit an unrelated managed draft');
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.state.hubInputs.length, 1);
  assert.equal(JSON.stringify(f.state.hubInputs).includes('custom-private'), false);
});

test('last-provider deletion survives a failed Hub reload and keeps local editing usable', async t => {
  const f = await fixture(t);
  f.state.failHubRead = true;
  assert.equal(await f.removeProvider('device:custom-private'), true);
  assert.equal(f.snapshot().providers.length, 0);
  assert.equal(f.evaluate('_mdData.localRevision'), 2);
  assert.equal(f.evaluate('_mdData.usable.length'), 0);
  assert.equal(f.evaluate('_mdLoadState'), 'ready');
  assert.equal(f.evaluate('_mdDirty'), false);
  assert.doesNotMatch(f.body.innerHTML, /data-md-load-error/);
  assert.equal(f.state.notices.some(message => message.includes('没存上')), false);
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  f.touch(id, { name: 'Replacement local', baseUrl: 'http://127.0.0.1:2/v1', authMode: 'none' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers[0].name, 'Replacement local');
  assert.equal(f.state.hubInputs.length, 0);
});

test('pending local persistence disables edits and joins reentrant saves without losing a later edit', async t => {
  const f = await fixture(t);
  let release;
  f.state.gate = new Promise(resolve => { release = resolve; });
  f.touch('device:custom-private', { name: 'First explicit edit' });
  const first = f.evaluate('_mdSave(false)'), second = f.evaluate('_mdSave(false)');
  assert.equal(f.button.disabled, true);
  assert.equal(f.field.disabled, true);
  for (let i = 0; !f.state.localInputs.length && i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.state.localInputs.length, 1);
  f.touch('device:custom-private', { name: 'Unaccepted in-flight event' });
  assert.equal(f.evaluate('_mdProvider("device:custom-private").name'), 'First explicit edit');
  release();
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(f.state.localInputs.length, 1);
  assert.equal(f.field.disabled, false);
  f.touch('device:custom-private', { name: 'Second explicit edit' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers[0].name, 'Second explicit edit');
  assert.equal(f.snapshot().revision, 3);
});

test('a committed local save stays successful when catalog rendering fails', async t => {
  const f = await fixture(t);
  f.touch('device:custom-private', { name: 'Persisted before render failure' });
  f.evaluate('_mdRender = () => { throw Error("Synthetic rendering failure"); }');
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers[0].name, 'Persisted before render failure');
  assert.equal(f.evaluate('_mdLocalDirty'), false);
  assert.ok(f.state.notices.includes('模型配置已保存，但模型清单刷新失败，请重新打开模型页面。'));
});

for (const change of ['hub', 'panel']) test(`a ${change} switch during local persistence cannot send the old managed draft`, async t => {
  const f = await fixture(t);
  let release;
  f.state.gate = new Promise(resolve => { release = resolve; });
  f.touch('managed', { baseUrl: 'https://previous.fixture.invalid/v1' });
  f.touch('device:custom-private', { name: 'Local edit' });
  const saved = f.evaluate('_mdSave(false)');
  for (let i = 0; !f.state.localInputs.length && i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.state.localInputs.length, 1);
  if (change === 'hub') f.box.SoduWorkAPI.setHubEnvironment('development', 'http://127.0.0.1:9876');
  else f.evaluate('closeModelsPanel()');
  release();
  assert.equal(await saved, false);
  assert.equal(f.snapshot().providers[0].name, 'Local edit');
  assert.equal(f.state.hubInputs.length, 0);
});

test('a name-only service stays visibly unsaved and reports the missing API address', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  f.touch(f.evaluate('_mdNew.id'), { name: 'Incomplete service' });
  assert.equal(await f.evaluate('_mdSave(false)'), false);
  assert.equal(f.state.localInputs.length, 0);
  assert.equal(f.snapshot().revision, 1);
  assert.match(f.body.innerHTML, /id="md-url-error"[^>]*>请填写 API 地址/);
  assert.deepEqual(f.state.notices, []);
  assert.match(f.body.innerHTML, /cfg-state is-draft[^>]*>未保存/);
  assert.equal(f.button.disabled, false);
});

test('reopening keeps a new service and its key and model pending until a successful write', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  f.touch(id, { name: 'Pending service' });
  f.evaluate(`_mdKeyEdits[${JSON.stringify(id)}] = 'synthetic-draft-key';
    _mdMyModels.push({providerId:${JSON.stringify(id)}, name:'draft-model'});
    closeModelsPanel(); _mdEditor = '';`);
  assert.equal(await f.evaluate('_mdLoad()'), true);
  assert.equal(f.evaluate('_mdNew.id'), id);
  assert.equal(f.evaluate(`_mdKeyEdits[${JSON.stringify(id)}]`), 'synthetic-draft-key');
  assert.equal(f.evaluate(`_mdMyModels.some(row => row.providerId === ${JSON.stringify(id)})`), true);
  assert.equal(f.evaluate('_mdLocalDirty && _mdDirty'), true);
  assert.equal(f.button.disabled, false);
  assert.equal(f.button.textContent, '保存');
  assert.equal(f.snapshot().revision, 1);
  f.touch(id, { baseUrl: 'http://127.0.0.1:2/v1' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers.some(row => 'device:' + row.id === id), true);
  assert.equal(f.evaluate('_mdNew'), null);
  assert.equal(f.button.textContent, '已保存');
});

test('reopening a new service editor preserves its complete device namespace when selecting the draft', async t => {
  const f = await fixture(t, { collision: true });
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  assert.match(id, /^device:custom-/);
  f.touch(id, { name: 'Reopened local draft' });
  f.evaluate(`closeModelsPanel(); _mdOpen = ''; _mdEditor = ${JSON.stringify('__new__:' + id)};`);
  assert.equal(await f.evaluate('_mdLoad()'), true);
  assert.equal(f.evaluate('_mdOpen'), id);
  assert.equal(f.evaluate('_mdNew.id'), id);
  assert.match(f.body.innerHTML, new RegExp('data-mdselect="' + id + '" aria-pressed="true"'));
  assert.match(f.body.innerHTML, /cfg-state is-draft[^>]*>未保存/);
  assert.equal(f.snapshot().providers.length, 1);
  assert.equal(f.state.localInputs.length, 0);
  assert.equal(f.state.hubInputs.length, 0);
});

test('saving another local service leaves an incomplete new service pending', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  f.touch(id, { name: 'Unfinished new service' });
  f.evaluate(`_mdKeyEdits[${JSON.stringify(id)}] = 'synthetic-pending-key';
    _mdMyModels.push({providerId:${JSON.stringify(id)}, name:'pending-model', localOnly:true});`);
  f.evaluate('_mdOpen = "device:custom-private"; _mdEditor = "";');
  f.touch('device:custom-private', { name: 'Explicitly saved edit' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.snapshot().providers[0].name, 'Explicitly saved edit');
  assert.equal(f.snapshot().providers.length, 1);
  assert.equal(f.evaluate('_mdNew.id'), id);
  assert.equal(f.evaluate(`_mdKeyEdits[${JSON.stringify(id)}]`), 'synthetic-pending-key');
  assert.equal(f.evaluate(`_mdMyModels.filter(row=>row.providerId===${JSON.stringify(id)}).length`), 1);
  assert.equal(JSON.stringify(f.state.localInputs).includes(id), false);
  assert.equal(f.evaluate('_mdLocalDirty && _mdDirty'), true);
  assert.equal(f.button.textContent, '保存');
  assert.ok(f.state.notices.includes('其他模型配置已保存，新增服务尚未保存，请补全后保存。'));
});

test('an incomplete new draft cannot force a local write during a managed save', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService(); _mdOpen = "managed"; _mdEditor = "";');
  f.touch('managed', { baseUrl: 'https://edited.fixture.invalid/v1' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.state.localInputs.length, 0);
  assert.equal(f.state.hubInputs.length, 1);
  assert.equal(f.snapshot().revision, 1);
  assert.equal(f.evaluate('_mdLocalDirty && _mdDirty'), true);
});

test('a draft confirmed in storage is cleared even after the editor selection changes', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  store.save({ providers: [...f.snapshot().providers,
    { id: id.slice('device:'.length), name: 'Committed elsewhere', baseUrl: 'http://127.0.0.1:2/v1', authMode: 'none' }] });
  f.evaluate('closeModelsPanel(); _mdEditor = "";');
  assert.equal(await f.evaluate('_mdLoad()'), true);
  assert.equal(f.evaluate('_mdNew'), null);
  assert.equal(f.evaluate('_mdDirty'), false);
  assert.equal(f.button.textContent, '已保存');
});

test('an unsaved new service is not carried into a different Hub scope', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  f.touch(id, { name: 'Previous Hub draft' });
  f.box.SoduWorkAPI.setHubEnvironment('development', 'http://127.0.0.1:9876');
  assert.equal(await f.evaluate('_mdLoad()'), true);
  assert.equal(f.evaluate('_mdNew'), null);
  assert.doesNotMatch(f.body.innerHTML, /Previous Hub draft/);
});

for (const kind of ['connection', 'capability']) test(`${kind} checks ignore an unrelated incomplete service draft`, async t => {
  const f = await fixture(t);
  let probes = 0;
  f.box.SoduWorkAPI.testMyModel = async () => { probes++; return { ok: true, models: ['private-model'] }; };
  f.box.SoduWorkAPI.checkMyModelCapabilities = async () => { probes++; return { ok: true }; };
  f.evaluate('_mdAddService()');
  const id = f.evaluate('_mdNew.id');
  f.touch(id, { name: 'Pending service' });
  f.evaluate(`_mdMyModels.push({providerId:${JSON.stringify(id)}, name:'pending-model', localOnly:true});
    _mdOpen = 'device:custom-private'; _mdEditor = '';`);
  if (kind === 'connection') await f.evaluate('_mdDoTest({getAttribute:()=>"device:custom-private"})');
  else await f.evaluate('_mdCheckModel("device:custom-private", "private-model")');
  assert.equal(probes, 1);
  assert.equal(f.state.localInputs.length, 0);
  assert.equal(f.snapshot().revision, 1);
  assert.equal(f.evaluate(`_mdMyModels.some(row=>row.providerId===${JSON.stringify(id)})`), true);
  assert.equal(f.evaluate('_mdDirty'), true);
});

test('a saved local service that requires a missing key is not presented as locally available', async t => {
  const f = await fixture(t);
  f.touch('device:custom-private', { authMode: 'key' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.evaluate('_mdState(_mdProvider("device:custom-private"))'), 'open');
});

for (const baseUrl of ['http://', 'not-a-url']) test(`an invalid API address (${baseUrl}) reports its field before writing`, async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  f.touch(f.evaluate('_mdNew.id'), { name: 'Invalid address', baseUrl });
  assert.equal(await f.evaluate('_mdSave(false)'), false);
  assert.equal(f.state.localInputs.length, 0);
  assert.match(f.body.innerHTML, /data-purl="[^"]+"[^>]*aria-invalid="true"/);
  assert.match(f.body.innerHTML, /id="md-url-error"[^>]*>[^<]+/);
  assert.deepEqual(f.state.notices, []);
});

test('reloading a saved service clears validation for an invalid edit that was discarded', async t => {
  const f = await fixture(t);
  f.touch('device:custom-private', { baseUrl: 'invalid' });
  assert.equal(await f.evaluate('_mdSave(false)'), false);
  assert.match(f.body.innerHTML, /aria-invalid="true"/);
  f.evaluate('closeModelsPanel(); _mdEditor = "";');
  assert.equal(await f.evaluate('_mdLoad()'), true);
  assert.equal(f.evaluate('_mdProvider("device:custom-private").baseUrl'), 'http://127.0.0.1:1/v1');
  assert.equal(f.button.textContent, '已保存');
  assert.doesNotMatch(f.body.innerHTML, /aria-invalid="true"/);
});

for (const patch of [{ name: 'n'.repeat(65) }, { baseUrl: 'https://fixture.invalid/v1/'.padEnd(401, 'x') }])
test(`an oversized draft ${Object.keys(patch)[0]} cannot block another service save`, async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService()');
  f.touch(f.evaluate('_mdNew.id'), { name: 'Draft service', baseUrl: 'https://fixture.invalid/v1', ...patch });
  f.evaluate('_mdOpen = "managed"; _mdEditor = "";');
  f.touch('managed', { baseUrl: 'https://edited.fixture.invalid/v1' });
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.state.localInputs.length, 0);
  assert.equal(f.state.hubInputs.length, 1);
  assert.equal(f.evaluate('_mdDirty'), true);
});

test('a child editor resolves the complete device provider ID when there is no active selection', async t => {
  const f = await fixture(t);
  f.evaluate('_mdAddService(); _mdOpen = ""; _mdRender();');
  assert.equal(f.evaluate('_mdOpen'), f.evaluate('_mdNew.id'));
  assert.match(f.evaluate('_mdOpen'), /^device:custom-/);
});

test('saved device credentials retain local status and local key controls', async t => {
  const f = await fixture(t);
  f.touch('device:custom-private', { authMode: 'key' });
  f.evaluate('_mdKeyEdits["device:custom-private"] = "synthetic-device-key"');
  assert.equal(await f.evaluate('_mdSave(false)'), true);
  assert.equal(f.evaluate('_mdState(_mdProvider("device:custom-private"))'), 'local');
  assert.match(f.body.innerHTML, /移除本地密钥/);
  assert.match(f.body.innerHTML, /本地访问密钥已配置/);
  assert.doesNotMatch(f.body.innerHTML, /当前使用这个模型的个人 Key/);
});
