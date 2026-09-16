#!/usr/bin/env python3
"""Offline checks for the local logic in check_ads_bib.py. Run: python3 test_check_ads_bib.py"""

import json
import os
import tempfile
import time
import urllib.parse
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import check_ads_bib
from check_ads_bib import (
    AdsResult,
    candidate_queries,
    check_entries_parallel,
    cited_keys,
    duplicate_groups,
    fetch_ads_once,
    ads_resolve_reference,
    first_author_surname,
    combined_identifier_query,
    identifier_consensus,
    is_empty_result,
    local_identifiers,
    prefetch_exports,
    issue_search_url,
    reference_string,
    with_fallback,
    identity_conflicts,
    malformed_author,
    parse_bibtex_text,
    parsed_ads_entry,
    preferred_match,
    series_tokens,
    title_similarity,
    titles_conflict,
    wants_review,
    urlopen_with_retries,
    write_text_atomically,
)


def entry(text):
    entries = parse_bibtex_text(text)
    assert len(entries) == 1, f"expected one entry, got {len(entries)}"
    return entries[0]


def test_wrong_paper_is_flagged():
    """The reproducer: a key that names one author, resolving to another's paper."""
    local = entry(
        """@ARTICLE{Pan2021,
           author = {{Rafraf}, B. and {Others}, C.},
           title = {A study of something entirely different},
           doi = {10.1000/rafraf},
           year = {2021}}"""
    )
    ads = parsed_ads_entry(
        local,
        """@ARTICLE{X,
           author = {{Rafraf}, B. and {Others}, C.},
           title = {A study of something entirely different},
           doi = {10.1000/rafraf},
           year = {2021}}""",
    )
    # Internally consistent, so only the citation key can catch it.
    assert identity_conflicts(local, ads) == ["key"], identity_conflicts(local, ads)


def test_matching_key_is_clean():
    local = entry(
        """@ARTICLE{Rafraf2021,
           author = {{Rafraf}, B.},
           title = {A study of something},
           year = {2021}}"""
    )
    ads = parsed_ads_entry(local, local.raw)
    assert identity_conflicts(local, ads) == []


def test_key_year_tolerance_and_drift():
    """Preprint-vs-journal drift is fine; a four-year gap is not."""
    base = """@ARTICLE{Smith2020,
              author = {{Smith}, J.}, title = {A title}, year = {%s}}"""
    for year, expected in (("2019", []), ("2020", []), ("2021", []), ("2024", ["key"])):
        local = entry(base % "2020")
        ads = parsed_ads_entry(local, base % year)
        assert identity_conflicts(local, ads) == expected, (year, identity_conflicts(local, ads))


def test_corporate_and_particle_surnames_survive():
    for key, author in (
        ("Planck2018", "{Planck Collaboration} and {Aghanim}, N."),
        ("vanDenBosch2018", "{van den Bosch}, F.~C."),
        ("Smith2018", "John Smith"),
    ):
        local = entry("@ARTICLE{%s, author = {%s}, title = {T}, year = {2018}}" % (key, author))
        assert local.key == key, local.key
        ads = parsed_ads_entry(local, "@ARTICLE{X, author = {%s}, title = {T}, year = {2018}}" % author)
        assert identity_conflicts(local, ads) == [], (key, identity_conflicts(local, ads))


def test_non_author_year_keys_are_not_checked():
    local = entry("@ARTICLE{some_dataset_v2, author = {{Zzz}, A.}, title = {T}, year = {1999}}")
    ads = parsed_ads_entry(local, "@ARTICLE{X, author = {{Aaa}, B.}, title = {T}, year = {2020}}")
    assert "key" not in identity_conflicts(local, ads)


def test_latex_markup_does_not_false_positive():
    """The Obuljen case: identical titles written with different LaTeX markup."""
    pairs = [
        (r"Neutral hydrogen H\,{\sc i} in galaxies", "Neutral hydrogen H I in galaxies"),
        (r"A mass of {\ensuremath{\approx}}10 M$_{\odot}$", "A mass of ≈10 M⊙"),
        (r"The H\,{\sc i} mass function at z {\ensuremath{\approx}} 0", "The H I mass function at z ≈ 0"),
        ("Galaxies -- a review", "Galaxies – a review"),
        (r"Dark \& luminous matter", "Dark & luminous matter"),
    ]
    for local_title, ads_title in pairs:
        assert title_similarity(local_title, ads_title) >= 0.85, (local_title, ads_title)


