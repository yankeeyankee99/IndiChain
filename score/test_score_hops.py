"""Hop bands for the bar chart. No network."""

from score_hops import consensus_chain, hop_items, hop_score, score_chain, score_runs

PCSK9 = (
    "Proprotein convertase subtilisin/kexin type 9 (PCSK9) is a crucial protein "
    "governing the circulating levels of low density lipoprotein-cholesterol (LDL-C), "
    "by virtue of its pivotal role in the degradation of the LDL receptor (LDLR)."
)
SUA = (
    "In gouty patients, urate lowering therapies (ULTs) are recommended to bring "
    "serum uric acid (SUA) levels below 6.0 mg/dL, with the aim of dissolving urate depositions."
)
DEF = "Cystic fibrosis is a genetic and multisystemic disease."
CHLORIDE = "CFTR is a functional chloride channel facilitating transmembrane anion flow down electrochemical gradients."


def test_full_when_the_sentence_states_both_ends():
    assert hop_score("PCSK9", "LDL receptor (LDLR)", PCSK9, "30522786", "PCSK9", "Hypercholesterolemia") == 1.0


def test_self_loop_and_definition_and_direct_jump_are_zero():
    assert hop_score("CFTR", "CFTR", CHLORIDE, "1", "CFTR", "Cystic fibrosis") == 0.0
    assert hop_score("Cystic fibrosis", "cystic fibrosis", DEF, "37558605", "CFTR", "Cystic fibrosis") == 0.0
    assert hop_score("CFTR", "Cystic fibrosis", "Mutations in CFTR cause cystic fibrosis.", "1", "CFTR", "Cystic fibrosis") == 0.0
    assert hop_score("XDH", "xanthine dehydrogenase (XDH)", "XDH is xanthine dehydrogenase.", "1", "XDH", "Hyperuricemia") == 0.0


def test_nested_name_does_not_count_as_both_ends():
    assert hop_score("uric acid", "serum uric acid", SUA, "814587117291175937", "XDH", "Hyperuricemia") == 0.4


def test_slogan_is_not_a_separate_zero():
    assert hop_score(
        "PCSK9",
        "LDL receptor",
        "PCSK9 therapy treats disorders of the LDL receptor.",
        "1",
        "PCSK9",
        "Hypercholesterolemia",
    ) == 0.4


def test_carried_ldl_sentence_states_the_hop():
    quote = (
        "Cholesterol is carried through the blood by low-density lipoprotein (LDL) "
        '("bad" cholesterol) and then into cells by the LDL receptor.'
    )
    assert hop_score(
        "LDL receptor (LDLR)",
        "LDL (bad) cholesterol",
        quote,
        "812048418846277634",
        "PCSK9",
        "Hypercholesterolemia",
    ) == 1.0


def test_mesh_name_matches_the_common_disease_name():
    quote = "IgA1-producing plasma cells deposit galactose-deficient IgA1 and cause IgA nephropathy."
    assert hop_score(
        "IgA1-producing plasma cells",
        "Glomerulonephritis, IGA",
        quote,
        "24178970",
        "Kv1.3",
        "Glomerulonephritis, IGA",
    ) == 1.0


def test_self_loops_stay_in_the_denominator_and_a_missed_end_does():
    both = score_chain({
        "name": "Hypercholesterolemia",
        "target": "PCSK9",
        "reached": True,
        "steps": [
            {"step": "PCSK9 → PCSK9", "quotes": [PCSK9], "paper_id": "1"},
            {"step": "PCSK9 → LDL receptor (LDLR)", "quotes": [PCSK9], "paper_id": "30522786"},
        ],
    }, {"1": PCSK9, "30522786": PCSK9})
    sugar = "SGLT2 mediates kidney glucose reabsorption."
    stopped = score_chain({
        "name": "Diabetes Mellitus",
        "target": "SGLT2",
        "reached": False,
        "steps": [{
            "step": "SGLT2 → kidney glucose reabsorption",
            "quotes": [sugar],
            "paper_id": "1",
        }],
    }, {"1": sugar})
    assert both["score"] == 6.5
    assert both["items"]["not_shortcut"] == 0.5
    assert stopped["score"] == 3.5


