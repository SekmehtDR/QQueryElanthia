import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings, WebCfg
from epedia.core import memory
from epedia.db import connect
from epedia.frontends.web import access
from epedia.frontends.web.app import create_app

CODE = "correct horse battery"
TUNNEL = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc123", "x-forwarded-proto": "https"}  # what a Cloudflare tunnel adds
LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}


def make(tmp_path, code=CODE):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    s.web.access_code = code
    return s


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make(tmp_path)), base_url="https://testserver") as c:
        yield c


def test_the_access_code_must_be_long_enough():
    with pytest.raises(ValueError):
        WebCfg(access_code="short")
    assert WebCfg(access_code="  long enough code  ").access_code == "long enough code"
    assert WebCfg(access_code="   ").access_code is None


def test_the_owner_at_the_computer_needs_no_login_but_a_visitor_through_the_tunnel_does(client):
    assert client.get("/api/persona").status_code == 200  # local: no tunnel headers
    page = client.get("/", headers=TUNNEL)
    assert page.status_code == 401 and "Access code" in page.text  # the sign-in page, not the chat
    assert client.get("/api/persona", headers=TUNNEL).status_code == 401
    assert client.get("/static/render.js", headers=TUNNEL).status_code == 200  # portraits and scripts are not secret


def test_the_right_code_signs_a_visitor_in_and_a_wrong_one_does_not(client):
    assert client.post("/login", json={"code": "nope nope nope"}, headers=TUNNEL).status_code == 401
    ok = client.post("/login", json={"code": CODE}, headers=TUNNEL)
    assert ok.status_code == 200 and access.AUTH_COOKIE in ok.headers["set-cookie"]
    assert "HttpOnly" in ok.headers["set-cookie"] and "Secure" in ok.headers["set-cookie"]  # https came through the tunnel
    assert client.get("/api/persona", headers=TUNNEL).status_code == 200  # the cookie is now sent along


def test_guessing_the_code_is_stopped_after_a_few_tries(client):
    for _ in range(5):
        assert client.post("/login", json={"code": "wrong wrong"}, headers=TUNNEL).status_code == 401
    assert client.post("/login", json={"code": CODE}, headers=TUNNEL).status_code == 429  # even the right one waits
    other = {**TUNNEL, "cf-connecting-ip": "198.51.100.9"}
    assert client.post("/login", json={"code": CODE}, headers=other).status_code == 200  # another address is unaffected


def test_visitors_cannot_reach_the_index_controls_or_status_even_signed_in(client):
    client.post("/login", json={"code": CODE}, headers=TUNNEL)
    for path in ("/api/index/info", "/api/status", "/api/index/changes/1"):
        assert client.get(path, headers=TUNNEL).status_code == 403
    for path, body in (("/api/index/start", {"mode": "delta"}), ("/api/index/clear", {"confirm": "CLEAR"}), ("/api/index/stop", None)):
        r = client.post(path, json=body, headers={**TUNNEL, "X-QQE": "1", "Host": "localhost:8765"})
        assert r.status_code == 403, path  # a forged Host header does not make a tunnelled request local
    assert client.get("/api/index/info").status_code == 200  # the owner still can


def test_visitors_see_no_internals_in_the_version_or_persona(client):
    client.post("/login", json={"code": CODE}, headers=TUNNEL)
    p = client.get("/api/persona", headers=TUNNEL).json()
    assert p["local"] is False and set(p["build"]) == {"version", "commit"}
    assert set(client.get("/api/version", headers=TUNNEL).json()) == {"version", "commit"}
    mine = client.get("/api/persona").json()
    assert mine["local"] is True and "chat_model" in mine["build"]