def test_different_titles_still_conflict():
    assert title_similarity("The rotation curves of spiral galaxies", "Primordial nucleosynthesis constraints") < 0.85


def test_author_conflict_detected():
    local = entry("@ARTICLE{Smith2020, author = {{Smith}, J.}, title = {A title}, year = {2020}}")
    ads = parsed_ads_entry(local, "@ARTICLE{X, author = {{Jones}, K.}, title = {A title}, year = {2020}}")
    assert identity_conflicts(local, ads) == ["author", "key"]


def test_first_author_surname():
    assert first_author_surname("{Leavitt}, H.~S. and {Pickering}, E.") == "leavitt"
    assert first_author_surname("{van den Bosch}, F.~C.") == "vandenbosch"
    assert first_author_surname("{Planck Collaboration}") == "planckcollaboration"
    assert first_author_surname("") == ""


def test_skip_directive():
    text = """% checkcitation: skip
@BOOK{Jeffreys1939, author = {{Jeffreys}, H.}, title = {Theory of Probability}, year = {1939}}

@ARTICLE{Smith2020, author = {{Smith}, J.}, title = {T}, year = {2020}}"""
    parsed = parse_bibtex_text(text)
    assert [item.skip for item in parsed] == [True, False]


def test_skip_directive_needs_the_adjacent_line():
    text = """% checkcitation: skip
@BOOK{A, title = {T}}
@BOOK{B, title = {T}}"""
    assert [item.skip for item in parse_bibtex_text(text)] == [True, False]


def test_duplicate_detection():
    entries = parse_bibtex_text(
        """@ARTICLE{Dutton2007a, title = {T}, year = {2007}}
           @ARTICLE{Dutton2007b, title = {T}, year = {2007}}
           @ARTICLE{Other2010, title = {U}, year = {2010}}"""
    )
    resolved = AdsResult("OK", "", [{"bibcode": "2007ApJ...654...27D"}])
    other = AdsResult("OK", "", [{"bibcode": "2010ApJ...111...11X"}])
    groups = duplicate_groups([(entries[0], resolved), (entries[1], resolved), (entries[2], other)])
    assert len(groups) == 1
    (identity, duplicated), = groups
    assert identity == ("bibcode", "2007ApJ...654...27D")
    assert [item.key for item in duplicated] == ["Dutton2007a", "Dutton2007b"]


def test_malformed_author():
    assert malformed_author(entry("@ARTICLE{Koribalski2020, author = {{Koribalski}, B. and {et al.}}, title = {T}}"))
    assert not malformed_author(entry("@ARTICLE{Ok2020, author = {{Koribalski}, B. and others}, title = {T}}"))
    assert not malformed_author(entry("@ARTICLE{Ok2021, author = {{Koribalski}, B. and {Staveley-Smith}, L.}, title = {T}}"))


def test_preferred_match_collapses_arxiv_duplicate():
    matches = [
        {"bibcode": "2021arXiv210100001S", "title": ["A study of galaxies"]},
        {"bibcode": "2021ApJ...900...11S", "title": ["A study of galaxies"]},
    ]
    assert preferred_match(matches)["bibcode"] == "2021ApJ...900...11S"


def test_preferred_match_keeps_genuine_ambiguity():
    assert (
        preferred_match(
            [
                {"bibcode": "2021ApJ...900...11S", "title": ["A study of galaxies"]},
                {"bibcode": "2021MNRAS.500...22S", "title": ["Something else entirely"]},
            ]
        )
        is None
    )


def test_timeout_is_retried():
    response = BytesIO(b"ok")
    with patch("check_ads_bib.urllib.request.urlopen", side_effect=[TimeoutError, response]) as urlopen:
        assert urlopen_with_retries(object(), timeout=1) == b"ok"
    assert urlopen.call_count == 2


def test_cited_keys():
    tex = r"""
    \citep[e.g.][]{Smith2020, Jones2019}
    \citet{Brown2018}~\autocite{Green2017}
    % \cite{CommentedOut2000}
    \nocite{*}
    """
    assert cited_keys(tex) == {"Smith2020", "Jones2019", "Brown2018", "Green2017", "*"}


