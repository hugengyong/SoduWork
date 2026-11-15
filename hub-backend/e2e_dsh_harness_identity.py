"""Capture real DSH model requests without a real provider or personal state.

The supplied public adapter/runtime are read-only inputs. Standard and minimal
presets exercise fresh handovers and cold native resume with a deliberately
incorrect historical assistant introduction. Success proves request context,
not how an unmodified language model would answer an identity question.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch


OLD_INTRO = "I am the Google Harness² assistant."
FIXTURE_KEY = "dsh-identity-loopback-fixture"
FIXTURE_MODEL = "dsh-identity-fixture-model"
IDENTITY_TAG = "soduwork-current-harness"
# Complete native title prompt from the public 0.2.1-rc.2 host; an executor
# cannot bypass identity checks merely by mentioning the word "title".
TITLE_SYSTEM_SHA256 = "c7c06ab4dd220c8046e470d731ae6b3b63f24aff1f890a75fb1b092aa4952842"


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return ""


def native_title_request(body: dict) -> bool:
    messages = body.get("messages") or []
    if (len(messages) != 2 or set(body) - {"model", "messages", "stream", "stream_options", "max_tokens"}
            or any(set(message) != {"role", "content"} or not isinstance(message["content"], str) for message in messages)
            or [message["role"] for message in messages] != ["system", "user"]
            or hashlib.sha256(messages[0]["content"].encode()).hexdigest() != TITLE_SYSTEM_SHA256):
        return False
    prefix = "Generate the session title from this JSON array of human messages:\n"
    if not messages[1]["content"].startswith(prefix):
        return False
    try:
        rows = json.loads(messages[1]["content"][len(prefix):])
        return (isinstance(rows, list) and bool(rows) and all(isinstance(row, dict)
                and set(row) == {"seq", "text"} and type(row["seq"]) is int and row["seq"] > 0
                and isinstance(row["text"], str) for row in rows))
    except (ValueError, TypeError):
        return False


def proof(body: dict, preset: str, stage: str, task: str) -> dict:
    messages = body.get("messages") or []
    assert body.get("model") == FIXTURE_MODEL, "Wrong model route"
    locations, sections = [], []
    for index, message in enumerate(messages):
        for section in re.findall(
                rf"<{IDENTITY_TAG}>(.*?)</{IDENTITY_TAG}>", text(message.get("content")), re.S):
            facts = next((json.loads(line) for line in section.splitlines()
                          if line.strip().startswith('{"agentId":')), None)
            assert facts == {"agentId": "dsh", "harness": "DeepSeek Harness"}, "Wrong trusted harness facts"
            assert "not a foundation-model identity" in section, "Missing harness/model distinction"
            assert "historical or project data" in section, "Missing historical-identity boundary"
            locations.append({"role": message.get("role"), "index": index})
            sections.append(section.strip())
    assert locations, "No production execution identity reached the model request"
    required_role = "user" if preset == "minimal" else "system"
    assert any(row["role"] == required_role for row in locations), f"Identity missing from {preset} {required_role} context"
    all_text = "\n".join(text(message.get("content")) for message in messages)
    assert task in all_text, "Task history was lost"
    if stage != "seed":
        assert OLD_INTRO in all_text, "The conflicting historical introduction was not preserved"
    if stage == "resume":
        assert any(message.get("role") == "assistant" and OLD_INTRO in text(message.get("content"))
                   for message in messages), "Cold resume did not restore its native assistant history"
    assert not any("You are the Google Harness² assistant running in SoduWork." in text(message.get("content"))
                   for message in messages if message.get("role") in {"system", "developer"}), "Harness² executor prompt leaked into current DSH authority"
    assert FIXTURE_KEY not in json.dumps(body), "Fixture credential entered model messages"
    return {"identityLocations": locations, "identitySections": sorted(set(sections)),
            "taskHistoryPreserved": True, "oldIntroductionPreserved": stage != "seed",
            "nativeAssistantHistoryRestored": stage == "resume"}


def response(content: str, stream: bool) -> tuple[bytes, str]:
    if stream:
        events = [{"id": "identity-fixture", "object": "chat.completion.chunk", "created": 1,
                   "model": FIXTURE_MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                  for delta, finish in (({"role": "assistant", "content": content}, None), ({}, "stop"))]
        return ("".join("data: " + json.dumps(event) + "\n\n" for event in events)
                + "data: [DONE]\n\n").encode(), "text/event-stream"
    return json.dumps({"id": "identity-fixture", "object": "chat.completion", "created": 1,
        "model": FIXTURE_MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}}).encode(), "application/json"


def worker(spec: dict) -> dict:
    # The parent supplies an entirely synthetic environment before any service import.
    from app.services import acp_agents, acp_bridge, dsh_runtime

    env = dict(os.environ)
    acp_agents.home_env("dsh", env, spec["state"], installation_root=spec["installation"])
    acp_agents.inject_llm_env("dsh", env, {"base": spec["base"], "key": FIXTURE_KEY,
        "model": FIXTURE_MODEL, "contextWindow": 32768, "maxOutputTokens": 512})
    dsh_runtime.prepare_temp(env, spec["state"], spec["preset"], spec["workspace"])
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --require=" + spec["guard"]).strip()
    if spec.get("without_identity"):
        env.pop("SODUWORK_EXECUTION_IDENTITY", None)
    events, selections = [], []
    argv = [spec["node"], spec["adapter"], *acp_agents._agent_args("dsh", {"llm": {"base": spec["base"], "model": FIXTURE_MODEL}})]
    with patch.dict(os.environ, env, clear=True), patch.object(acp_bridge, "_emit", events.append):
        bridge = acp_bridge.AcpBridge(argv, spec["workspace"], "dsh", "readonly", spec.get("session_id", ""))
        original_request = bridge.request

        def request(method, params):
            result = original_request(method, params)
            if method == "session/new":
                selected = original_request("session/set_config_option", {
                    "sessionId": result["sessionId"], "configId": "preset", "value": spec["preset"]})
                options = selected.get("configOptions") or []
                current = next((option.get("currentValue") for option in options
                                if option.get("id") in {"agent", "preset"}), None)
                assert current == spec["preset"], "Native preset selection was not acknowledged"
                selections.append(current)
            elif method in {"session/resume", "session/load"}:
                current = next((option.get("currentValue") for option in result.get("configOptions", [])
                                if option.get("id") in {"agent", "preset"}), None)
                assert current == spec["preset"], "Cold resume did not restore the requested native preset"
                selections.append(current)
            return result

        bridge.request = request
        try:
            code = bridge.run(spec["prompt"])
            assert code == 0, "Native DSH prompt did not complete"
            assert bridge.session_id, "No native session ID"
            output = "".join(str(event.get("item", {}).get("text") or event.get("part", {}).get("text") or "")
                               for event in events)
            # The provider only emits this marker after validating the wire request.
            assert spec["expected_output"] in output, "Native output did not contain the request-validation result"
            return {"ok": True, "sessionId": bridge.session_id, "presetSelections": selections,
                    "agentExecutable": spec["node"], "adapterEntry": spec["adapter"], "exit": code}
        finally:
            bridge.close()


def isolated_environment(root: Path, node: Path) -> dict:
    home, temporary = root / "home", root / "tmp"
    home.mkdir(parents=True)
    temporary.mkdir()
    env = {"PATH": str(node.parent) + os.pathsep + os.defpath, "HOME": str(home), "USERPROFILE": str(home),
        "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary),
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"), "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"), "DSH_ACP_CACHE_DIR": str(home / "dsh-acp-cache"),
        "DSH_TELEMETRY_DISABLED": "1", "DSH_PERMISSION_MODE": "read-only",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "AUTH_SECRET": "isolated-dsh-identity-fixture-auth-value", "ADMIN_PASS": "isolated-dsh-identity-fixture-pass",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def launch(spec: dict, env: dict, timeout: int, root: Path) -> dict:
    config = root / "worker.json"
    config.write_text(json.dumps(spec), encoding="utf-8")
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(config)],
        env=env, cwd=str(Path(__file__).parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **flags)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=10)
        raise TimeoutError("Owned DSH fixture exceeded its bounded timeout") from None
    if child.returncode:
        detail = (stdout + "\n" + stderr).replace(FIXTURE_KEY, "<fixture-key>")
        raise AssertionError("DSH fixture failed: " + detail[-5000:])
    return json.loads(stdout.strip().splitlines()[-1])


def run(args, root: Path) -> dict:
    adapter, node = args.adapter_entry.resolve(strict=True), args.node.resolve(strict=True)
    manifest = adapter.parent.parent / "package.json"
    inputs = [adapter, manifest]
    report = {"ok": False, "platform": sys.platform, "cases": [], "requests": [], "auxiliaryRequests": [],
        "observedExecutorRequests": 0,
        "boundary": "Real installed DSH adapter/host and production ACP bridge/gateway; synthetic loopback HTTP model, synthetic histories and temporary homes. No production Hub, user profile, provider or natural-model identity claim."}
    report["versions"] = {"adapter": json.loads(manifest.read_text(encoding="utf-8"))["version"],
                          "node": subprocess.check_output([str(node), "--version"], text=True).strip()}
    installation = root / "installation"
    runtime = installation / "agent-runtimes/dsh-host"
    runtime.parent.mkdir(parents=True)
    if args.runtime_root:
        original_runtime = args.runtime_root.resolve(strict=True)
        inputs.append(original_runtime / "node_modules/@deepseek-ai/dsh/package.json")
        shutil.copytree(original_runtime, runtime)
    else:
        archive = args.runtime_archive.resolve(strict=True)
        inputs.append(archive)
        from app.services import dsh_runtime
        dsh_runtime._safe_extract(str(archive), str(runtime))
    report["versions"]["host"] = json.loads((runtime / "node_modules/@deepseek-ai/dsh/package.json").read_text(encoding="utf-8"))["version"]
    fingerprints = {path: file_hash(path) for path in inputs}
    guard = root / "loopback-only.cjs"
    guard.write_text("""'use strict';