def test_chain_mean_caps_a_partial_chain_under_a_full_chain():
    full = score_chain({
        "name": "Hypercholesterolemia",
        "target": "PCSK9",
        "steps": [{
            "step": "PCSK9 → LDL receptor (LDLR)",
            "quotes": [PCSK9],
            "paper_id": "30522786",
        }],
    }, {"30522786": PCSK9})
    partial = score_chain({
        "name": "Hyperuricemia",
        "target": "XDH",
        "steps": [{
            "step": "uric acid → serum uric acid",
            "quotes": [SUA],
            "paper_id": "9",
        }],
    }, {"9": SUA})
    assert full["score"] == 7.0
    assert partial["score"] == 6.4
    assert partial["score"] < full["score"]


def test_a_quote_missing_from_the_paper_is_not_full():
    chain = {
        "name": "Hypercholesterolemia",
        "target": "PCSK9",
        "steps": [{
            "step": "PCSK9 → LDL receptor (LDLR)",
            "quotes": [PCSK9],
            "paper_id": "30522786",
        }],
    }
    found = score_chain(chain, {"30522786": PCSK9})
    missing = score_chain(chain, {"999": PCSK9})
    assert found["items"]["in_cited"] == 1.0
    assert found["items"]["id_text"] == 1.0
    assert missing["items"]["in_cited"] == 0.0
    assert missing["items"]["id_text"] == 0.0
    assert found["score"] == 7.0
    assert missing["score"] == 5.0


def test_consensus_keeps_only_a_sentence_that_joins_the_ends():
    joined = {
        "target": "PCSK9",
        "name": "Hypercholesterolemia",
        "reached": True,
        "steps": [{
            "step": "PCSK9 → LDL receptor (LDLR)",
            "quotes": [PCSK9],
            "paper_id": "30522786",
        }, {
            "step": "PCSK9 → PCSK9",
            "quotes": [PCSK9],
            "paper_id": "1",
        }],
    }
    missed = {
        "target": "PCSK9",
        "name": "Hypercholesterolemia",
        "reached": True,
        "steps": [{
            "step": "PCSK9 → LDL receptor (LDLR)",
            "quotes": ["Plasma lipid levels changed in the study cohort."],
            "paper_id": "9",
        }],
    }
    agreed = consensus_chain([joined, missed])
    assert [step["step"] for step in agreed["steps"]] == ["PCSK9 → LDL receptor (LDLR)"]
    papers = {"30522786": PCSK9, "1": PCSK9}
    assert score_chain(agreed, papers)["score"] == 7.0
    assert score_chain(missed, papers)["score"] == 4.4
    assert score_chain(agreed, papers)["score"] > score_chain(missed, papers)["score"]
    pooled = score_runs([joined], papers)
    assert pooled["score"] == 6.5
    assert pooled["score"] < score_chain(agreed, papers)["score"]


def test_a_disease_definition_does_not_zero_the_hop():
    quote = "A high blood glucose level is the defining feature of this disease."
    scored = score_chain({
        "name": "Diabetes Mellitus",
        "target": "SGLT2",
        "steps": [{
            "step": "blood glucose → diabetes mellitus",
            "quotes": [quote],
            "paper_id": "1",
        }],
    }, {"1": quote})
    assert scored["items"]["not_shortcut"] == 1.0
    assert hop_items(
        "blood glucose", "diabetes mellitus", quote, "1", "SGLT2", "Diabetes Mellitus", {"1": quote}
    )["in_cited"] == 1.0


def test_stored_text_without_the_sentence_is_separate():
    quote = "PCSK9 binds the LDL receptor and mediates its degradation."
    items = hop_items(
        "PCSK9",
        "LDL receptor",
        quote,
        "12730697",
        "PCSK9",
        "Hypercholesterolemia",
        {"12730697": "A different sentence from the same paper."},
    )
    assert items["paper_id"] == 1.0
    assert items["id_text"] == 1.0
    assert items["in_cited"] == 0.0


if __name__ == "__main__":
    test_full_when_the_sentence_states_both_ends()
    test_self_loop_and_definition_and_direct_jump_are_zero()
    test_nested_name_does_not_count_as_both_ends()
    test_slogan_is_not_a_separate_zero()
    test_carried_ldl_sentence_states_the_hop()
    test_mesh_name_matches_the_common_disease_name()
    test_self_loops_stay_in_the_denominator_and_a_missed_end_does()
    test_chain_mean_caps_a_partial_chain_under_a_full_chain()
    test_a_quote_missing_from_the_paper_is_not_full()
    test_consensus_keeps_only_a_sentence_that_joins_the_ends()
    test_a_disease_definition_does_not_zero_the_hop()
    test_stored_text_without_the_sentence_is_separate()
    print("hop scores ok")
