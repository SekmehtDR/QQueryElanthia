import asyncio
from datetime import datetime, timezone

import pytest

from epedia import db
from epedia.config import Settings
from epedia.wiki import history, timeline

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    yield c
    c.close()


def put(conn, title, versions, field="guild"):
    """versions: (revid, date, user, value) oldest first."""
    conn.executemany("INSERT OR REPLACE INTO field_values(title, revid, ts, user, field, value) VALUES (?,?,?,?,?,?)",
                     [(title, r, d + "T00:00:00Z", u, field, v) for r, d, u, v in versions])


BINU = [(1, "2017-08-20", "BINU", "Necromancer"), (2, "2018-01-13", "BINU", "Warrior Necro-Mage"),
        (3, "2018-01-13", "BINU", "Necromancer"), (4, "2019-05-09", "THESCHUETZY", "Warrior Mage"),
        (5, "2025-07-30", "BINU", "Necromancer")]


def reading(conn, title, field="guild"):
    return timeline.judge(field, timeline.segments(conn, title, field), NOW)


def test_parse_fields_reads_the_infobox_and_strips_links():
    t = "{{PC |name=Binu |status=a |race=[[Human]] |gender=Male |guild=Warrior  Mage\n|instance=Prime}}"
    assert timeline.parse_fields(t) == {"guild": "Warrior Mage", "race": "Human", "gender": "Male", "status": "a"}
    assert timeline.parse_fields("no infobox")["guild"] == ""


def test_the_owner_restoring_a_value_after_another_editor_changed_it_is_credible(conn):
    put(conn, "Binu", BINU)
    r = reading(conn, "Binu")
    assert r.current == "Necromancer" and r.verdict == "credible"
    assert "after another editor" in r.why and "several times" in r.why
    assert [s.value for s in r.history][:2] == ["Necromancer", "Warrior Mage"]


def test_an_unchanged_fact_needs_no_comment(conn):
    put(conn, "Binu", [(1, "2017-08-20", "BINU", "Necromancer"), (2, "2018-01-01", "BINU", "necromancer")])
    assert reading(conn, "Binu").verdict == "unchanged"
    assert timeline.readings(conn, "Binu", NOW) == []


def test_a_change_away_from_the_owners_value_by_someone_else_is_suspect(conn):
    put(conn, "Binu", [(1, "2017-08-20", "BINU", "Warrior Mage"), (2, "2026-10-20", "Prankster", "Necromancer")])
    r = reading(conn, "Binu")
    assert r.verdict == "suspect" and "owner (BINU) last had it as Warrior Mage" in r.why


def test_a_recent_change_by_someone_else_to_a_blank_field_is_unverified(conn):
    put(conn, "Binu", [(1, "2017-08-20", "BINU", ""), (2, "2026-10-20", "Prankster", "Necromancer")])
    r = reading(conn, "Binu")
    assert r.verdict == "unverified" and "too recent" in r.why


def test_a_fact_changed_back_and_forth_by_others_is_disputed(conn):
    put(conn, "Zed", [(1, "2020-01-01", "Zed", "Cleric"), (2, "2020-02-01", "A", "Thief"),
                      (3, "2020-03-01", "B", "Cleric")])
    assert reading(conn, "Zed").verdict == "disputed"


def test_an_old_change_nobody_challenged_is_accepted(conn):
    put(conn, "Zed", [(1, "2020-01-01", "Zed", ""), (2, "2022-02-01", "A", "Thief")])
    assert reading(conn, "Zed").verdict == "accepted"


def test_owner_is_the_page_creator_or_an_editor_named_like_the_page(conn):
    conn.execute("INSERT INTO revisions(revid, title, ts, user, size) VALUES (1, 'Zed', '2019-01-01T00:00:00Z', 'Maker', 10)")
    assert timeline.owners(conn, "Zed") == {"zed", "maker"}


