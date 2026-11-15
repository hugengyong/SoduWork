'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { piProvider } = require('../main/conversation/model-selection');
const privacy = require('../main/pi-session/local-model-runtime-privacy.cjs');

test('Pi sends no Authorization header for a local no-key model', { timeout: 20000 }, async t => {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push(req.headers);
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.end('data: ' + JSON.stringify({ id: 'local', object: 'chat.completion.chunk', model: 'qwen3',
      choices: [{ index: 0, delta: { role: 'assistant', content: 'ok' }, finish_reason: null }] }) +
      '\n\ndata: ' + JSON.stringify({ id: 'local', object: 'chat.completion.chunk', model: 'qwen3',
        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) +
      '\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-local-transport-'));
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const sdk = await import('../node_modules/@earendil-works/pi-coding-agent/dist/index.js');
  const runtime = await sdk.ModelRuntime.create({ modelsPath: null,
    authPath: path.join(root, 'auth.json'), modelsStorePath: path.join(root, 'models.db') });
  const next = { id: 'local:model-' + '1'.repeat(32), wireModelId: 'qwen3', label: 'Qwen 3', key: '', localOnly: true,
    baseUrl: 'http://127.0.0.1:' + server.address().port + '/v1' };
  runtime.registerProvider('soduwork', piProvider(next));
  const model = privacy.configure(runtime, runtime.getModel('soduwork', next.id), next);
  const result = await runtime.stream(model, { systemPrompt: '', messages: [{ role: 'user', content: [{ type: 'text', text: 'ping' }] }] }).result();
  assert.equal(result.stopReason, 'stop');
  assert.equal(result.content[0].text, 'ok');
  assert.equal(result.model, next.id);
  assert.equal(result.responseModel, undefined);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].authorization, undefined);
});

