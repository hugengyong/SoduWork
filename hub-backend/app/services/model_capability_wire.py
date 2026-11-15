"""Strict, bounded Chat Completions evidence parsing; diagnostics contain only codes."""
from __future__ import annotations

import codecs
import json
import re


def compatibility(raw=None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {**({"maxTokensField": raw["maxTokensField"]}
               if raw.get("maxTokensField") in ("max_tokens", "max_completion_tokens") else {}),
            **({"toolChoice": raw["toolChoice"]} if raw.get("toolChoice") in ("auto", "named") else {}),
            **({"streamOptions": raw["streamOptions"]} if type(raw.get("streamOptions")) is bool else {})}


class ProbeIssue(Exception):
    def __init__(self, code, status="inconclusive"):
        super().__init__(code)
        self.code, self.status = code, status


def error_fields(error) -> dict:
    try:
        payload = json.loads(error.body)
    except (ValueError, TypeError):
        return {}
    value = payload.get("error") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def optional_parameter(error) -> str:
    if error.status not in (400, 422):
        return ""
    fields = error_fields(error)
    code = str(fields.get("code") or fields.get("type") or "").lower()
    text = str(fields.get("message") or "").lower()
    param = str(fields.get("param") or "").lower()
    allowed = ("max_tokens", "max_completion_tokens", "tool_choice", "stream_options")
    if code in ("unsupported_parameter", "unsupported_value", "unknown_parameter", "unrecognized_parameter"):
        if param in allowed:
            return param
    # Some gateways omit param/code but return a structured, explicit refusal.
    if re.search(r"not support|unsupported|unrecognized|unknown (?:parameter|argument)", text):
        for candidate in allowed:
            if re.search(r"(?<![a-z_])" + candidate + r"(?![a-z_])", text):
                return candidate
    return ""


def failure(error, check) -> tuple[str, str]:
    fields = error_fields(error)
    if str(fields.get("code") or "") in ("insufficient_quota", "insufficient_balance",
            "billing_hard_limit_reached", "billing_not_active", "1113"):
        return "failed", "billing_unavailable"
    if error.status in (408, 429, 500, 502, 503, 504):
        return "inconclusive", "timeout" if error.status in (408, 504) else "temporarily_unavailable"
    if error.status in (401, 403):
        return "failed", "credentials_or_access"
    if error.status == 404:
        return "failed", "model_or_endpoint_missing"
    if error.status in (400, 422):
        fields = error_fields(error)
        code = str(fields.get("code") or "").lower()
        param = str(fields.get("param") or "").lower()
        text = str(fields.get("message") or "").strip().lower()
        # Optional controls never establish that their associated feature is absent.
        if optional_parameter(error) or any(value in text for value in ("tool_choice", "stream_options")):
            return "inconclusive", "parameter_not_supported"
        codes = {"imageInput": {"image_not_supported", "unsupported_image", "unsupported_image_input", "vision_not_supported", "image_input_not_supported"},
                 "toolCalling": {"tools_not_supported", "tool_calling_not_supported", "function_calling_not_supported", "unsupported_tools"},
                 "streaming": {"streaming_not_supported", "stream_not_supported", "unsupported_streaming"}}
        patterns = {"imageInput": r"(?:image(?:s| input)?|vision|multimodal input) (?:is |are )?not supported(?: by (?:this |the )?model)?[.!]?",
                    "toolCalling": r"(?:tools|tool calling|function calling) (?:is |are )?not supported(?: by (?:this |the )?model)?[.!]?",
                    "streaming": r"streaming (?:is )?not supported(?: by (?:this |the )?model)?[.!]?"}
        primary = {"imageInput": {"image", "image_url"}, "toolCalling": {"tools", "functions"}, "streaming": {"stream"}}
        if (code in codes.get(check, set())
                or code == "unsupported_parameter" and param in primary.get(check, set())
                or check in patterns and re.fullmatch(patterns[check], text)):
            return "unsupported", "capability_not_supported"
    return "inconclusive", "request_not_accepted"


def meaningful(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


class StreamEvidence:
    """Incremental UTF-8/SSE decoder including CR, CRLF, data folding and comments."""
    def __init__(self, limit):
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.limit, self.size = limit, 0
        self.line, self.data, self.event = [], [], ""
        self.after_cr = False
        self.observed = self.reasoning = self.terminal = self.done = self.finished = False
        self.finish_reason = None
        self.calls, self.content, self.thoughts = {}, [], []

    def feed(self, raw):
        self.size += len(raw)
        if self.size > self.limit:
            raise ProbeIssue("response_limit")
        try:
            self._text(self.decoder.decode(raw))
        except UnicodeDecodeError as exc:
            raise ProbeIssue("invalid_response") from exc

    def _text(self, text):
        for char in text:
            if self.after_cr:
                self.after_cr = False
                if char == "\n":
                    continue
            if char in "\r\n":
                self._line()
                self.after_cr = char == "\r"
            else:
                self.line.append(char)

    def _line(self):
        line = "".join(self.line)
        self.line.clear()
        if not line:
            self._event()
        elif line.startswith(":"):
            return
        else:
            key, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if key == "data":
                self.data.append(value)
            elif key == "event":
                self.event = value

    def _event(self):
        kind, self.event = self.event, ""
        data, self.data = "\n".join(self.data), []
        if kind == "error":
            raise ProbeIssue("stream_error")
        if not data:
            return
        if kind in ("ping", "heartbeat"):
            try:
                heartbeat = json.loads(data)
            except ValueError:
                return
            if isinstance(heartbeat, dict) and heartbeat.get("error") is not None:
                raise ProbeIssue("stream_error")
            return
        if self.done:
            raise ProbeIssue("invalid_response")
        if data.strip() == "[DONE]":
            # The Chat Completions dialect defines this terminal marker.
            self.terminal = self.done = True
            return
        try:
            value = json.loads(data)
        except ValueError as exc:
            raise ProbeIssue("invalid_response") from exc
        if not isinstance(value, dict):
            raise ProbeIssue("invalid_response")
        if value.get("error") is not None or value.get("type") == "error":
            raise ProbeIssue("stream_error")
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProbeIssue("invalid_response")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ProbeIssue("invalid_response")
            if choice.get("index") not in (None, 0):
                raise ProbeIssue("invalid_response")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict) or self.finished and delta:
                raise ProbeIssue("invalid_response")
            reason = choice.get("finish_reason")
            if reason in ("length", "max_tokens", "max_output_tokens"):
                raise ProbeIssue("response_truncated")
            if reason is not None:
                if reason not in ("stop", "tool_calls", "function_call", "content_filter"):
                    raise ProbeIssue("invalid_response")
                if reason == "content_filter":
                    raise ProbeIssue("content_filtered")
                self.finish_reason = reason
                self.terminal = self.finished = True
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ProbeIssue("invalid_response")
                self.content.append(content)
            thought = any(meaningful(delta.get(key)) for key in ("reasoning_content", "reasoning", "thinking"))
            self.reasoning = self.reasoning or thought
            if isinstance(delta.get("reasoning_content"), str):
                self.thoughts.append(delta["reasoning_content"])
            calls = delta.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ProbeIssue("invalid_response")
            self._tool_deltas(calls)
            tool_delta = any(isinstance(call, dict) and isinstance(call.get("function"), dict)
                             and any(meaningful(call["function"].get(key)) for key in ("name", "arguments"))
                             for call in calls) if isinstance(calls, list) else False
            self.observed = self.observed or thought or meaningful(delta.get("content")) or tool_delta

    def _tool_deltas(self, parts):
        for part in parts:
            if not isinstance(part, dict):
                raise ProbeIssue("invalid_response")
            index = part.get("index", 0)
            if type(index) is not int or not 0 <= index < 16 or len(parts) > 1 and "index" not in part:
                raise ProbeIssue("invalid_response")
            if part.get("type") not in (None, "function"):
                raise ProbeIssue("invalid_response")
            function = part.get("function") or {}
            if not isinstance(function, dict):
                raise ProbeIssue("invalid_response")
            call = self.calls.setdefault(index, {"id": "", "type": "function",
                "function": {"name": "", "arguments": ""}})
            for target, source, key, limit in ((call, part, "id", 200),
                    (call["function"], function, "name", 128),
                    (call["function"], function, "arguments", self.limit)):
                fragment = source.get(key)
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise ProbeIssue("invalid_response")
                    target[key] += fragment
                    if len(target[key]) > limit:
                        raise ProbeIssue("response_limit")

    def message(self) -> dict:
        result = {"role": "assistant", "content": "".join(self.content) or None}
        if self.calls:
            result["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        if self.thoughts:
            result["reasoning_content"] = "".join(self.thoughts)
        return result

    def finish(self) -> tuple[bool, bool]:
        try:
            self._text(self.decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise ProbeIssue("invalid_response") from exc
        # A trailing unterminated data frame is truncated, even after prior text.
        if self.line or self.data:
            raise ProbeIssue("stream_incomplete")
        if not self.terminal:
            raise ProbeIssue("stream_incomplete")
        if not self.observed:
            raise ProbeIssue("expected_output_missing")
        return self.observed, self.reasoning
