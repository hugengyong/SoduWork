'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { readManifest, runnerPath, requiredFiles, verify } = require('../scripts/verify-runtime.cjs');

const root = path.resolve(__dirname, '..');

test('runtime manifest declares a zero-prerequisite client', () => {
  const manifest = readManifest(root);
  assert.deepEqual(manifest.hostPrerequisites, []);
  assert.equal(manifest.networkInstall, false);
  assert.equal(manifest.bundled.runner.required, true);
});

test('runner location is deterministic and never resolved from PATH', () => {
  assert.match(runnerPath(root, 'win32').replaceAll('\\', '/'), /resources\/runner\/soduwork-runner\.exe$/);
  assert.match(runnerPath(root, 'linux').replaceAll('\\', '/'), /resources\/runner\/soduwork-runner$/);
});

test('source checkout reports missing generated runner clearly', () => {
  assert.throws(() => verify(root, process.platform, { packaged: true }), /ENOENT/);
});
test('package gate rejects each missing component and accepts a complete resource tree', t => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-runtime-gate-'));
  t.after(() => { assert.equal(path.dirname(temp), os.tmpdir()); fs.rmSync(temp, { recursive: true, force: true }); });
  const resources = path.join(temp, 'resources');
  fs.mkdirSync(resources);
  const manifest = readManifest(root);
  fs.writeFileSync(path.join(resources, 'runtime-manifest.json'), JSON.stringify(manifest));
  const files = requiredFiles(manifest, 'win32');
  for (const relative of files) {
    const file = path.join(resources, relative); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, 'fixture');
  }
  assert.equal(verify(temp, 'win32', { packaged: true }).checkedFiles, files.length);
  for (const relative of files) {
    const file = path.join(resources, relative); fs.writeFileSync(file, '');
    assert.throws(() => verify(temp, 'win32', { packaged: true }), /missing or empty/);
    fs.writeFileSync(file, 'fixture');
  }
  manifest.hostPrerequisites = ['python'];
  fs.writeFileSync(path.join(resources, 'runtime-manifest.json'), JSON.stringify(manifest));
  assert.throws(() => verify(temp, 'win32', { packaged: true }), /hostPrerequisites/);
});
test('Windows npm survives electron-builder root node_modules filtering', async t => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-node-copy-'));
  t.after(() => { assert.equal(path.dirname(temp), os.tmpdir()); fs.rmSync(temp, { recursive: true, force: true }); });
  const relative = '.runtime/node/node_modules/npm/bin/npm-cli.js';
  const source = path.join(temp, relative);
  fs.mkdirSync(path.dirname(source), { recursive: true }); fs.writeFileSync(source, 'npm entry');
  const pkg = require('../package.json');
  const mapping = pkg.build.win.extraResources.find(item => item.to === 'node/node_modules');
  assert.ok(mapping, 'npm requires an explicit resource root');
  const { FileMatcher, copyFiles } = require('app-builder-lib/out/fileMatcher');
  const output = path.join(temp, 'resources', mapping.to);
  await copyFiles([new FileMatcher(path.join(temp, mapping.from), output, value => value, mapping.filter)]);
  assert.equal(fs.readFileSync(path.join(output, 'npm/bin/npm-cli.js'), 'utf8'), 'npm entry');
});