def test_store_and_fetch_all_reads_each_pending_character_page_once(conn):
    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, is_redirect, page_kind, updated_at) "
                 "VALUES (1, 0, 'Binu', 'binu', 5, 0, 'pc', 'x'), (2, 0, 'Sword', 'sword', 9, 0, 'item', 'x')")
    calls = []

    class Client:
        async def get(self, params):
            calls.append(params["titles"])
            revs = [{"revid": r, "timestamp": d + "T00:00:00Z", "user": u,
                     "slots": {"main": {"content": "{{PC |guild=" + v + "}}"}}} for r, d, u, v in BINU]
            return {"query": {"pages": [{"title": params["titles"], "revisions": revs}]}}

    assert timeline.pending_titles(conn) == [("Binu", 5)]
    assert asyncio.run(timeline.fetch_all(Client(), conn)) == 1
    assert calls == ["Binu"] and timeline.pending_titles(conn) == []
    assert asyncio.run(timeline.fetch_all(Client(), conn)) == 0  # nothing new: no request
    assert timeline.readings(conn, "Binu", NOW)[0].verdict == "credible"


def test_the_history_passage_carries_a_doubtful_fact_but_is_left_out_when_everything_is_trusted(conn):
    conn.executemany("INSERT INTO revisions(revid, title, ts, user, size) VALUES (?,?,?,?,?)",
                     [(10, "Binu", "2017-08-20T00:00:00Z", "BINU", 100), (11, "Binu", "2019-05-09T00:00:00Z", "THESCHUETZY", 101),
                      (12, "Binu", "2025-07-30T00:00:00Z", "BINU", 100)])
    put(conn, "Binu", BINU)
    # the creator's own value stands: nothing to hedge, so the model is not shown the margin to remark on
    assert history.passage(conn, "Binu", 10, "https://elanthipedia.play.net") is None
    conn.execute("DELETE FROM field_values WHERE title='Binu'")
    put(conn, "Binu", [(1, "2017-08-20", "BINU", "Warrior Mage"), (2, "2026-10-20", "Prankster", "Necromancer")])
    conn.execute("INSERT INTO revisions(revid, title, ts, user, size) VALUES (13, 'Binu', '2026-10-20T00:00:00Z', 'Prankster', 99)")
    p = history.passage(conn, "Binu", 10, "https://elanthipedia.play.net")
    assert p and "Key facts that changed" in p["text"] and "Guild: Necromancer: suspect" in p["text"]
    assert "Warrior Mage (BINU" in p["text"]  # a doubtful fact carries the older values so it can be cautioned


def test_an_owner_building_and_tidying_their_own_page_is_not_a_dispute(conn):
    # Binu's real metadata: a stub grown into a page, an undo of his own edit, one other editor's one-byte change,
    # and his own manual revert of it
    rows = [(1, "2017-04-24", "BINU", 89, "", ""), (2, "2017-08-20", "BINU", 130, "", ""),
            (3, "2017-08-20", "BINU", 143, "Undo revision 474138 by [[Special:Contributions/BINU|BINU]]", ""),
            (4, "2018-01-13", "BINU", 204, "", ""), (5, "2018-01-13", "BINU", 1755, "", ""),
            (6, "2019-05-09", "THESCHUETZY", 1756, "", ""), (7, "2025-07-30", "BINU", 1755, "", "mw-manual-revert")]
    conn.executemany("INSERT INTO revisions(revid, title, ts, user, size, comment, tags) VALUES (?,?,?,?,?,?,?)",
                     [(r, "Binu", d + "T00:00:00Z", u, z, c, t) for r, d, u, z, c, t in rows])
    verdict, reasons = history.assess(history.recent_edits(conn, "Binu", 10))
    assert verdict == "mixed"  # one real revert, nothing rewritten
    assert not any("rewrite" in r for r in reasons) and "1 edit undid" in reasons[0]


def test_caveat_names_earlier_values_only_when_the_answer_states_the_fact(conn):
    put(conn, "Binu", BINU[:4] + [(5, "2026-10-20", "Prankster", "Necromancer")])
    notes = timeline.caveats(conn, "Binu", "Binu is a Necromancer [1].", NOW)
    assert len(notes) == 1 and "Warrior Mage" in notes[0] and "can't vouch" in notes[0]
    assert timeline.caveats(conn, "Binu", "Binu lives in Ta'Vaalor.", NOW) == []
    assert timeline.caveats(conn, "Binu", "Binu is a Necromancers fan.", NOW) == []  # whole words only