def test_series_papers_are_not_the_same_paper():
    """Paper I and Paper II score 0.99 on a character ratio; the numbering is the signal."""
    one = "The SAMI Galaxy Survey: instrument, Paper I"
    two = "The SAMI Galaxy Survey: instrument, Paper II"
    assert title_similarity(one, two) > 0.85, "the fuzzy ratio alone would accept this"
    assert titles_conflict(one, two)
    assert titles_conflict("Planck 2015 results. XIII", "Planck 2018 results. VI")
    assert titles_conflict("Gaia Data Release 2", "Gaia Data Release 3")
    # Same paper, different markup, must still agree.
    assert not titles_conflict("H\\,{\\sc i} in Paper II", "H I in Paper II")
    assert not titles_conflict("A study of galaxies", "A study of galaxies.")


def test_ordinary_words_are_not_series_numbers():
    """A roman-numeral test loose enough to match 'civil' would invent conflicts."""
    assert series_tokens("Civil dim mid did structure") == []
    assert series_tokens("Paper XIII of 2020") == ["xiii", "2020"]


def test_identifier_wrappers_are_not_conflicts():
    """`arXiv:` and a doi.org URL are how half the wild .bib files write these."""
    local = entry(
        """@ARTICLE{Smith2020,
           author = {{Smith}, J.}, title = {A title}, year = {2020},
           doi = {https://doi.org/10.1000/abc}, eprint = {arXiv:2001.00001}}"""
    )
    ads = parsed_ads_entry(
        local,
        """@ARTICLE{X,
           author = {{Smith}, J.}, title = {A title}, year = {2020},
           doi = {10.1000/abc}, eprint = {2001.00001}}""",
    )
    assert identity_conflicts(local, ads) == [], identity_conflicts(local, ads)


def test_commented_out_entry_is_not_an_entry():
    """@comment{@ARTICLE{...}} is how an entry is disabled; parsing it re-enables it."""
    entries = parse_bibtex_text(
        """@comment{@ARTICLE{Old2019, title = {Superseded}, year = {2019}}}
           @string{aj = "Astronomical Journal"}
           @ARTICLE{Live2020, title = {Current}, year = {2020}}"""
    )
    assert [item.key for item in entries] == ["Live2020"], [item.key for item in entries]


def test_starred_cite_commands_are_found():
    assert cited_keys(r"\citet*{A} \citep*{B} \citeauthor*{C}") == {"A", "B", "C"}


def test_duplicates_need_a_resolved_record():
    """For AMBIGUOUS, matches[0] is just the top hit, not a record the entry resolved to."""
    entries = parse_bibtex_text(
        """@ARTICLE{One2020, title = {T}, year = {2020}}
           @ARTICLE{Two2020, title = {U}, year = {2020}}"""
    )
    guess = AdsResult("AMBIGUOUS", "", [{"bibcode": "2020ApJ...1....1X"}, {"bibcode": "2020ApJ...2....2Y"}])
    assert duplicate_groups([(entries[0], guess), (entries[1], guess)]) == []


def test_title_query_drops_latex():
    """Solr tokenises \\sc and \\ensuremath as words, and then matches nothing."""
    local = entry(r"""@ARTICLE{Smith2020,
                      title = {Neutral hydrogen H\,{\sc i} at z \ensuremath{\approx} 0}, year = {2020}}""")
    (label, query), = [pair for pair in candidate_queries(local) if pair[0] == "title"]
    assert "sc" not in query.split('"')[1].split(), query
    assert "ensuremath" not in query and "approx" not in query, query
    assert "neutral hydrogen h" in query


def test_wrapped_identifiers_never_reach_ads():
    """A doi.org URL indexes nowhere, so querying it reports a correct entry MISSING."""
    local = entry(
        """@ARTICLE{Riess2022,
           title = {A title}, year = {2022},
           doi = {https://doi.org/10.3847/2041-8213/ac5c5b}, eprint = {arxiv:2112.04510}}"""
    )
    queries = dict(candidate_queries(local))
    assert queries["doi"] == 'doi:"10.3847/2041-8213/ac5c5b"', queries["doi"]
    assert queries["arxiv"] == 'identifier:"arXiv:2112.04510"', queries["arxiv"]
    combined = combined_identifier_query(local)
    assert 'doi:"10.3847/2041-8213/ac5c5b"' in combined, combined
    assert 'identifier:"arXiv:2112.04510"' in combined, combined
    assert "https://doi.org" not in combined and "arxiv:2112" not in combined