const net = require('node:net'), connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const o = first && typeof first === 'object' ? first : {port:first,host:typeof args[1]==='string'?args[1]:'localhost'};
  if (o.path || !['127.0.0.1','localhost','::1'].includes(String(o.host || 'localhost')))
    throw Error('E2E_NON_LOOPBACK_CONNECTION_BLOCKED');
  return connect.apply(this,args);
};
""", encoding="utf-8")
    active, provider_errors = {}, []
    serial = itertools.count()
    wire = args.output / "wire"
    wire.mkdir()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            case = dict(active)
            valid = False
            try:
                assert self.path == "/v1/chat/completions", "Unexpected model route"
                assert self.headers.get("Authorization") == "Bearer " + FIXTURE_KEY, "Wrong fixture credential"
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                assert body.get("model") == FIXTURE_MODEL, "Wrong model route"
                # DSH titles sessions using a separate, non-executor model call.
                # Whitelist only that native auxiliary prompt, not arbitrary calls
                # missing execution identity. Give concurrent requests unique files.
                auxiliary = native_title_request(body)
                filename = f'{next(serial):02d}-{case["preset"]}-{case["stage"]}' + ('-title' if auxiliary else '') + '.json'
                (wire / filename).write_text(json.dumps(body, indent=2, ensure_ascii=False).replace(FIXTURE_KEY, "<fixture-key>"), encoding="utf-8")
                if auxiliary:
                    report["auxiliaryRequests"].append({"kind": "native-session-title", "wire": "wire/" + filename})
                    data, kind = response("DSH identity fixture", body.get("stream") is True)
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                report["observedExecutorRequests"] += 1
                evidence = proof(body, case["preset"], case["stage"], case["task"])
                assert case["expected_output"] not in json.dumps(body), "The request already contains its expected proof marker"
                report["requests"].append({"preset": case["preset"], "stage": case["stage"],
                    "wire": "wire/" + filename, "stream": body.get("stream") is True, **evidence})
                valid = True
            except Exception as exc:
                provider_errors.append({"preset": case.get("preset"), "stage": case.get("stage"),
                                        "wire": "wire/" + filename if "filename" in locals() else None, "error": str(exc)})
                body = locals().get("body", {})
            data, kind = response(case["expected_output"] if valid else "E2E_REQUEST_CONTEXT_REJECTED", body.get("stream") is True)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for preset in ("standard", "minimal"):
            folder = root / preset
            folder.mkdir()
            workspace = folder / "workspace"
            workspace.mkdir()
            task = f"verified-{preset}-delivery.txt contains the already-completed task result"
            (workspace / f"verified-{preset}-delivery.txt").write_text("completed fixture task", encoding="utf-8")
            env = isolated_environment(folder, node)
            spec = {"preset": preset, "adapter": str(adapter), "node": str(node),
                "installation": str(installation), "state": str(folder / "agent-state"),
                "workspace": str(workspace), "guard": str(guard),
                "base": f"http://127.0.0.1:{server.server_port}/v1", "without_identity": args.without_identity}
            for stage in ("fresh", "seed", "resume"):
                marker = f"DSH_WIRE_PROOF_{preset}_{stage}"
                expected = OLD_INTRO + " " + task if stage == "seed" else marker
                prompt = (f"[Historical conversation]\nuser: Complete the fixture task.\nassistant: {OLD_INTRO}\nTask evidence: {task}\n[End historical conversation]\nCurrent user request: 你是谁？"
                          if stage == "fresh" else "Remember this completed task evidence: " + task
                          if stage == "seed" else "你是谁？Continue with the existing task evidence; do not repeat completed work.")
                assert f"<{IDENTITY_TAG}>" not in prompt, "The fixture cannot inject its own trusted identity"
                active.clear()
                active.update(preset=preset, stage=stage, task=task, expected_output=expected)
                result = launch({**spec, "prompt": prompt, "expected_output": expected,
                                 "session_id": seed_session if stage == "resume" else ""}, env, args.timeout, folder)
                assert not provider_errors, provider_errors
                if stage == "seed":
                    seed_session = result["sessionId"]
                    assert seed_session != fresh_session, "A fresh seed reused another native session"
                else:
                    if stage == "fresh":
                        fresh_session = result["sessionId"]
                    if stage == "resume":
                        assert result["sessionId"] == seed_session, "Cold resume replaced its native session"
                report["cases"].append({"preset": preset, "stage": stage, **result})
            assert (workspace / f"verified-{preset}-delivery.txt").read_text(encoding="utf-8") == "completed fixture task"
        assert len(report["cases"]) == 6 and len(report["requests"]) == 6
        assert all(file_hash(path) == digest for path, digest in fingerprints.items())
        report.update(ok=True, installedInputFilesUnchanged=len(fingerprints))
    except Exception as exc:
        report["error"] = str(exc).replace(FIXTURE_KEY, "<fixture-key>")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if provider_errors:
            report["providerErrors"] = provider_errors
    report["requestStageCounts"] = {preset + "/" + stage: sum(
        row["preset"] == preset and row["stage"] == stage for row in report["requests"])
        for preset in ("standard", "minimal") for stage in ("fresh", "seed", "resume")}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--adapter-entry", type=Path)
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument("--runtime-root", type=Path)
    runtime.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--without-identity", action="store_true", help="Negative control: suppress production identity; this must fail")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.loads(args.worker.read_text(encoding="utf-8")))))
        return 0
    if not all((args.adapter_entry, args.node, args.output, args.runtime_root or args.runtime_archive)):
        parser.error("--adapter-entry, --node, --output and one runtime input are required")
    if not 10 <= args.timeout <= 180:
        parser.error("--timeout must be between 10 and 180 seconds")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sw-dsh-identity-") as directory:
        report = run(args, Path(directory).resolve())
    report["temporaryStateRemoved"] = not Path(directory).exists()
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "cases": len(report["cases"]), "output": str(args.output), "error": report.get("error")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
