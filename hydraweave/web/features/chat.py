"""Same-origin chat jobs for routers launched by this portal.

The browser names a profile and sends bounded text messages. Router addresses,
credentials and occupied machines are resolved on the server through the same
frozen-target contract as benchmarks. Jobs share admission, cancellation,
deadlines and browser ownership with other interactive inference work.
"""
import json
import time

from hydraweave.bench import client, targets
from hydraweave.web.api import JobFailed, redact
from . import ops


MAX_MESSAGES = 32
MAX_MESSAGE_CHARS = 32768
MAX_BODY_BYTES = 48 * 1024
MAX_TOKENS = 2048
MAX_REPLY_CHARS = 128 * 1024
DEADLINE_S = 600


def document(api, query):
    ops.strict(query, ())
    profiles = []
    for row in targets.listing(api)[0]:
        if row["kind"] != "profile" or row["state"] != "running":
            continue
        result = {"id": row["id"], "endpoint": None, "requires_auth": None, "why": None}
        try:
            material, _ = targets.snapshot(api, ("profile", row["id"]))
            targets._split_url(material["url"])
            result.update(endpoint=material["url"].rstrip("/"), requires_auth=bool(material["key_name"]))
        except (ValueError, LookupError, PermissionError) as exc:
            result["why"] = str(exc)
        profiles.append(result)
    return api.document("chat", profiles=profiles,
                        limits={"messages": MAX_MESSAGES, "message_chars": MAX_MESSAGE_CHARS,
                                "body_bytes": MAX_BODY_BYTES, "max_tokens": MAX_TOKENS},
                        delivery="completed-response", note="The portal sends requests to its own running profiles. "
                        "The complete response appears when generation finishes; Stop cancels the server request.")


def validate(api, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("chat takes an object with profile, messages and optional max_tokens")
    ops.strict(arguments, ("profile", "messages", "max_tokens"), ("profile", "messages"))
    parsed = targets.parse({"profile": arguments["profile"]})
    messages = arguments["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError(f"messages must contain 1..{MAX_MESSAGES} text messages; clear the conversation to start again")
    checked = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("each message needs only role and content")
        role, content = message["role"], message["content"]
        if role not in ("system", "user", "assistant") or role == "system" and index != 0:
            raise ValueError("message roles are user, assistant, and an optional first system message")
        if not isinstance(content, str) or not 1 <= len(content) <= MAX_MESSAGE_CHARS:
            raise ValueError(f"message content must be text of 1..{MAX_MESSAGE_CHARS} characters")
        checked.append({"role": role, "content": content})
    if checked[-1]["role"] != "user" or not checked[-1]["content"].strip():
        raise ValueError("the last message must be a nonempty user message")
    if len(json.dumps(checked, ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("conversation exceeds 48 KiB; clear the conversation to start again")
    maximum = arguments.get("max_tokens", 512)
    if type(maximum) is not int or not 1 <= maximum <= MAX_TOKENS:
        raise ValueError(f"max_tokens must be an integer in 1..{MAX_TOKENS}")
    targets.describe(api, parsed)
    material, digest = targets.snapshot(api, parsed)
    return {"profile": parsed[1], "messages": checked, "max_tokens": maximum,
            "target_digest": digest, "resources": material["resources"]}


def resources(api, arguments):
    # The admitted set is the same snapshot whose digest the job rechecks before
    # connecting. Re-reading a changed placement here could hold the wrong nodes.
    return set(arguments["resources"])


def run(api, arguments, ctx):
    if ctx.cancelled():
        return None
    resolved = targets.resolve(api, ("profile", arguments["profile"]), frozen=arguments["target_digest"])

    def safe(value):
        # Also remove a credential echoed by a broken upstream as ordinary text,
        # where structured redaction alone cannot recognise it as a secret.
        text = str(value)
        if resolved.endpoint.key:
            text = text.replace(resolved.endpoint.key, "[redacted]")
        return redact(text)

    ctx("Reading the model served by this profile")
    try:
        model, _, _ = targets.probe_model(resolved, ctx.cancelled)
        if not isinstance(model, str) or not 1 <= len(model) <= 200:
            raise ValueError("the router returned an invalid model id")
        if ctx.cancelled():
            return None
        ctx("Generating a response; the complete answer appears when generation finishes")
        received = 0

        def delta(piece, thought, now):
            nonlocal received
            received += len(piece) + len(thought)
            if received > MAX_REPLY_CHARS:
                raise client.ClientError("chat response exceeds its size limit")

        reply = client.chat(resolved.endpoint, {"model": model, "messages": arguments["messages"],
                            "max_tokens": arguments["max_tokens"], "temperature": 0, "stream": True,
                            "stream_options": {"include_usage": True}},
                            timeout=max(0.001, ctx.deadline - time.monotonic()),
                            cancelled=ctx.cancelled, on_delta=delta)
    except (client.ClientError, ValueError, LookupError) as exc:
        if ctx.cancelled():
            return None
        raise JobFailed(safe(exc)) from None
    if ctx.cancelled() or reply.cancelled:
        return None
    if not reply.ok:
        raise JobFailed(safe(reply.error or "chat request failed"))
    if len(reply.text) > MAX_REPLY_CHARS:
        raise JobFailed("chat response exceeds its size limit")
    if not reply.text:
        raise JobFailed("The model returned no answer text before completion; try a shorter prompt or a larger output limit.")
    return {"profile": arguments["profile"], "model": safe(model),
            "message": {"role": "assistant", "content": safe(reply.text)},
            "finish_reason": safe(reply.finish_reason)[:64] if reply.finish_reason else None,
            "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}


def register(reg):
    reg.get("chat", document)
    reg.job("chat/send", run, validate=validate, admission="bench", resources=resources, deadline_s=DEADLINE_S)
    reg.capability("chat/send", True, "Chat through the portal's running profile routers, with server-side credentials")