def test_atomic_write_follows_a_symlink_and_keeps_mode():
    """A .bib symlinked from a shared master must be updated, not quietly replaced."""
    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "master.bib"
        master.write_text("@ARTICLE{A, title = {T}}\n")
        master.chmod(0o664)
        link = Path(tmp) / "paper.bib"
        link.symlink_to(master)
        write_text_atomically(link, "@ARTICLE{A, title = {U}}\n")
        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert master.read_text() == "@ARTICLE{A, title = {U}}\n"
        assert master.stat().st_mode & 0o777 == 0o664, oct(master.stat().st_mode)


def test_one_bad_response_does_not_kill_a_single_worker_run():
    """jobs=1 is the default; a truncated response must cost one entry, not the run."""
    entries = parse_bibtex_text(
        """@ARTICLE{A2020, title = {T}, year = {2020}}
           @ARTICLE{B2020, title = {U}, year = {2020}}"""
    )

    def explode(entry_arg, *args, **kwargs):
        if entry_arg.key == "A2020":
            raise ConnectionResetError("Remote end closed connection without response")
        return AdsResult("OK", "", [{"bibcode": "2020ApJ...1....1X"}])

    with patch.object(check_ads_bib, "check_entry", explode):
        results = check_entries_parallel(entries, "token", rows=5, timeout=1, sleep=0, jobs=1, progress=False)
    assert [result.status for _, result in results] == ["ERROR", "OK"]
    assert "Remote end closed" in results[0][1].message


def test_cache_write_failure_keeps_the_response():
    """An unwritable cache is a slow run, not a lost ADS response."""
    check_ads_bib.reset_ads_run_cache()

    def store(value):
        raise OSError("read-only file system")

    assert fetch_ads_once("search", "q", lambda: ["doc"], store) == ["doc"]


def test_reference_string_uses_bibliographic_coordinates():
    """The resolver matches on author/year/journal/volume/page, not on a title."""
    article = entry(
        """@ARTICLE{Planck2020, author = {{Planck Collaboration} and {Aghanim}, N.},
           title = {Planck 2018 results}, journal = {\\aap}, year = {2020},
           volume = {641}, pages = {A6}}"""
    )
    assert reference_string(article) == "Planck Collaboration 2020, \\aap, 641, A6", reference_string(article)

    ranged = entry(
        """@ARTICLE{Springel2005, author = {{Springel}, V.}, title = {T},
           journal = {MNRAS}, year = {2005}, volume = {364}, pages = {1105--1134}}"""
    )
    assert reference_string(ranged) == "Springel, V. 2005, MNRAS, 364, 1105", reference_string(ranged)

    # No coordinates: fall back to the title, which is how a book resolves.
    book = entry("@BOOK{Jeffreys1939, author = {{Jeffreys}, H.}, title = {Theory of Probability}, year = {1939}}")
    assert reference_string(book) == "Jeffreys, H. 1939, theory of probability", reference_string(book)

    assert reference_string(entry("@ARTICLE{X, title = {T}, volume = {1}}")) == ""


def test_resolver_score_is_never_trusted():
    """A reference with one wrong page still scores 0.7, on a different author's paper."""
    check_ads_bib.reset_ads_run_cache()
    replies = {
        "good": b"1.0 2022ApJ...934L...7R -- Riess, A. G. 2022, ApJ, 934, L7",
        "weak": b"0.7 2022ApJ...934L...9L -- Riess, A. G. 2022, ApJ, 934, L9",
        "none": b"0.0 ................... -- Nobody 2019 ;; Exception: Hypotheses exhausted",
        "json": b'{"resolved": "0.8 1939thpr.book.....J -- Jeffreys, H. 1939, Theory of Probability"}',
    }
    for name, expected in (("good", "2022ApJ...934L...7R"), ("weak", "2022ApJ...934L...9L"),
                           ("none", None), ("json", "1939thpr.book.....J")):
        check_ads_bib.reset_ads_run_cache()
        with patch.object(check_ads_bib, "urlopen_with_retries", lambda *a, **k: replies[name]):
            assert ads_resolve_reference(name, "token", 5) == expected, name
    # A low score is still handed on: only identity_conflicts may reject it.