def test_each_visitor_has_private_chats_the_owner_and_other_visitors_cannot_see(tmp_path):
    s = make(tmp_path)
    c = connect(s)
    mine = memory.new_session(c, "web", owner="local")
    msg = memory.add_message(c, mine, "user", "my private question")
    memory.note_plan(c, msg, "my private question?", "search", "general")
    c.close()
    app = create_app(s)
    with TestClient(app, base_url="https://testserver") as a, TestClient(app, base_url="https://testserver") as b:
        for cl in (a, b):
            cl.post("/login", json={"code": CODE}, headers=TUNNEL)
        sid = a.post("/api/sessions", headers=TUNNEL).json()["session_id"]
        conn = connect(s)
        memory.add_message(conn, sid, "user", "visitor A asks something")
        conn.close()
        # through the tunnel there are no Past chats at all, not even one's own; the start screen's question list is the
        # same as the owner's (their Past chats), and a visitor's own questions are not in it
        for cl in (a, b):
            assert cl.get("/api/sessions", headers=TUNNEL).status_code == 403
            assert cl.get("/api/recent-questions", headers=TUNNEL).json() == {"questions": ["my private question?"]}
        assert a.get(f"/api/sessions/{sid}/messages", headers=TUNNEL).status_code == 403
        assert b.get(f"/api/sessions/{mine}/messages", headers=TUNNEL).status_code == 403  # nor the owner's
        assert b.delete(f"/api/sessions/{sid}", headers={**TUNNEL, "X-QQE": "1"}).status_code == 404
        assert a.delete(f"/api/sessions/{sid}", headers={**TUNNEL, "X-QQE": "1"}).status_code == 200
        owner_view = a.get("/api/sessions")  # the same client, now without tunnel headers: the owner at the computer
        assert owner_view.status_code == 200 and [r["title"] for r in owner_view.json()] == ["my private question"]
        assert a.get("/api/recent-questions").json() == {"questions": ["my private question?"]}


def test_delete_all_clears_every_page_chat_and_only_from_this_computer(tmp_path):
    s = make(tmp_path)
    c = connect(s)
    for _ in range(3):
        memory.add_message(c, memory.new_session(c, "web", owner="local"), "user", "mine")
    visitor = memory.new_session(c, "web", owner="someone-else")
    cli = memory.new_session(c, "cli")
    c.close()
    with TestClient(create_app(s), base_url="https://testserver") as cl:
        assert cl.delete("/api/sessions").status_code == 403  # no X-QQE: another site cannot wipe them
        cl.post("/login", json={"code": CODE}, headers=TUNNEL)
        assert cl.delete("/api/sessions", headers={**TUNNEL, **LOCAL}).status_code == 403  # nor a visitor
        rebound = {"X-QQE": "1", "Host": "evil.example:8765", "Origin": "http://evil.example:8765"}
        assert cl.delete("/api/sessions", headers=rebound).status_code == 403  # nor a page rebound to 127.0.0.1
        assert len(cl.get("/api/sessions").json()) == 3  # (the visitor's has no question, so it is not listed)
        assert cl.delete("/api/sessions", headers=LOCAL).json() == {"deleted": 4}  # every page chat, visitors' too
        assert cl.get("/api/sessions").json() == []
    c = connect(s)
    left = {r[0] for r in c.execute("SELECT session_id FROM sessions")}
    assert visitor not in left and left == {cli}  # the terminal's own conversations are not the page's
    assert c.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    c.close()


def test_recent_questions_are_everyones_newest_first_and_go_with_deleted_chats(tmp_path):
    s = make(tmp_path)
    c = connect(s)

    def asked(sid, text, standalone, mode="search", kind="general"):
        msg = memory.add_message(c, sid, "user", text)
        memory.note_plan(c, msg, standalone, mode, kind)

    one = memory.new_session(c, "web", owner="local")
    asked(one, "How do I start forging?", "How do I start forging?", kind="crafting")
    asked(one, "What tools do I need for it?", "What tools do I need for forging?", kind="crafting")
    asked(one, "hi fennick", "hi fennick", kind="meta")  # small talk
    asked(one, "what time is it", "what time is it", mode="clock")
    asked(one, "Who is Zarnak?", "Who is Zarnak?", mode="declined")
    asked(one, "are you sure?", "What tools do I need for forging?", mode="recheck")
    two = memory.new_session(c, "web", owner="local")
    asked(two, "how do i START forging", "How do I start  forging?")  # asked again: shown once, as asked last
    asked(two, "x" * 200, "x" * 200)  # a pasted essay
    visitor = memory.new_session(c, "web", owner="someone-else")
    asked(visitor, "Is a visitor's question shown?", "Is a visitor's question shown?")
    cli = memory.new_session(c, "cli")
    asked(cli, "From the terminal", "From the terminal?")  # the page shows what was asked on the page
    # everything asked on the page, visitors' questions included; not the terminal's
    assert memory.recent_user_questions(c) == ["Is a visitor's question shown?", "How do I start forging?",
                                               "What tools do I need for forging?"]
    assert memory.recent_user_questions(c, limit=1) == ["Is a visitor's question shown?"]
    memory.delete_session(c, two)
    memory.delete_session(c, one)  # purged chats take their questions with them
    c.close()
    with TestClient(create_app(s), base_url="https://testserver") as cl:
        cl.post("/login", json={"code": CODE}, headers=TUNNEL)
        for headers in ({}, TUNNEL):  # the owner and a visitor see the same list
            assert cl.get("/api/recent-questions", headers=headers).json() == {"questions": ["Is a visitor's question shown?"]}
        # the owner can open the visitor's chat (to read and clean up); a visitor cannot list or open chats at all
        listed = cl.get("/api/sessions").json()
        assert [(r["title"], r["owner"]) for r in listed] == [("Is a visitor's question shown?", "someone-else")]
        got = cl.get(f"/api/sessions/{visitor}/messages").json()
        assert got["owner"] == "someone-else" and got["messages"][0]["content"] == "Is a visitor's question shown?"
        assert cl.get(f"/api/sessions/{visitor}/messages", headers=TUNNEL).status_code == 403
        assert cl.delete(f"/api/sessions/{visitor}", headers=LOCAL).json() == {"deleted": True}
        assert cl.get("/api/recent-questions").json() == {"questions": []}  # the page then says nobody has asked


