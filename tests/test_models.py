from agent_wire.models import WireDocument


def test_unknown_private_keys_are_dropped():
    raw = {
        "url": "https://x.com/a/status/1",
        "channel": "x",
        "body": "hello",
        "viewer": {"handle": "me"},
        "email": "me@example.com",
        "csrf_token": "abc",
        "comments": [
            {"id": "1", "body": "hi", "session_id": "s", "replies": [{"id": "2", "body": "yo"}]}
        ],
    }
    dumped = WireDocument.model_validate(raw).model_dump()
    assert not {"viewer", "email", "csrf_token"} & dumped.keys()
    assert "session_id" not in dumped["comments"][0]
    assert dumped["comments"][0]["replies"][0]["body"] == "yo"