for (const fixture of [
  { name: 'explicit Qwen format on a gateway alias', wireModelId: 'gateway-alias', thinkingFormat: 'qwen' },
  { name: 'automatic Qwen format from the wire model name', wireModelId: 'qwen3-local' },
]) test('Pi transmits native images with thinking disabled and learned compatibility: ' + fixture.name, { timeout: 20000 }, async t => {
  const requests=[];
  const server=http.createServer(async(req,res)=>{
    const parts=[];for await(const part of req)parts.push(part);requests.push(JSON.parse(Buffer.concat(parts).toString()));
    res.writeHead(200,{'content-type':'text/event-stream'});
    res.end('data: '+JSON.stringify({choices:[{index:0,delta:{content:'ok'},finish_reason:null}]})+'\n\n'+
      'data: '+JSON.stringify({choices:[{index:0,delta:{},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'soduwork-native-wire-'));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));fs.rmSync(root,{recursive:true,force:true});});
  const sdk=await import('../node_modules/@earendil-works/pi-coding-agent/dist/index.js');
  const runtime=await sdk.ModelRuntime.create({modelsPath:null,authPath:path.join(root,'auth.json'),modelsStorePath:path.join(root,'models.db')});
  const next={id:'local:model-'+'2'.repeat(32),wireModelId:fixture.wireModelId,key:'',localOnly:true,
    baseUrl:'http://127.0.0.1:'+server.address().port+'/v1',input:['text','image'],imageRouting:'native',
    thinkingMode:'none',...(fixture.thinkingFormat ? {thinkingFormat:fixture.thinkingFormat} : {}),compatibility:{maxTokensField:'max_tokens',streamOptions:false}};
  runtime.registerProvider('soduwork',piProvider(next));
  const model=privacy.configure(runtime,runtime.getModel('soduwork',next.id),next);
  const picture=require('../main/model-capability-image.cjs').challenge().url.split(',')[1];
  await runtime.stream(model,{systemPrompt:'',messages:[{role:'user',content:[{type:'text',text:'Describe the image'},
    {type:'image',data:picture,mimeType:'image/png'}]}]}).result();
  assert.equal(requests[0].enable_thinking,false);
  assert.equal(requests[0].stream_options,undefined);
  assert.equal(requests[0].messages[0].content[1].type,'image_url');
  assert.equal(requests[0].model,fixture.wireModelId);
});

test('Pi uses an actual empty tool loadout for a text-only model and restores tools for a later model', { timeout: 20000 }, async t => {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const parts = []; for await (const part of req) parts.push(part);
    requests.push(JSON.parse(Buffer.concat(parts).toString()));
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.end('data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: 'ok' }, finish_reason: null }] }) + '\n\n' +
      'data: ' + JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-tool-loadout-'));
  let session;
  t.after(async () => { session?.dispose(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); fs.rmSync(root, { recursive: true, force: true }); });
  const sdk = await import('../node_modules/@earendil-works/pi-coding-agent/dist/index.js');
  const runtime = await sdk.ModelRuntime.create({ modelsPath: null, authPath: path.join(root, 'auth.json'), modelsStorePath: path.join(root, 'models.db') });
  const next = { id: 'local:model-' + '3'.repeat(32), wireModelId: 'text-only-fixture', key: '', localOnly: true,
    baseUrl: 'http://127.0.0.1:' + server.address().port + '/v1', capabilityProfile: { toolCalling: { state: 'unsupported' } } };
  runtime.registerProvider('soduwork', piProvider(next));
  const model = privacy.configure(runtime, runtime.getModel('soduwork', next.id), next);
  const settings = sdk.SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
  const loader = new sdk.DefaultResourceLoader({ cwd: root, agentDir: root, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [{ name: 'soduwork-tool-context', hidden: true, factory: require('../main/pi-session/tool-context').extension }] });
  await loader.reload();
  const manager = sdk.SessionManager.inMemory(root);
  const usage = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
  manager.appendMessage({ role: 'user', content: 'Previous request.', timestamp: 1 });
  manager.appendMessage({ role: 'assistant', content: [{ type: 'toolCall', id: 'past-read', name: 'read', arguments: { path: 'proof.txt' } }],
    api: 'openai-completions', provider: 'soduwork', model: next.id, stopReason: 'toolUse', usage, timestamp: 2 });
  manager.appendMessage({ role: 'toolResult', toolCallId: 'past-read', toolName: 'read',
    content: [{ type: 'text', text: 'Historical safe result.' }], isError: false, timestamp: 3 });
  const archived = JSON.stringify(manager.getBranch());
  ({ session } = await sdk.createAgentSession({ cwd: root, agentDir: root, model, modelRuntime: runtime, settingsManager: settings,
    resourceLoader: loader, sessionManager: manager }));
  assert.ok(session.getActiveToolNames().length, 'The ordinary SDK session must start with its real default tools');
  const target = path.join(__dirname, '../main/pi-session/model-tools.cjs');
  assert.equal(fs.existsSync(target), true, 'Turn-scoped model tool policy must exist');
  const policy = require(target), before = session.getActiveToolNames();
  const restore = policy.prepare(session, next, false);
  assert.deepEqual(session.getActiveToolNames(), []);
  await session.prompt('Reply briefly.');
  assert.equal(requests[0].tools, undefined); assert.equal(requests[0].tool_choice, undefined);
  assert.equal(requests[0].messages.some(message => message.role === 'tool' || message.tool_calls), false);
  assert.match(JSON.stringify(requests[0].messages), /Historical safe result/);
  assert.ok(JSON.stringify(manager.getBranch()).includes('past-read'));
  assert.deepEqual(manager.getBranch().slice(0, JSON.parse(archived).length), JSON.parse(archived));
  restore(); assert.deepEqual(session.getActiveToolNames(), before);
  policy.prepare(session, { ...next, capabilityProfile: { toolCalling: { state: 'supported' } } }, false);
  await session.prompt('Reply briefly again.');
  assert.ok(requests[1].tools.length);
  assert.ok(requests[1].messages.some(message => message.tool_call_id === 'past-read'));
  assert.throws(() => policy.prepare(session, next, true), /工具/);
});