def test_a_deleted_or_foreign_chat_id_in_a_message_starts_a_fresh_conversation(tmp_path, monkeypatch):
    from epedia.core.assistant import Assistant, Done, Token

    async def fake(self, session_id, message, **kw):
        yield Token("hi")
        yield Done("hi", [], [], [], None)

    monkeypatch.setattr(Assistant, "answer_stream", fake)
    s = make(tmp_path)
    c = connect(s)
    mine = memory.new_session(c, "web", owner="local")
    c.close()
    with TestClient(create_app(s), base_url="https://testserver") as client:
        client.post("/login", json={"code": CODE}, headers=TUNNEL)
        text = client.post("/api/chat", json={"session_id": mine, "message": "hello"}, headers={**TUNNEL, "X-QQE": "1"}).text
        assert text.startswith("event: session") and mine not in text  # a visitor cannot write into the owner's chat


def test_long_messages_are_refused(tmp_path, monkeypatch):
    s = make(tmp_path)
    s.web.max_message_chars = 50
    with TestClient(create_app(s), base_url="https://testserver") as client:
        client.post("/login", json={"code": CODE}, headers=TUNNEL)
        assert client.post("/api/chat", json={"session_id": "x", "message": "y" * 51},
                           headers={**TUNNEL, "X-QQE": "1"}).status_code == 413


def test_proxy_headers_make_a_request_non_local_and_plain_loopback_local():
    class Req:
        def __init__(self, peer, headers):
            self.client = type("C", (), {"host": peer})()
            self.headers = headers

    assert access.is_local(Req("127.0.0.1", {}))
    assert not access.is_local(Req("127.0.0.1", {"cf-connecting-ip": "1.2.3.4"}))
    assert not access.is_local(Req("127.0.0.1", {"x-forwarded-for": "1.2.3.4"}))
    assert not access.is_local(Req("198.51.100.4", {}))  # a real remote peer is never local
    assert access.client_ip(Req("127.0.0.1", {"cf-connecting-ip": "1.2.3.4"})) == "1.2.3.4"
    assert access.client_ip(Req("127.0.0.1", {"x-forwarded-for": "5.6.7.8, 9.9.9.9"})) == "5.6.7.8"


def test_without_an_access_code_nothing_changes_for_the_owner(tmp_path):
    s = make(tmp_path, code=None)
    with TestClient(create_app(s), base_url="https://testserver") as client:
        assert client.get("/api/persona").status_code == 200
        assert client.get("/api/index/info").status_code == 200


def test_a_tunnelled_request_is_refused_outright_when_no_access_code_is_set(tmp_path):
    s = make(tmp_path, code=None)
    with TestClient(create_app(s), base_url="https://testserver") as client:
        assert client.get("/", headers=TUNNEL).status_code == 403  # sharing without a code fails closed
        assert client.post("/login", json={"code": "anything goes"}, headers=TUNNEL).status_code == 403
        assert client.get("/api/persona", headers=TUNNEL).status_code == 403
        assert client.get("/api/persona").status_code == 200  # the owner is unaffected
