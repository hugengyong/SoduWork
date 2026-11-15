"""Small real capability checks; never run generated tools or echo upstream data."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import re
import secrets
import struct
import time
import zlib

from . import model_transport
from .model_capabilities import compatibility
from .model_capability_wire import ProbeIssue, StreamEvidence, failure, meaningful, optional_parameter
from .model_errors import UpstreamError

CHECKS = ("connection", "imageInput", "toolCalling", "streaming")
TIMEOUT = 30
TOTAL_TIMEOUT = 120
MAX_CALLS, MAX_NEGOTIATIONS = 5, 2
MAX_BYTES = 256 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_challenge() -> tuple[str, str]:
    """Render a random six-digit challenge with a tiny dependency-free PNG writer."""
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    width, height = 372, 108
    pixels = bytearray(b"\xff" * width * height * 3)
    segments = ("abcedf", "bc", "abged", "abgcd", "fgbc", "afgcd", "afgecd", "abc", "abcdefg", "abfgcd")
    boxes = {"a": (10, 5, 40, 11), "b": (40, 11, 47, 46), "c": (40, 53, 47, 88),
             "d": (10, 88, 40, 95), "e": (3, 53, 10, 88), "f": (3, 11, 10, 46),
             "g": (10, 46, 40, 53)}
    for i, digit in enumerate(code):
        for part in segments[int(digit)]:
            x1, y1, x2, y2 = boxes[part]
            for y in range(y1 + 5, y2 + 5):
                for x in range(x1 + i * 60 + 7, x2 + i * 60 + 7):
                    start = (y * width + x) * 3
                    pixels[start:start + 3] = b"\x18\x24\x3a"
    rows = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return code, "data:image/png;base64," + base64.b64encode(png).decode()


def _settings(conf: dict) -> dict:
    config = conf.get("config") or {}
    return {**config, **{key: value for key, value in conf.items() if key != "config"}}


def _thinking(conf: dict) -> dict:
    from .harness2_provider import thinking_options
    settings = _settings(conf)
    options = thinking_options(settings)
    # These formats use the same explicit provider switch as the desktop.
    mode, fmt = settings.get("thinkingMode"), settings.get("thinkingFormat")
    enabled = mode == "always" or settings.get("thinkingAlwaysOn") is True
    disabled = mode in ("none", "controllable")
    if enabled or disabled:
        if fmt in ("zai", "deepseek"):
            options["thinking"] = {"type": "enabled" if enabled else "disabled"}
            if fmt == "zai":
                options["thinking"]["clear_thinking"] = False
        elif fmt == "qwen":
            options["enable_thinking"] = enabled
        elif fmt in ("qwen-chat-template", "chat-template", "baseten"):
            options["chat_template_kwargs"] = {"enable_thinking": enabled}
            if fmt == "qwen-chat-template":
                options["chat_template_kwargs"]["preserve_thinking"] = True
        elif fmt == "string-thinking":
            options["thinking"] = "enabled" if enabled else "disabled"
        elif fmt == "ant-ling":
            options["thinking"] = {"type": "enabled" if enabled else "disabled"}
        elif fmt in ("openrouter", "together"):
            options["reasoning"] = {"enabled": enabled}
            if fmt == "openrouter" and enabled:
                options["reasoning"]["effort"] = "low"
    # Saved explicit provider defaults take precedence over derived switches.
    defaults = settings.get("defaults") or {}
    options.update({key: value for key, value in defaults.items() if key in {
        "thinking", "enable_thinking", "chat_template_kwargs", "reasoning", "reasoning_effort"}})
    return options


def _payload(conf: dict, check: str) -> tuple[dict, str]:
    expected = ""
    prompt = "Reply with the single word OK."
    settings = _settings(conf)
    compat = compatibility(conf.get("compatibility"))
    output = compat.get("maxTokensField") or ("max_completion_tokens" if settings.get("reasoning") is True
        and settings.get("thinkingFormat") == "openai" else "max_tokens")
    limit = settings.get("maxOutputTokens") or 65536
    headroom = 2048 if settings.get("thinkingMode") == "always" or settings.get("thinkingAlwaysOn") else 512
    budget = min(headroom, max(1, int(limit)), max(1, int(settings.get("contextWindow") or 128000)))
    stream = check in ("streaming", "toolCalling")
    body = {"model": conf["model"], output: budget, "stream": stream}
    body.update(_thinking(conf))
    if stream and compat.get("streamOptions", True):
        body["stream_options"] = {"include_usage": True}
    if check == "imageInput":
        expected, image = image_challenge()
        prompt = [{"type": "text", "text": "Read the six digits in this image. Reply only with those digits."},
                  {"type": "image_url", "image_url": {"url": image}}]
    if check == "toolCalling":
        expected = secrets.token_hex(8)
        prompt = ("Call soduwork_capability_echo with value " + expected +
                  ". This is a capability check. After receiving its synthetic tool result, reply only with its receipt.")
        body.update({"tools": [{"type": "function", "function": {
            "name": "soduwork_capability_echo", "description": "Echo the supplied check value; no side effects.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                           "required": ["value"], "additionalProperties": False}}}],
            "tool_choice": {"type": "function", "function": {"name": "soduwork_capability_echo"}}})
        if compat.get("toolChoice") == "auto":
            body["tool_choice"] = "auto"
    body["messages"] = [{"role": "user", "content": prompt}]
    return body, expected


def _failure(error: UpstreamError, check: str) -> tuple[str, str]:
    return failure(error, check)


async def _stream(response, *, tools=False):
    content_type = response.headers.get("content-type", "")
    if not content_type.lower().startswith("text/event-stream"):
        raise ProbeIssue("stream_not_observed")
    evidence = StreamEvidence(MAX_BYTES)
    async for raw in response.chunks():
        evidence.feed(raw)
    observed, reasoning = evidence.finish()
    if tools:
        if evidence.finish_reason != "tool_calls" or set(evidence.calls) != {0}:
            raise ProbeIssue("invalid_response")
        return {"choices": [{"message": evidence.message(), "finish_reason": evidence.finish_reason}],
                "_reasoningStreamReported": reasoning}
    return observed, reasoning


class _Budget:
    def __init__(self):
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.calls = self.negotiations = 0
        self.variants = set()

    def timeout(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProbeIssue("budget_exhausted")
        return min(TIMEOUT, left)

    def start(self):
        if self.calls >= MAX_CALLS:
            raise ProbeIssue("budget_exhausted")
        self.calls += 1

    def negotiate(self, error, body) -> bool:
        parameter = optional_parameter(error)
        group = "output" if parameter in ("max_tokens", "max_completion_tokens") else parameter
        if (not parameter or parameter not in body or group in self.variants
                or self.negotiations >= MAX_NEGOTIATIONS):
            return False
        self.variants.add(group)
        self.negotiations += 1
        if group == "output":
            other = "max_completion_tokens" if parameter == "max_tokens" else "max_tokens"
            body[other] = body.pop(parameter)
        elif parameter == "tool_choice":
            if body[parameter] == "auto":
                return False
            body[parameter] = "auto"
        elif parameter == "stream_options":
            body.pop(parameter)
        return True


async def _exchange(conf, body, budget, working, *, stream=False, tools=False):
    budget.start()
    while True:
        response = None
        try:
            timeout = budget.timeout()
            request_deadline = time.monotonic() + timeout
            async with asyncio.timeout(timeout) as deadline:
                response = await model_transport.open_response(conf, "/chat/completions", json.dumps(body).encode(),
                    stream=stream, timeout=timeout)
                if stream:
                    data = await _stream(response, tools=tools)
                else:
                    raw = bytearray()
                    async for chunk in response.chunks():
                        if len(raw) + len(chunk) > MAX_BYTES:
                            raise ProbeIssue("response_limit")
                        raw.extend(chunk)
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("error") is not None:
                        raise ProbeIssue("invalid_response")
                    choice = (data.get("choices") or [{}])[0]
                    if choice.get("finish_reason") in ("length", "max_tokens", "max_output_tokens"):
                        raise ProbeIssue("response_truncated")
                    if not isinstance(choice.get("message"), dict):
                        raise ProbeIssue("invalid_response")
                # A transport may translate cancellation/socket closure into
                # EOF. Terminal text does not make an expired request succeed.
                if deadline.expired() or time.monotonic() >= request_deadline:
                    raise ProbeIssue("timeout")
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                outcome = getattr(response, "outcome", "completed")
                if outcome != "completed":
                    raise ProbeIssue("timeout" if outcome == "timeout" else "transport_interrupted")
                working["maxTokensField"] = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
                if "tool_choice" in body:
                    working["toolChoice"] = "named" if isinstance(body["tool_choice"], dict) else "auto"
                if stream:
                    working["streamOptions"] = "stream_options" in body
                return data
        except UpstreamError as error:
            if not budget.negotiate(error, body):
                raise
        finally:
            if response is not None:
                await response.aclose()


async def _check(conf, check, budget, working):
    body, expected = _payload({**conf, "compatibility": working}, check)
    data = await _exchange(conf, body, budget, working, stream=body["stream"], tools=check == "toolCalling")
    if check == "streaming":
        return data
    value = data["choices"][0]["message"]
    reported = bool(data.get("_reasoningStreamReported"))
    content = value.get("content")
    if check == "connection":
        return meaningful(content) or meaningful(value.get("reasoning_content")), False
    if check == "imageInput":
        return isinstance(content, str) and re.sub(r"\D", "", content) == expected, False
    calls = value.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) != 1:
        return False, reported
    call = calls[0]
    function = call.get("function") or {}
    if (call.get("type") != "function" or not meaningful(call.get("id")) or len(call["id"]) > 200
            or function.get("name") != "soduwork_capability_echo"):
        return False, reported
    arguments = json.loads(function.get("arguments") or "null")
    if not isinstance(arguments, dict) or arguments != {"value": expected}:
        return False, reported
    receipt = secrets.token_hex(12)
    assistant = {"role": "assistant", "content": content, "tool_calls": calls}
    if isinstance(value.get("reasoning_content"), str):
        assistant["reasoning_content"] = value["reasoning_content"]
    followup, _ = _payload({**conf, "compatibility": working}, "connection")
    followup["messages"] = [*body["messages"], assistant, {"role": "tool", "tool_call_id": call["id"],
        "content": json.dumps({"value": expected, "receipt": receipt})}]
    # This result is fabricated locally. No model-suggested function is executed.
    final = await _exchange(conf, followup, budget, working)
    final_choice = final["choices"][0]
    answer = final_choice["message"].get("content")
    return (isinstance(answer, str) and answer.strip() == receipt
            and final_choice.get("finish_reason") == "stop"), reported


async def run(conf: dict, checks: list[str]) -> dict:
    results, reasoning = {}, False
    working, budget = compatibility(conf.get("compatibility")), _Budget()
    for check in dict.fromkeys(checks):
        stamp = now()
        if conf.get("kind", "openai-compat") != "openai-compat":
            results[check] = {"status": "inconclusive", "code": "protocol_not_tested", "checkedAt": stamp}
            continue
        try:
            passed, reported = await _check(conf, check, budget, working)
            reasoning = reasoning or reported
            status, code = ("passed", "verified") if passed else ("inconclusive", "expected_output_missing")
        except ProbeIssue as exc:
            status, code = exc.status, exc.code
        except UpstreamError as exc:
            status, code = _failure(exc, check)
        except TimeoutError:
            status, code = "inconclusive", "timeout"
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            status, code = "inconclusive", "invalid_response"
        results[check] = {"status": status, "code": code, "checkedAt": stamp}
    return {"reportVersion": 2, "apiFamily": "chat-completions", "checkedAt": now(), "checks": results,
            "reasoningStreamReported": reasoning, "compatibility": working}