def test_a_dead_identifier_falls_back_to_title_then_the_resolver():
    """One bad DOI used to end the search; now the entry still gets a proposal."""
    local = entry(
        """@ARTICLE{Springel2005, author = {{Springel}, V.},
           title = {The cosmological simulation code GADGET-2},
           journal = {MNRAS}, year = {2005}, volume = {364}, pages = {1105},
           doi = {10.9999/typo}}"""
    )
    export = """@ARTICLE{X, author = {{Springel}, V.},
                title = {The cosmological simulation code GADGET-2},
                journal = {MNRAS}, year = {2005}, volume = {364}, pages = {1105}}"""
    missing = AdsResult("MISSING", 'doi:"10.9999/typo"', [], "doi:0")

    # 1. the title query finds it
    check_ads_bib.reset_ads_run_cache()
    with patch.object(check_ads_bib, "ads_search", lambda q, *a, **k: [{"bibcode": "2005MNRAS.364.1105S"}]), \
         patch.object(check_ads_bib, "ads_export_bibtex", lambda *a, **k: export):
        result = with_fallback(local, missing, "token", 5, 5, 0)
    assert result.status == "IDENTIFIER_MISMATCH", result.status
    assert "title and year" in result.message, result.message
    assert result.ads_bibtex, "the proposal must arrive with the card, not behind a click"

    # 2. the title query finds nothing, the resolver does
    check_ads_bib.reset_ads_run_cache()
    with patch.object(check_ads_bib, "ads_search", lambda q, *a, **k: [] if "title" in q else [{"bibcode": "2005MNRAS.364.1105S"}]), \
         patch.object(check_ads_bib, "ads_resolve_reference", lambda *a, **k: "2005MNRAS.364.1105S"), \
         patch.object(check_ads_bib, "ads_export_bibtex", lambda *a, **k: export):
        result = with_fallback(local, missing, "token", 5, 5, 0)
    assert result.status == "IDENTIFIER_MISMATCH", result.status
    assert "reference resolver" in result.message, result.message

    # 3. nothing matches: the original MISSING is handed back untouched
    check_ads_bib.reset_ads_run_cache()
    with patch.object(check_ads_bib, "ads_search", lambda *a, **k: []), \
         patch.object(check_ads_bib, "ads_resolve_reference", lambda *a, **k: None):
        assert with_fallback(local, missing, "token", 5, 5, 0) is missing


def test_a_fallback_match_that_is_another_paper_still_conflicts():
    """The fallback widens the search; it must not widen what counts as agreement."""
    local = entry(
        """@ARTICLE{Croom2021, author = {{Croom}, S.}, title = {The SAMI Survey, Paper I},
           journal = {MNRAS}, year = {2021}, volume = {1}, pages = {1}, doi = {10.9999/typo}}"""
    )
    export = """@ARTICLE{X, author = {{Croom}, S.}, title = {The SAMI Survey, Paper II},
                journal = {MNRAS}, year = {2021}}"""
    check_ads_bib.reset_ads_run_cache()
    with patch.object(check_ads_bib, "ads_search", lambda *a, **k: [{"bibcode": "2021MNRAS...1....1C"}]), \
         patch.object(check_ads_bib, "ads_export_bibtex", lambda *a, **k: export):
        result = with_fallback(local, AdsResult("MISSING", "q", [], "doi:0"), "token", 5, 5, 0)
    assert result.status == "ADS_RECORD_CONFLICT", result.status
    assert "title" in result.message


def test_the_handoff_search_is_looser_than_the_query_that_failed():
    """Replaying a query that returned nothing helps nobody."""
    local = entry(
        """@ARTICLE{Smith2020, author = {{Smith}, J. and {Jones}, A.},
           title = {A study of H\\,{\\sc i}}, year = {2020}}"""
    )
    url = issue_search_url(local, AdsResult("MISSING", 'title:"exact" year:2020', [], ""))
    assert url.startswith("https://ui.adsabs.harvard.edu/search/q=")
    query = urllib.parse.unquote(url.split("q=", 1)[1])
    assert query == 'title:(a study of h i) author:"Smith" year:2019-2021', query
    assert issue_search_url(entry("@ARTICLE{X, note = {n}}"), AdsResult("MISSING", "", [], "")) == ""


