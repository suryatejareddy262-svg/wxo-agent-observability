"""
forwarder/hook.py
=================
In-process auto-instrumentation hook for Python agent frameworks and SDKs.
Captures agent invocations and tool executions, converts them to WXO OTLP
format, and posts them to WXO Agent Analytics.

Usage:
    # Option 1: In code
    import forwarder.hook
    forwarder.hook.instrument()

    # Option 2: Zero code change via sitecustomize
    export PYTHONPATH="/path/to/src/forwarder:$PYTHONPATH"
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from forwarder.forwarder import OtlpForwarder, auth_configured, build_otlp_payload, get_bearer_token, post_to_wxo

log = logging.getLogger("wxo_otlp_hook")
_INSTRUMENTED = False
_LOCK = threading.Lock()
_forwarder = OtlpForwarder()


def instrument():
    """Monkey-patch supported SDKs (Anthropic, OpenAI) for automatic WXO tracing."""
    global _INSTRUMENTED
    with _LOCK:
        if _INSTRUMENTED:
            return
        _patch_anthropic()
        _patch_openai()
        _INSTRUMENTED = True


def _ship_trace_async(**kwargs):
    """Asynchronously format and ship trace to WXO."""
    def _worker():
        try:
            routing = _forwarder.resolve_wxo_agent(
                candidate_keys=[kwargs.get("agent_key"), kwargs.get("model_name")],
                default_name=kwargs.get("agent_name", "external_agent"),
            )
            agent_id = routing["agent_id"]
            tenant_id = routing["tenant_id"]
            export_url = routing["export_url"]
            api_key = routing["api_key"]
            token_url = routing["token_url"]

            if not agent_id or not export_url or not auth_configured(api_key):
                return

            payload = build_otlp_payload(
                agent_id=agent_id,
                agent_name=routing["agent_name"],
                tenant_id=tenant_id,
                environment=routing["environment"],
                prompt=kwargs.get("prompt", ""),
                response=kwargs.get("response", ""),
                start_ns=kwargs.get("start_ns", 0),
                end_ns=kwargs.get("end_ns", 0),
                input_tokens=kwargs.get("input_tokens", 0),
                output_tokens=kwargs.get("output_tokens", 0),
                is_error=kwargs.get("is_error", False),
                error_msg=kwargs.get("error_msg", ""),
                platform=kwargs.get("platform", "external_agent"),
                model_name=kwargs.get("model_name", ""),
                session_id=kwargs.get("session_id", ""),
                user_id=kwargs.get("user_id", ""),
                tool_calls=kwargs.get("tool_calls", []),
            )

            token = get_bearer_token(api_key, token_url, logger=log)
            if token:
                post_to_wxo(
                    payload=payload,
                    bearer_token=token,
                    export_url=export_url,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    logger=log,
                )
        except Exception as exc:
            log.debug(f"Hook trace shipping error: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


def _patch_anthropic():
    try:
        import anthropic
        from anthropic.resources import Messages
    except ImportError:
        return

    orig_create = Messages.create

    def wrapped_create(self, *args, **kwargs):
        start_ns = int(time.time() * 1e9)
        model = kwargs.get("model", "claude")
        messages = kwargs.get("messages", [])
        prompt = ""
        for m in messages:
            if isinstance(m, dict):
                content = m.get("content", "")
                if isinstance(content, str):
                    prompt += f"[{m.get('role', 'user')}]: {content}\n"
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("text"):
                            prompt += f"[{m.get('role', 'user')}]: {b.get('text')}\n"

        prompt = prompt.strip()

        try:
            resp = orig_create(self, *args, **kwargs)
        except Exception as exc:
            end_ns = int(time.time() * 1e9)
            _ship_trace_async(
                prompt=prompt,
                response=str(exc),
                model_name=model,
                start_ns=start_ns,
                end_ns=end_ns,
                is_error=True,
                error_msg=str(exc),
                platform="anthropic",
            )
            raise

        end_ns = int(time.time() * 1e9)

        if hasattr(resp, "model_dump"):
            resp_dict = resp.model_dump()
            resp_text = ""
            tool_calls = []
            for block in resp_dict.get("content", []):
                if block.get("type") == "text":
                    resp_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "name": block.get("name", "tool"),
                        "input": json.dumps(block.get("input", {})),
                        "output": "",
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                    })

            usage = resp_dict.get("usage", {})
            _ship_trace_async(
                prompt=prompt,
                response=resp_text,
                model_name=resp_dict.get("model", model),
                start_ns=start_ns,
                end_ns=end_ns,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                tool_calls=tool_calls,
                session_id=str(resp_dict.get("id", "")),
                platform="anthropic",
            )

        return resp

    Messages.create = wrapped_create
    log.info("WXO tracing hook attached to Anthropic SDK")


def _patch_openai():
    try:
        import openai
        from openai.resources.chat import Completions
    except ImportError:
        return

    orig_create = Completions.create

    def wrapped_create(self, *args, **kwargs):
        start_ns = int(time.time() * 1e9)
        model = kwargs.get("model", "gpt")
        messages = kwargs.get("messages", [])
        prompt = ""
        for m in messages:
            if isinstance(m, dict):
                prompt += f"[{m.get('role', 'user')}]: {m.get('content', '')}\n"
        prompt = prompt.strip()

        try:
            resp = orig_create(self, *args, **kwargs)
        except Exception as exc:
            end_ns = int(time.time() * 1e9)
            _ship_trace_async(
                prompt=prompt,
                response=str(exc),
                model_name=model,
                start_ns=start_ns,
                end_ns=end_ns,
                is_error=True,
                error_msg=str(exc),
                platform="openai",
            )
            raise

        end_ns = int(time.time() * 1e9)

        if hasattr(resp, "model_dump"):
            resp_dict = resp.model_dump()
            resp_text = ""
            tool_calls = []
            for choice in resp_dict.get("choices", []):
                msg = choice.get("message", {})
                if msg.get("content"):
                    resp_text += msg.get("content", "")
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    tool_calls.append({
                        "name": fn.get("name", "tool"),
                        "input": fn.get("arguments", ""),
                        "output": "",
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                    })

            usage = resp_dict.get("usage", {})
            _ship_trace_async(
                prompt=prompt,
                response=resp_text,
                model_name=resp_dict.get("model", model),
                start_ns=start_ns,
                end_ns=end_ns,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                tool_calls=tool_calls,
                session_id=str(resp_dict.get("id", "")),
                platform="openai",
            )

        return resp

    Completions.create = wrapped_create
    log.info("WXO tracing hook attached to OpenAI SDK")


# Auto-instrument on import if configured
if os.environ.get("WXO_OTEL_EXPORT_URL") or os.environ.get("WXO_AGENT_MAP"):
    instrument()
