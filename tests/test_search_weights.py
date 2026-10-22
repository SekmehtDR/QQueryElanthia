from epedia.config import RetrievalCfg
from epedia.retrieval.search import score_chunk

CFG = RetrievalCfg()
BASE = dict(ns=0, is_disambig=False, is_log=False, is_obsolete=False, title_label="", partial_weight=0.0,
            in_prior=False)


def score(rrf=0.02, kind="crafting", page_kind="guide", **over):
    return score_chunk(rrf, CFG, question_kind=kind, page_kind=page_kind, **{**BASE, **over})


def test_favoured_page_kind_scores_higher_than_unfavoured():
    guide, _ = score(page_kind="guide")
    pc, _ = score(page_kind="pc")
    assert guide > pc


def test_log_and_obsolete_are_penalised():
    plain, w_plain = score(kind="character", page_kind="pc")
    log, w_log = score(kind="character", page_kind="pc", is_log=True)
    old, w_old = score(kind="character", page_kind="pc", is_obsolete=True)
    assert log < plain and old < plain
    assert w_log == w_plain * CFG.log_weight and w_old == w_plain * CFG.obsolete_weight


def test_exact_title_beats_a_better_kind_because_boosts_come_after_multipliers():
    # a pc page the user named exactly, asked as a crafting question, vs the favoured guide with a strong rank
    named, _ = score(rrf=0.0, kind="crafting", page_kind="pc", title_label="exact")
    guide, _ = score(rrf=0.0328, kind="crafting", page_kind="guide")
    assert named > guide


def test_unknown_or_missing_kind_is_neutral():
    a, wa = score(kind=None, page_kind="shop")
    b, wb = score(kind="general", page_kind="shop")
    assert wa == wb == 1.0 and a == b


def test_prior_boost_and_partial_boost_are_additive():
    base, _ = score(kind="general", page_kind="item")
    prior, _ = score(kind="general", page_kind="item", in_prior=True)
    part, _ = score(kind="general", page_kind="item", title_label="partial", partial_weight=1.0)
    assert round(prior - base, 6) == CFG.prior_boost
    assert round(part - base, 6) == CFG.title_partial_boost


def test_kind_weight_overrides_merge_over_defaults():
    cfg = RetrievalCfg.model_validate({"kind_weights": {"crafting": {"guide": 2.0}}})
    assert cfg.kind_weights["crafting"]["guide"] == 2.0
    assert cfg.kind_weights["crafting"]["recipe"] == 1.2  # untouched default
    assert cfg.kind_weights["place"]["place"] == 1.4