PLANCK_DOC = {
    "bibcode": "2020A&A...641A...6P",
    "title": ["Planck 2018 results. VI. Cosmological parameters"],
    "year": "2020",
    "doi": ["10.1051/0004-6361/201833910"],
    "identifier": ["2018arXiv180706209P", "2020A&A...641A...6P",
                   "10.48550/arXiv.1807.06209", "10.1051/0004-6361/201833910", "arXiv:1807.06209"],
}
OTHER_DOC = {
    "bibcode": "2005MNRAS.364.1105S",
    "title": ["The cosmological simulation code GADGET-2"],
    "year": "2005",
    "doi": ["10.1111/j.1365-2966.2005.09655.x"],
    "identifier": ["2005MNRAS.364.1105S", "10.1111/j.1365-2966.2005.09655.x"],
}
PLANCK_ENTRY = """@ARTICLE{Planck2020, title = {T}, year = {2020},
   adsurl = {https://ui.adsabs.harvard.edu/abs/2020A&A...641A...6P},
   doi = {10.1051/0004-6361/201833910}, eprint = {1807.06209}}"""


def consensus(local, docs):
    """Run the consensus against a canned ADS response, counting the requests."""
    check_ads_bib.reset_ads_run_cache()
    calls = []

    def stub(query, *args, **kwargs):
        calls.append(query)
        return docs

    with patch.object(check_ads_bib, "ads_search", stub):
        return identifier_consensus(local, "token", 5, 5, 0), calls


def test_one_request_resolves_every_identifier():
    """Three identifiers used to cost three searches; the record lists its own."""
    local = entry(PLANCK_ENTRY)
    assert [label for label, _ in local_identifiers(local)] == ["bibcode", "doi", "arxiv"]
    result, calls = consensus(local, [PLANCK_DOC])
    assert result.status == "OK", (result.status, result.message)
    assert len(calls) == 1, calls
    assert calls[0].count(" OR ") == 3, calls[0]


def test_identifiers_naming_different_papers_still_conflict():
    """The whole point of checking more than one identifier must survive the merge."""
    local = entry(PLANCK_ENTRY.replace("10.1051/0004-6361/201833910", "10.1111/j.1365-2966.2005.09655.x"))
    result, calls = consensus(local, [PLANCK_DOC, OTHER_DOC])
    assert result.status == "IDENTIFIER_CONFLICT", result.status
    assert "bibcode=2020A&A...641A...6P" in result.message and "doi=2005MNRAS.364.1105S" in result.message
    assert len(calls) == 1


def test_an_identifier_ads_does_not_know_is_a_mismatch_not_a_miss():
    local = entry(PLANCK_ENTRY.replace("10.1051/0004-6361/201833910", "10.9999/typo"))
    result, _ = consensus(local, [PLANCK_DOC])
    assert result.status == "IDENTIFIER_MISMATCH", result.status
    assert "doi lookup failed" in result.message, result.message


def test_nothing_recognised_is_missing():
    result, _ = consensus(entry(PLANCK_ENTRY), [])
    assert result.status == "MISSING", result.status
    assert result.message == "bibcode:0, doi:0, arxiv:0", result.message


def test_an_arxiv_and_journal_pair_is_not_ambiguous():
    """One identifier matching both records of the same paper is the refereed one."""
    preprint = {"bibcode": "2018arXiv180706209P", "title": PLANCK_DOC["title"],
                "identifier": ["arXiv:1807.06209", "2018arXiv180706209P"]}
    local = entry("""@ARTICLE{Planck2020, title = {T}, year = {2020}, eprint = {1807.06209}}""")
    result, _ = consensus(local, [preprint, PLANCK_DOC])
    assert result.status == "OK", (result.status, result.message)
    assert result.matches[0]["bibcode"] == "2020A&A...641A...6P"


def test_one_export_request_serves_every_local_bibcode():
    """One request per hundred entries, instead of one per entry."""
    check_ads_bib.reset_ads_run_cache()
    check_ads_bib.ADS_CACHE = None
    entries = parse_bibtex_text(
        """@ARTICLE{A2020, adsurl = {https://ui.adsabs.harvard.edu/abs/2020ApJ...1....1A}}
           @ARTICLE{B2020, adsurl = {https://ui.adsabs.harvard.edu/abs/2020ApJ...2....2B}}
           @ARTICLE{C2020, title = {no bibcode here}}"""
    )
    asked = []

    def bulk(bibcodes, token, timeout):
        asked.append(sorted(bibcodes))
        return {b: "@ARTICLE{%s, title = {T}}" % b for b in bibcodes}

    with patch.object(check_ads_bib, "ads_export_bibtex_many", bulk):
        prefetch_exports(entries, "token", 5)
    assert asked == [["2020ApJ...1....1A", "2020ApJ...2....2B"]], asked
    # The per-entry path now finds them without a request of its own.
    def explode(*args, **kwargs):
        raise AssertionError("a per-entry export request was made after the prefetch")
    with patch.object(check_ads_bib, "urlopen_with_retries", explode):
        assert check_ads_bib.ads_export_bibtex("2020ApJ...1....1A", "token", 5).startswith("@ARTICLE{2020ApJ...1....1A,")