def test_the_owners_own_value_is_stated_plainly_with_a_that_said_note_of_what_it_used_to_be(conn):
    put(conn, "Binu", BINU)  # the owner restored Necromancer after a third party's Warrior Mage
    assert reading(conn, "Binu").verdict == "credible"
    assert timeline.caveats(conn, "Binu", "Binu is a Necromancer [1].", NOW, earlier_too=False) == []
    (note,) = timeline.caveats(conn, "Binu", "Binu is a Necromancer [1].", NOW)
    assert note.startswith("That said, my notes first had Binu's guild as Warrior Necro-Mage (2018) and Warrior Mage (2019)")
    assert "vouch" not in note and "someone else" not in note  # it is not doubted, only remembered


def test_updates_refresh_history_only_after_the_bulk_read_and_only_for_changed_characters(conn, monkeypatch):
    from contextlib import asynccontextmanager

    from epedia.db import set_state
    from epedia.wiki import sync

    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, is_redirect, page_kind, updated_at) "
                 "VALUES (1, 0, 'Binu', 'binu', 5, 0, 'pc', 'x'), (2, 0, 'Tiv', 'tiv', 6, 0, 'pc', 'x')")
    seen = []

    class Client:
        async def paginate(self, params):
            seen.append("edits")
            if False:
                yield {}

        async def get(self, params):
            seen.append(params["titles"])
            return {"query": {"pages": [{"title": params["titles"], "revisions": [
                {"revid": 1, "timestamp": "2020-01-01T00:00:00Z", "user": "A", "slots": {"main": {"content": "|guild=Cleric"}}}]}]}}

    @asynccontextmanager
    async def fake_open(cfg):
        assert cfg.fallback_source is None
        yield Client()

    monkeypatch.setattr("epedia.wiki.client.open_client", fake_open)
    s = Settings()
    asyncio.run(sync.refresh_history(s, conn, {"Binu"}))
    assert seen == []  # the bulk read has not been done: no requests
    set_state(conn, history.COMPLETE_KEY, "2026-01-01T00:00:00Z")
    asyncio.run(sync.refresh_history(s, conn, {"Binu"}))
    assert seen == ["edits", "Binu"]  # new edits, then only the character page that changed


def test_a_piped_link_in_a_field_is_read_whole():
    assert timeline.parse_fields("{{PC |guild=[[Moon Mage|Moon Mage]] |race=Elf}}")["guild"] == "Moon Mage"


def test_the_reverted_tag_marks_the_undone_edit_not_a_revert():
    assert not history.Edit("2026-01-01T00:00:00Z", "A", "", 10, 1, "mw-reverted").is_revert
    assert history.Edit("2026-01-01T00:00:00Z", "A", "", 10, 1, "mw-undo").is_revert


def test_a_page_made_by_a_script_is_not_owned_by_it_and_placeholders_are_not_owner_values(conn):
    conn.execute("INSERT INTO revisions(revid, title, ts, user, size) VALUES "
                 "(1, 'Emuin', '2009-01-01T00:00:00Z', 'Maintenance script', 5), (2, 'Emuin', '2009-02-01T00:00:00Z', 'Maker', 9)")
    assert timeline.owners(conn, "Emuin") == {"emuin", "maker"}
    put(conn, "Zed", [(1, "2009-01-01", "Zed", "n"), (2, "2020-01-01", "Other", "Necromancer")])
    assert reading(conn, "Zed").verdict != "suspect"


def test_the_assistant_volunteers_what_a_trusted_fact_used_to_be_for_pages_it_rests_on_not_passing_mentions(conn):
    from epedia.config import Settings
    from epedia.core.assistant import Assistant

    put(conn, "Binu", BINU)
    a = Assistant(Settings(), conn, None)
    passages = [{"title": "Binu", "chunk_id": 1}]
    assert a._edit_notes(passages, "What guild is Binu?", "Binu is a Necromancer [1].")[0].startswith("That said")
    # not named in the question, and the answer only mentions him without resting on his page: no note
    assert a._edit_notes(passages, "Who fought the volcano?", "Binu, a Necromancer, was there.") == []
    # an answer that cites his page rests on it, whatever the question named
    assert a._edit_notes(passages, "Who fought the volcano?", "Binu is a Necromancer [1].")[0].startswith("That said")


