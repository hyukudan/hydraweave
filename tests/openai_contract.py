"""Small assertions shared by tests that exercise OpenAI-compatible responses."""


def assert_chat_response(document):
    assert isinstance(document.get("id"), str) and document["id"]
    assert document.get("object") == "chat.completion"
    assert isinstance(document.get("choices"), list) and document["choices"]
    assert isinstance(document.get("usage"), dict)
    choice = document["choices"][0]
    assert isinstance(choice.get("message"), dict)
    assert isinstance(choice["message"].get("content"), str)


def assert_sse_done(chunks):
    text = b"".join(chunks).decode("utf-8") if chunks and isinstance(chunks[0], bytes) else "".join(chunks)
    assert "data: [DONE]" in text
