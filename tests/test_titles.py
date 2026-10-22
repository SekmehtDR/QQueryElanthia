from epedia.retrieval.titles import normalize, query_as_title


def test_normalize_strips_namespace_and_variant_number():
    assert normalize("Weapon:Broadsword (1)") == "broadsword"
    assert normalize("Weapon:Broadsword") == "broadsword"
    assert normalize("Barbarian") == "barbarian"
    assert normalize("Merchant_Guild (disambiguation)") == "merchant guild"


def test_normalize_keeps_unknown_prefix_colon():
    assert normalize("Re: Something odd") == "re: something odd"


def test_query_as_title_strips_question_scaffolding():
    assert query_as_title("What is a broadsword?") == "broadsword"
    assert query_as_title("Tell me about the Barbarian guild") == "barbarian guild"
    assert query_as_title("who is Lexxa") == "lexxa"
