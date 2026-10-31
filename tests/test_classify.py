from epedia.config import ChunkingCfg
from epedia.index.classify import classify_page, extract_links, soft_redirect_target
from epedia.text.chunker import chunk_page, notice_line
from epedia.text.html2md import Block


def kind(ns, title, cats=(), **kw):
    return classify_page(ns, title, list(cats), **kw).kind


def test_namespaces_decide_first():
    assert kind(120, "Post:Shield Update - 04/29/2012 - 16:40") == "post"
    assert kind(14, "Category:Berserks") == "category"
    assert kind(12, "Help:Contents") == "meta"
    assert kind(114, "Weapon:Broadsword (1)") == "item"
    assert kind(114, "Weapon:Abassi (crafted)") == "recipe"
    assert kind(116, "Title:Archwizard") == "item"


def test_main_namespace_kinds_from_categories():
    assert kind(0, "Relay", ["Player Characters", "Player Human"]) == "pc"
    assert kind(0, "Moliko", ["NPCs"]) == "npc"
    assert kind(0, "Mauriga's Botanicals", ["Shops", "Herb shops"]) == "shop"
    assert kind(0, "Crossing", ["Cities", "Zoluren cities"]) == "place"
    assert kind(0, "Musk hog", ["Bestiary"]) == "creature"
    assert kind(0, "Burn", ["Spells"]) == "spell"
    assert kind(0, "Hodierna", ["Immortals"]) == "lore"
    assert kind(0, "Forging guide", ["Crafting player guides"]) == "guide"
    assert kind(0, "Appraise command", ["Commands"]) == "command"
    assert kind(0, "Forging skill", ["Skills"]) == "skill"
    assert kind(0, "Hollow Eve Festival 450", ["Festivals"]) == "event"
    assert kind(0, "The First Land Herald/450-04-34", []) == "news"
    assert kind(208, "Concept:Elves", ["Races"]) == "race"


def test_player_character_beats_shop_and_logs_are_flagged():
    assert kind(0, "Relay", ["Shops", "Player Characters"]) == "pc"
    c = classify_page(0, "Ayrell/Logs/Wild Magic 09212024", [])
    assert c.kind == "log" and c.is_log


def test_title_patterns_and_obsolete_flag():
    assert kind(0, "Tailoring discipline") == "crafting"
    assert kind(0, "Fire of Ushnish (2.0)", ["Spells"]) == "spell"
    assert classify_page(0, "Fire of Ushnish (2.0)", ["Spells"]).is_obsolete
    assert classify_page(0, "Milgrym's Weapons (obsolete)", ["Shops"]).is_obsolete
    assert not classify_page(0, "Burn", ["Spells"]).is_obsolete


def test_subpage_inherits_parent_kind_and_soft_redirect_wins():
    assert kind(0, "Isharon/Titles", [], parent_kind="pc") == "pc"
    assert kind(0, "Misc/Notes", [], parent_kind="general") == "general"
    assert kind(0, "Shard History", ["Cities"], soft_redirect=True) == "soft_redirect"


def test_extract_links_skips_red_links_and_namespaces():
    html = (
        '<a href="/Zoluren" title="Zoluren">z</a> <a href="/Hav%27roth">h</a> '
        '<a href="/Category:Herb_shops">c</a> <a href="/File:X.png">f</a> '
        '<a href="/index.php?title=Item:Foo&amp;redlink=1" class="new">red</a> '
        '<a href="/Missing_Page" class="new" title="Missing">red2</a> <a href="/Zoluren">again</a>'
    )
    assert extract_links(html) == ["Zoluren", "Hav'roth"]


def test_extract_links_reads_every_way_the_wiki_writes_a_link():
    # item pages link through their templates as /index.php/Title; sections and ?title= forms point at pages too
    html = (
        '<a href="/index.php/Graverobber_(2)" title="Graverobber (2)">g</a> '
        '<a href="/Roars#Battle_Cries">r</a> <a href="/index.php?title=Kifa_Awrocis_(Siksraja)">k</a> '
        '<a href="/index.php?title=Svidaw_sis&amp;action=edit">edit</a> <a href="/Svidaw_sis?action=history">h</a> '
        '<a href="/index.php/Category:Weapons">c</a> <a href="/index.php/Post:Durability_2.0">p</a> '
        '<a href="/index.php">home</a> <a href="/Roars">again</a>'
    )
    assert extract_links(html) == ["Graverobber (2)", "Roars", "Kifa Awrocis (Siksraja)", "Post:Durability 2.0"]


def test_weapons_and_armor_in_the_main_namespace_are_items():
    assert kind(0, "Svidaw sis", ["Definitions", "Weapons"]) == "item"
    assert kind(0, "Tower shield", ["Definitions", "Shields"]) == "item"
    assert kind(0, "Ribbed bone armor", ["Armor"]) == "item"
    assert kind(0, "Ring vambraces (crafted)", ["Armor"]) == "recipe"
    # overviews stay what they were; a more specific kind still wins
    assert kind(0, "Inner Fire weapons", ["Definitions", "Weapons"]) == "mechanics"
    assert kind(0, "Small edged weapons (crafted)", ["Weapons"]) == "general"
    assert kind(0, "Roar helm", ["Armor", "Barbarian abilities"]) == "spell"
    assert kind(0, "Foil", ["Definitions", "Weapons", "Disambiguation"]) == "disambig"
    assert kind(0, "Parry", ["Definitions"]) == "mechanics"


def test_soft_redirect_target_parsed_from_html():
    html = (
        '<div class="redirectMsg"><p>Redirect to:</p><ul class="redirectText"><li>'
        '<a href="/Crossing_Warrior_Mage_Guild" title="Crossing Warrior Mage Guild">x</a></li></ul></div>'
    )
    assert soft_redirect_target(html) == "Crossing Warrior Mage Guild"
    assert soft_redirect_target("<p>normal page</p>") is None


def test_notice_line_added_to_lead_only():
    blocks = [Block(None, None, None, ["Lead text. " * 40]), Block(2, "Details", "Details", ["More text. " * 40])]
    chunks = chunk_page("T", blocks, ChunkingCfg(), None, ["incomplete", "stub"])
    assert len(chunks) == 2
    assert chunks[0].text.startswith("(Marked incomplete, a stub on the wiki.)")
    assert "(Marked" not in chunks[1].text
    assert notice_line([]) == ""
