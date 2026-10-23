import refs_compare as rc

REC = {"doi": "10.1007/s12599-020-00641-4", "title": "Robotic Process Mining: Vision and Challenges",
       "authors": ["Leno", "Polyvyanyy", "Dumas", "La Rosa", "Maggi"], "year": 2020,
       "venues": ["Business & Information Systems Engineering", "Bus Inf Syst Eng"], "source": "crossref"}


def ref(**kw):
    base = {"title": "Robotic process mining: vision and challenges", "authors": ["Leno", "Polyvyanyy"],
            "year": 2020, "venue": "Business & Information Systems Engineering", "doi": "10.1007/s12599-020-00641-4",
            "et_al": False}
    base.update(kw)
    return base


def test_verified():
    r = rc.compare(ref(), REC)
    assert r["verdict"] == "verified" and r["differences"] == []


def test_normalisation_case_punct_accents():
    assert rc.title_similarity("Réseaux: Étude!", "reseaux etude") == 1.0
    assert rc.title_similarity("Cats & Dogs", "cats and dogs") == 1.0


def test_subtitle_truncated_is_ok():
    assert rc.title_similarity("Attention is all you need for translation tasks", "Attention is all you need for translation tasks: a study") >= 0.9


def test_abbreviated_venue_is_a_match_or_minor():
    r = rc.compare(ref(venue="Bus. Inf. Syst. Eng."), REC)
    assert r["verdict"] == "verified"
    r = rc.compare(ref(venue="Proc. of ICSE"), {**REC, "venues": ["International Conference on Software Engineering"]})
    assert r["verdict"] == "verified"


def test_wrong_venue_listed_not_material():
    r = rc.compare(ref(venue="Nature"), REC)
    assert r["verdict"] == "verified_with_differences"
    assert r["differences"][0]["field"] == "venue"


def test_year_off_by_one_is_minor_off_by_more_is_mismatch():
    assert rc.compare(ref(year=2019), REC)["verdict"] == "verified_with_differences"
    assert rc.compare(ref(year=2015), REC)["verdict"] == "mismatch"


def test_real_doi_wrong_work_is_mismatch():
    other = {"doi": "10.1000/zzz", "title": "A Completely Different Paper About Soil", "authors": ["Garcia"],
             "year": 2011, "venues": ["Soil Science"]}
    r = rc.compare(ref(doi="10.1000/zzz"), other)
    assert r["verdict"] == "mismatch"
    assert {d["field"] for d in r["differences"]} >= {"title", "authors", "year"}


def test_first_author_mismatch_is_material():
    r = rc.compare(ref(authors=["Smith", "Jones"]), REC)
    assert r["verdict"] == "mismatch"


def test_et_al_only_first_author_checked():
    r = rc.compare(ref(authors=["Leno"], et_al=True), REC)
    assert r["verdict"] == "verified"


def test_identifier_only_reference_marks_no_comparable_fields():
    r = rc.compare({"title": None, "authors": [], "year": None, "venue": None, "doi": REC["doi"]}, REC)
    assert r["comparable_fields"] is False


def test_candidate_acceptance():
    assert rc.candidate_accepted(ref(), REC)
    assert not rc.candidate_accepted(ref(title="Robotic process mining"), {**REC, "title": "Process mining in healthcare"})
    assert not rc.candidate_accepted(ref(year=2005), REC)
    assert not rc.candidate_accepted(ref(authors=["Nobody"]), REC)
    assert not rc.candidate_accepted(ref(title=None), REC)
    assert rc.best_candidate(ref(), [{**REC, "title": "Other"}, REC]) is REC
