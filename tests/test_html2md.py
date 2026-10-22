from epedia.text.html2md import html_to_blocks


def test_stat_block_becomes_key_value_lines(parse_fixture):
    blocks = html_to_blocks(parse_fixture("broadsword_1")["text"])
    text = "\n".join(b.text for b in blocks)
    assert "Type: Heavy Edged" in text
    assert "Slice: somewhat heavy (8/28)" in text
    assert "Weight: 35 stones" in text
    assert "Appraised Cost: 287 Kronars" in text
    assert "Zerek's General Store" in text
    assert "<" not in text and "smwttcontent" not in text


def test_empty_label_rows_are_dropped(parse_fixture):
    text = "\n".join(b.text for b in html_to_blocks(parse_fixture("broadsword_1")["text"]))
    assert "Properties:" not in text


def test_headings_become_blocks_with_anchors(parse_fixture):
    blocks = html_to_blocks(parse_fixture("barbarian")["text"])
    assert blocks[0].level is None
    by_heading = {b.heading: b for b in blocks if b.heading}
    assert by_heading["Official Information"].level == 2
    assert by_heading["Guild Hall Locations"].level == 3
    assert by_heading["Guild Hall Locations"].anchor == "Guild_Hall_Locations"


def test_infobox_in_lead_and_no_duplicated_notice(parse_fixture):
    lead = html_to_blocks(parse_fixture("barbarian")["text"])[0].text
    assert "Primary Skillset: Weapon" in lead
    assert lead.count("Returning players may want to read") == 1


def test_no_edit_links_or_toc_text(parse_fixture):
    text = "\n".join(b.text for b in html_to_blocks(parse_fixture("barbarian")["text"]))
    assert "[edit]" not in text
    assert "Contents" not in text


def test_markdown_table_has_header_and_rows():
    html = (
        '<div class="mw-parser-output"><table><tr><th>Name</th><th>Pct</th></tr>'
        "<tr><td>Human</td><td>17%</td></tr><tr><td>Elf</td><td>9%</td></tr></table></div>"
    )
    text = html_to_blocks(html)[0].text
    assert text.splitlines()[0] == "| Name | Pct |"
    assert "| Human | 17% |" in text


def test_rowspan_is_expanded():
    html = (
        '<div class="mw-parser-output"><table><tr><th>A</th><th>B</th></tr>'
        '<tr><td rowspan="2">x</td><td>1</td></tr><tr><td>2</td></tr></table></div>'
    )
    text = html_to_blocks(html)[0].text
    assert "| x | 1 |" in text and "| x | 2 |" in text


def test_creature_infobox_becomes_key_value_lines_and_banner_is_stripped(parse_fixture):
    from epedia.text.html2md import html_to_page

    page = html_to_page(parse_fixture("musk_hog")["text"])
    text = "\n".join(b.text for b in page.blocks)
    assert "Overall: 1" in text
    assert "Skill Cap: 10 to 35" in text
    assert "Part Name: hog hoof" in text
    assert "Incomplete Article" not in text and "THIS FORM" not in text
    assert page.notices == ["incomplete"]
    assert page.soft_redirect is None


def test_soft_redirect_page_has_target_and_no_content(parse_fixture):
    from epedia.text.html2md import html_to_page

    page = html_to_page(parse_fixture("soft_redirect")["text"])
    assert page.soft_redirect == "Crossing Warrior Mage Guild"
    assert all(not b.text for b in page.blocks)


def test_related_forum_posts_section_is_dropped():
    html = (
        '<div class="mw-parser-output"><p>Intro text.</p><h2><span class="mw-headline" id="Related_Forum_Posts">'
        "Related Forum Posts</span></h2><p>Click here to search for related posts.</p>"
        '<h2><span class="mw-headline" id="Notes">Notes</span></h2><p>Real notes.</p></div>'
    )
    headings = [b.heading for b in html_to_blocks(html)]
    assert "Related Forum Posts" not in headings and "Notes" in headings


def test_wrapper_table_unwraps_and_short_label_table_becomes_key_value():
    rows = "".join(f"<tr><td>Label {i}</td><td>{i}</td></tr>" for i in range(6))
    html = (
        '<div class="mw-parser-output"><table style="float:right"><tr><td>'
        f"<table>{rows}</table></td></tr></table></div>"
    )
    text = html_to_blocks(html)[0].text
    assert "Label 0: 0" in text and "Label 5: 5" in text
    assert "|" not in text


def test_real_banner_removed_but_content_container_kept():
    banner = '<div style="border: 2px solid black"><b>Stub</b> This article is a stub, expand it.</div>'
    big = "<p>" + ("Real content. " * 120) + "</p>"
    html = f'<div class="mw-parser-output">{banner}<div class="info">{big}</div></div>'
    from epedia.text.html2md import html_to_page

    page = html_to_page(html)
    assert page.notices == ["stub"]
    assert "Real content." in page.blocks[0].text


def test_single_column_table_becomes_lines_not_a_col_1_grid():
    html = (
        '<div class="mw-parser-output"><table><tr><td><b>Province:</b> Zoluren</td></tr>'
        "<tr><td><b>Currency:</b> Kronar</td></tr></table></div>"
    )
    text = html_to_blocks(html)[0].text
    assert text.splitlines() == ["Province: Zoluren", "Currency: Kronar"]


def test_malformed_span_attributes_do_not_crash():
    html = (
        '<div class="mw-parser-output"><table><tr><th>A</th><th>B</th></tr>'
        '<tr><td colspan="=2" rowspan="x">v</td></tr></table></div>'
    )
    assert "v" in html_to_blocks(html)[0].text


def test_nested_lists_are_indented():
    html = '<div class="mw-parser-output"><ul><li>a<ul><li>b</li></ul></li><li>c</li></ul></div>'
    assert html_to_blocks(html)[0].text.splitlines() == ["- a", "  - b", "- c"]