test('each supported installer resolves its own pinned Armonel from the final resources path', async t => {
  const { selectRelease, noticeNames, bundledExecutable } = require('../main/armonel-runtime.cjs');
  const { prepare } = require('../scripts/prepare-armonel-runtime.cjs');
  const pkg = require('../package.json');
  const catalog = require('../../hub-backend/app/agent_catalog.json');
  const agent = catalog.agents.find(item => item.id === 'armonel');
  assert.equal(agent.install.installer, 'bundled');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-armonel-gate-'));
  t.after(() => { assert.equal(path.dirname(temp), os.tmpdir()); fs.rmSync(temp, { recursive: true, force: true }); });
  const { FileMatcher, copyFiles } = require('app-builder-lib/out/fileMatcher');
  for (const [platform, arch, section] of [
    ['darwin', 'arm64', 'mac'], ['darwin', 'x64', 'mac'], ['linux', 'x64', 'linux'], ['win32', 'x64', 'win'],
  ]) {
    const release = selectRelease(platform, arch);
    assert.equal(agent.install.version, release.version);
    assert.equal(release.name, 'armonel-' + platform + '-' + arch);
    const mapping = pkg.build[section].extraResources.find(item => item.to === 'armonel');
    const dir = path.resolve(temp, mapping.from.replace('${arch}', arch));
    const resources = path.join(temp, release.target, platform === 'darwin' ? 'SoduWork.app/Contents/Resources' : 'resources');
    fs.mkdirSync(dir, { recursive: true });
    const names = [release.executable, ...noticeNames];
    assert.deepEqual(mapping.filter, names);
    for (const name of names) fs.writeFileSync(path.join(dir, name), release.target);
    assert.equal(bundledExecutable({ isPackaged: false, getAppPath: () => temp }, null, platform, arch), path.join(dir, release.executable));
    assert.equal(bundledExecutable({ isPackaged: true }, resources, platform, arch), null);
    await copyFiles([new FileMatcher(dir, path.join(resources, mapping.to), value => value, mapping.filter)]);
    const resolved = bundledExecutable({ isPackaged: true }, resources, platform, arch);
    assert.equal(fs.readFileSync(resolved, 'utf8'), release.target);
    const required = requiredFiles(readManifest(root), platform, arch);
    for (const name of names) assert.ok(required.includes('armonel/' + name));
  }
  const archive = path.join(temp, 'wrong.tgz');
  fs.writeFileSync(archive, 'incorrect archive');
  await assert.rejects(prepare({ root: temp, platform: 'darwin', arch: 'arm64', env: {} }), /Missing armonel-darwin-arm64/);
  await assert.rejects(prepare({ root: temp, platform: 'win32', arch: 'x64',
    env: { SODUWORK_ARMONEL_PACKAGE: archive } }), /failed SHA-256/);
  assert.equal(bundledExecutable({ isPackaged: true }, temp, 'win32', 'arm64'), null);
});

test('private registry uses the target archive, keeps the job token on GitLab and rejects corrupt downloads', async t => {
  const { EventEmitter } = require('node:events');
  const { Readable } = require('node:stream');
  const { prepare } = require('../scripts/prepare-armonel-runtime.cjs');
  const { selectRelease } = require('../main/armonel-runtime.cjs');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soduwork-armonel-download-'));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const calls = [];
  const storage = 'https://storage.example.test/download';
  t.mock.method(require('node:https'), 'get', (url, options, respond) => {
    calls.push([url, options.headers['JOB-TOKEN']]);
    const response = Readable.from([Buffer.from('unverified contents')]);
    response.statusCode = url === storage ? 200 : 302;
    response.headers = url === storage ? {} : { location: storage };
    queueMicrotask(() => respond(response));
    return Object.assign(new EventEmitter(), { setTimeout() {} });
  });
  const api = 'https://gitlab.example.test/api/v4';
  const env = { CI_API_V4_URL: api, CI_JOB_TOKEN: 'fixture-token', SODUWORK_ARMONEL_PROJECT_ID: '12' };
  await assert.rejects(prepare({ root: temp, platform: 'darwin', arch: 'arm64', env }), /SHA-256 mismatch/);
  const release = selectRelease('darwin', 'arm64');
  assert.deepEqual(calls, [
    [`${api}/projects/12/packages/generic/armonel-npm/${release.version}/${release.file}`, 'fixture-token'],
    [storage, undefined],
  ]);
  assert.deepEqual(fs.readdirSync(path.join(temp, '.runtime-cache', 'armonel')), []);
  assert.equal(fs.existsSync(path.join(temp, '.runtime', 'armonel')), false);
});
