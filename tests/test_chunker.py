from epedia.config import ChunkingCfg
from epedia.text.chunker import chunk_page
from epedia.text.html2md import Block, html_to_blocks

CFG = ChunkingCfg()


def test_chunks_follow_sections_with_matching_anchors(parse_fixture):
    chunks = chunk_page("Barbarian", html_to_blocks(parse_fixture("barbarian")["text"]), CFG)
    paths = [c.section_path for c in chunks]
    assert paths[0] == ""
    assert "Inner Fire Abilities > Berserks" in paths
    berserks = next(c for c in chunks if c.section_path.endswith("Berserks"))
    assert berserks.anchor == "Berserks"
    assert berserks.text.startswith("Berserks are short duration")


def test_tiny_sections_merge_but_keep_first_anchor(parse_fixture):
    chunks = chunk_page("Barbarian", html_to_blocks(parse_fixture("barbarian")["text"]), CFG)
    merged = next(c for c in chunks if c.section_path.endswith("Roars"))
    assert merged.anchor == "Roars"  # a citation link must land where the chunk starts


def test_embed_text_prefix_and_hash_stable(parse_fixture):
    blocks = html_to_blocks(parse_fixture("broadsword_1")["text"])
    a = chunk_page("Weapon:Broadsword (1)", blocks, CFG)[0]
    b = chunk_page("Weapon:Broadsword (1)", blocks, CFG)[0]
    assert a.embed_text.startswith("search_document: Weapon:Broadsword (1) | Overview")
    assert a.content_hash == b.content_hash


def test_long_section_is_split_with_overlap_under_max():
    sentence = "The quick brown fox jumps over the lazy dog near the old stone bridge. "
    blocks = [Block(2, "Long", "Long", ["\n\n".join(sentence * 6 for _ in range(30))])]
    chunks = chunk_page("T", blocks, CFG)
    assert len(chunks) > 1
    assert all(c.token_est <= CFG.max_tokens for c in chunks)
    assert chunks[1].section_path == "Long (part 2)"


def test_big_table_splits_by_rows_and_repeats_header():
    rows = "\n".join(f"| item{i} | {'x' * 60} |" for i in range(200))
    table = f"| Name | Val |\n|---|---|\n{rows}"
    chunks = chunk_page("T", [Block(2, "Table", "Table", [table])], CFG)
    assert len(chunks) > 1
    for c in chunks:
        assert c.text.startswith("| Name | Val |\n|---|---|")
        assert all(line.startswith("|") for line in c.text.splitlines())


def test_short_closing_paragraph_of_a_long_section_is_not_lost():
    from epedia.text.chunker import _split

    text = "\n\n".join(["A" * 200] * 17 + ["FINAL short fact."])
    pieces = _split(text, CFG)
    assert len(pieces) >= 2
    assert any("FINAL short fact." in p for p in pieces)


def test_pure_overlap_tail_is_not_duplicated_as_its_own_piece():
    from epedia.text.chunker import _split

    para = ("Sentence number one is here. " * 8).strip()  # ~230 chars, under one overlap
    text = "\n\n".join([para] * 18)
    pieces = _split(text, CFG)
    assert all(len(p) > 40 for p in pieces)
    assert sum(p.count("Sentence number one is here.") for p in pieces) >= 18 * 8


def test_empty_page_gives_no_chunks():
    assert chunk_page("T", [Block(None, None, None)], CFG) == []