CRITTER = ("{{Critter |new=No |Critter Name=small tree sloth |level=32 |Province=Shard |Image=Sloth.gif"
           " |special attacks=[[Slam|slams]] its arms |desc=<!-- c --> long text {{x}} |slot={{#if:a|b}} }}\nThe sloth.")


def test_an_infobox_is_read_without_a_list_of_fields_and_decoration_is_left_out():
    got = timeline.parse_infobox(CRITTER)
    assert got["level"] == "32" and got["province"] == "Shard" and got["critter_name"] == "small tree sloth"
    assert "image" not in got and "desc" not in got and "slot" not in got  # decoration, and markup-heavy values
    assert got["special_attacks"] == "slams its arms"  # a link is flattened to its text
    assert timeline.parse_infobox("no template here") == {}


def test_a_non_character_page_keeps_only_the_changes_between_versions(conn):
    def rev(i, day, user, level):
        return {"revid": i, "timestamp": f"2026-0{day}-01T00:00:00Z", "user": user, "slots": {"main": {"content": CRITTER.replace("32", level)}}}
    revs = [rev(1, 1, "WDODD", "30"), rev(2, 2, "WDODD", "30"), rev(3, 3, "PRANK", "32"), rev(4, 4, "PRANK", "32")]
    timeline.store_fields(conn, "Small tree sloth", revs, 4, generic=True)
    rows = conn.execute("SELECT revid, value FROM field_values WHERE title='Small tree sloth' AND field='level' ORDER BY revid").fetchall()
    assert [tuple(r) for r in rows] == [(1, "30"), (3, "32")]  # the unchanged versions are not stored
    conn.execute("INSERT INTO revisions(revid, title, ts, user, size) VALUES (1,'Small tree sloth','2026-01-01T00:00:00Z','WDODD',5)")
    r = timeline.judge("level", timeline.segments(conn, "Small tree sloth", "level"), NOW)
    assert r.verdict == "suspect"  # the creator had 30; someone else made it 32


def test_an_infobox_detail_is_noted_only_when_the_answer_names_it_and_states_its_value(conn):
    put(conn, "Small tree sloth", [(1, "2026-01-01", "WDODD", "30"), (3, "2026-03-01", "PRANK", "32")], field="level")
    conn.execute("INSERT INTO revisions(revid, title, ts, user, size) VALUES (1,'Small tree sloth','2026-01-01T00:00:00Z','WDODD',5)")
    assert timeline.caveats(conn, "Small tree sloth", "It is level 32 [1].", NOW)[0].startswith("Mind you, my notes first had Small tree sloth's level as 30")
    assert timeline.caveats(conn, "Small tree sloth", "It slams for 32 damage [1].", NOW) == []  # a number that is not its level
    assert timeline.caveats(conn, "Small tree sloth", "It is hunted at level 30 [1].", NOW) == []  # not the current value


def test_pending_pages_include_infobox_kinds_with_more_than_one_edit(conn):
    for pid, title, kind in [(1, "Fire Ball", "spell"), (2, "Once", "spell"), (3, "Forging guide", "guide"), (4, "Binu", "pc")]:
        conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, is_redirect, page_kind, latest_revid, updated_at)"
                     " VALUES (?,0,?,?,0,?,9,'x')", (pid, title, title.lower(), kind))
    conn.executemany("INSERT INTO revisions(revid, title, ts, user, size) VALUES (?,?,?,?,?)",
                     [(1, "Fire Ball", "2026-01-01T00:00:00Z", "A", 1), (2, "Fire Ball", "2026-02-01T00:00:00Z", "B", 2),
                      (3, "Once", "2026-01-01T00:00:00Z", "A", 1)])
    assert [t for t, _ in timeline.pending_titles(conn)] == ["Binu", "Fire Ball"]  # not a guide, not a page never edited
    assert [t for t, _ in timeline.pending_titles(conn, others=False)] == ["Binu"]