def test_the_export_never_carries_a_literal_et_al():
    """The per-bibcode GET truncates authors to `et al.` - the very thing we warn about."""
    sent = {}

    def capture(request, timeout):
        sent["method"] = request.get_method()
        sent["body"] = json.loads(request.data.decode())
        return json.dumps({"export": "@ARTICLE{2016A&A...594A..13P,\n author = {{Planck} and {Ade}, P.}\n}"}).encode()

    check_ads_bib.reset_ads_run_cache()
    check_ads_bib.ADS_CACHE = None
    with patch.object(check_ads_bib, "urlopen_with_retries", capture):
        export = check_ads_bib.ads_export_bibtex("2016A&A...594A..13P", "token", 5)
    assert sent["method"] == "POST", sent["method"]
    assert sent["body"] == {"bibcode": ["2016A&A...594A..13P"]}, sent["body"]
    assert not malformed_author(entry(export)), export


def test_a_miss_expires_long_before_a_hit():
    """A resolved record does not change; a miss stops being one as soon as ADS indexes it."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = check_ads_bib.AdsCache(Path(tmp) / "c.json", ttl=check_ads_bib.DEFAULT_CACHE_TTL)
        cache.set("search", "hit", [{"bibcode": "2020ApJ...1....1A"}])
        cache.set("search", "miss", [])
        stale = time.time() - 2 * check_ads_bib.MISS_TTL
        for key in ("hit", "miss"):
            cache.namespace("search")[key]["stored_at"] = stale
        assert cache.get("search", "hit") is not None, "a hit must survive the short expiry"
        assert cache.get("search", "miss") is None, "a miss must not"
    assert is_empty_result([]) and is_empty_result("") and is_empty_result(None)
    assert not is_empty_result([{"bibcode": "x"}]) and not is_empty_result("@ARTICLE{}")


def test_the_app_is_the_default_but_never_in_a_pipe():
    """`check_ads_bib.sh ref.bib | less` must not hang on a server nobody opens."""
    parser = check_ads_bib.build_parser()
    plain = parser.parse_args(["ref.bib"])
    assert wants_review(plain, interactive=True), "a terminal gets the app"
    assert not wants_review(plain, interactive=False), "a pipe gets the report"
    assert wants_review(parser.parse_args(["ref.bib", "--review"]), interactive=False), "--review forces it"
    assert not wants_review(parser.parse_args(["ref.bib", "--no-review"]), interactive=True)
    # --replace is the other way of doing the same job; it must not also serve.
    assert not wants_review(parser.parse_args(["ref.bib", "--replace"]), interactive=True)


def test_a_collaboration_key_is_not_a_wrong_paper():
    """`CosmoVerse2025` resolves to a paper by Di Valentino. That is not a mismatch.

    It used to conflict forever, and replacing could never clear it, because the
    citation key is deliberately kept - so the app asked about it on every pass.
    """
    local = entry(
        """@ARTICLE{CosmoVerse2025,
           author = {{Di Valentino}, Eleonora and {Said}, Jackson Levi},
           title = {The CosmoVerse White Paper: Addressing observational tensions},
           year = {2025}}"""
    )
    assert identity_conflicts(local, parsed_ads_entry(local, local.raw)) == []

    # The project name has to actually be in the title.
    elsewhere = entry(
        """@ARTICLE{CosmoVerse2025,
           author = {{Di Valentino}, Eleonora}, title = {Something unrelated entirely}, year = {2025}}"""
    )
    assert identity_conflicts(elsewhere, parsed_ads_entry(elsewhere, elsewhere.raw)) == ["key"]

    # And a short key must not be waved through by a chance substring.
    short = entry("@ARTICLE{Li2020, author = {{Rafraf}, B.}, title = {A study of the lithium problem}, year = {2020}}")
    assert identity_conflicts(short, parsed_ads_entry(short, short.raw)) == ["key"]


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
