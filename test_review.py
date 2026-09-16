#!/usr/bin/env python3
"""Offline checks for the review server in review.py. Run: python3 test_review.py

No ADS calls: check_entries_parallel is stubbed, so every test is about the
server's own contract - what it writes, what it refuses, and what it hands the page.
"""

import json
import tempfile
import urllib.parse
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import check_ads_bib
import review

BIB = """@ARTICLE{Smith2020,
  author = {{Smith}, J.},
  title = {A study of galaxies},
  year = {2020},
  adsurl = {https://ui.adsabs.harvard.edu/abs/2020ApJ...900....1S}
}

@ARTICLE{Jones2019,
  author = {{Jones}, A.},
  title = {Another study},
  year = {2019}
}
"""

ADS_EXPORT = """@ARTICLE{2020ApJ...900....1S,
       author = {{Smith}, J.},
        title = {A study of galaxies},
         year = 2020,
       adsurl = {https://ui.adsabs.harvard.edu/abs/2020ApJ...900....1S}
}
"""


def result(status, **kw):
    return check_ads_bib.AdsResult(status, kw.pop("query", ""), kw.pop("matches", []), **kw)


def canned(statuses):
    """A stand-in for check_entries_parallel that answers from a key -> result map."""

    def stub(entries, token, **kwargs):
        return [(entry, statuses[entry.key]) for entry in entries]

    return stub


def session(tmp, statuses, text=BIB, tex=()):
    path = Path(tmp) / "ref.bib"
    path.write_text(text)
    item = review.Review(path, "token", tex=tex)
    with patch.object(check_ads_bib, "check_entries_parallel", canned(statuses)):
        item.refresh()
    return item


def serving(item):
    """A real server on an ephemeral port, plus a request helper that is never stale."""
    server = review.ThreadingHTTPServer(("127.0.0.1", 0), review.handler_for(item))
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path):
        with urlopen(base + path) as response:
            return json.load(response)

    def post(path, body, revision=None):
        headers = {"Content-Type": "application/json"}
        headers["If-Match"] = get("/api/state")["revision"] if revision is None else revision
        request = Request(base + path, headers=headers, data=json.dumps(body).encode())
        with urlopen(request) as response:
            return json.load(response)

    def stop():
        server.shutdown()
        server.server_close()
        worker.join()

    return base, get, post, stop


MISMATCH = {
    "Smith2020": result("ADS_BIBTEX_MISMATCH", matches=[{"bibcode": "2020ApJ...900....1S"}], ads_bibtex=ADS_EXPORT),
    "Jones2019": result("OK", matches=[{"bibcode": "2019ApJ...800....2J"}]),
}


def test_state_describes_every_entry():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        payload = item.payload()
        keys = [entry["key"] for entry in payload["entries"]]
        assert keys == ["Smith2020", "Jones2019"], keys
        smith = payload["entries"][0]
        assert smith["line"] == 1 and smith["status"] == "ADS_BIBTEX_MISMATCH"
        assert smith["issue"] and smith["action"], "the page needs the CLI's own wording"
        assert smith["ads"]["title"] == "A study of galaxies"
        assert smith["ads_bibtex"].startswith("@ARTICLE{Smith2020,"), "the key must survive into the proposal"
        assert smith["auto"] is True and smith["conflicts"] == []


def test_an_unresolved_entry_is_handed_over_with_a_search_link():
    """No route matched, so the card must offer somewhere to go, not an empty box."""
    statuses = dict(MISMATCH)
    statuses["Jones2019"] = result("MISSING", query='title:"Another study" year:2019', message="title:0")
    with tempfile.TemporaryDirectory() as tmp:
        entries = {e["key"]: e for e in session(tmp, statuses).payload()["entries"]}
        jones = entries["Jones2019"]
        assert jones["candidate"] == "" and jones["ads_bibtex"] == ""
        assert jones["search_url"].startswith("https://ui.adsabs.harvard.edu/search/q=")
        assert "year:2018-2020" in urllib.parse.unquote(jones["search_url"]), jones["search_url"]
        assert jones["manual"] is True, "and it must still accept a pasted replacement"
        # A resolved entry has nothing to hand over.
        assert entries["Smith2020"]["search_url"] == ""
        assert entries["Smith2020"]["matches"][0]["url"].endswith("/abstract")


def test_a_candidate_that_may_be_another_paper_is_not_one_keypress():
    """The CLI demands a typed `replace` here, so the page must demand a click."""
    conflicting = dict(MISMATCH)
    conflicting["Smith2020"] = result(
        "ADS_RECORD_CONFLICT",
        matches=[{"bibcode": "2020ApJ...900....9Z"}],
        ads_bibtex=ADS_EXPORT.replace("A study of galaxies", "A study of galaxies, Paper II"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        smith = session(tmp, conflicting).payload()["entries"][0]
        assert smith["conflicts"] == ["title"], smith["conflicts"]
        assert smith["auto"] is False


def test_replace_writes_the_file_behind_a_backup():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
                payload = post("/api/replace", {"key": "Smith2020", "bibtex": ADS_EXPORT})
        finally:
            stop()
        text = item.path.read_text()
        assert "year = 2020," in text, text
        assert "@ARTICLE{Smith2020," in text, "the citation key must be kept"
        assert "2020ApJ...900....1S," not in text, "the ADS key leaked into the file"
        assert "@ARTICLE{Jones2019," in text, "an untouched entry was disturbed"
        backup = item.path.with_name(item.path.name + ".bak")
        assert backup.read_text() == BIB, "the backup is not the file as it was"
        assert payload["replaced"] == 1 and payload["backup"] == backup.name


def test_a_stale_tab_cannot_overwrite_an_outside_edit():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            stale = get("/api/state")["revision"]
            item.path.write_text(BIB.replace("Another study", "Another study, revised"))
            before = item.path.read_bytes()
            try:
                post("/api/replace", {"key": "Smith2020", "bibtex": ADS_EXPORT}, revision=stale)
            except HTTPError as exc:
                assert exc.code == 409, exc.code
                assert "Reload" in json.load(exc)["error"]
            else:
                assert False, "a stale tab was allowed to write"
            assert item.path.read_bytes() == before, "the outside edit was clobbered"
        finally:
            stop()


def test_recheck_needs_no_revision_because_it_is_how_a_stale_tab_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            item.path.write_text(BIB.replace("Another study", "Another study, revised"))
            with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
                payload = post("/api/recheck", {}, revision="stale-nonsense")
            titles = [entry["local"]["title"] for entry in payload["entries"]]
            assert "Another study, revised" in titles, titles
        finally:
            stop()


def test_two_entries_under_one_key_refuse_replacement():
    """Replacing by key would silently pick one of them and print the other's name."""
    doubled = BIB + BIB.split("\n\n")[0] + "\n"
    statuses = dict(MISMATCH)
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, statuses, text=doubled)
        base, get, post, stop = serving(item)
        try:
            before = item.path.read_bytes()
            try:
                post("/api/replace", {"key": "Smith2020", "bibtex": ADS_EXPORT})
            except HTTPError as exc:
                assert exc.code == 400, exc.code
                assert "share the key" in json.load(exc)["error"]
            else:
                assert False, "an ambiguous key was replaced anyway"
            assert item.path.read_bytes() == before
        finally:
            stop()


def test_a_replacement_must_be_exactly_one_entry():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            before = item.path.read_bytes()
            for bad in (ADS_EXPORT + ADS_EXPORT, "not bibtex at all"):
                try:
                    post("/api/replace", {"key": "Smith2020", "bibtex": bad})
                except HTTPError as exc:
                    assert exc.code == 400, exc.code
                else:
                    assert False, f"accepted {bad[:20]!r}"
            assert item.path.read_bytes() == before
        finally:
            stop()


def test_bad_requests_answer_instead_of_dropping_the_connection():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            for path, headers, data, code in (
                ("/api/nope", {"Content-Type": "application/json"}, b"{}", 404),
                ("/api/replace", {"Content-Type": "text/plain"}, b"{}", 400),
                ("/api/replace", {"Content-Type": "application/json"}, b"not json", 400),
                ("/api/replace", {"Content-Type": "application/json"}, b"[]", 400),
            ):
                try:
                    urlopen(Request(base + path, headers=headers, data=data))
                except HTTPError as exc:
                    assert exc.code == code, (path, exc.code)
                else:
                    assert False, f"{path} was accepted"
            try:
                urlopen(base + "/api/nothing")
            except HTTPError as exc:
                assert exc.code == 404
            else:
                assert False, "an unknown GET was accepted"
        finally:
            stop()


def test_the_page_is_served_fresh_from_disk():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            with urlopen(base + "/") as response:
                assert response.headers["Cache-Control"] == "no-store, must-revalidate"
                assert b"<title>ADS citation review</title>" in response.read()
        finally:
            stop()


def test_refresh_rechecks_only_what_changed():
    """One rewrite moves every later offset, so a write re-parses; it must not re-query."""
    asked = []

    def counting(entries, token, **kwargs):
        asked.append([entry.key for entry in entries])
        return [(entry, MISMATCH[entry.key]) for entry in entries]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ref.bib"
        path.write_text(BIB)
        item = review.Review(path, "token")
        with patch.object(check_ads_bib, "check_entries_parallel", counting):
            item.refresh()
            assert asked == [["Smith2020", "Jones2019"]], asked
            path.write_text(BIB.replace("Another study", "Another study, revised"))
            item.refresh()
        assert asked[-1] == ["Jones2019"], asked
        assert len(item.results) == 2


def test_skipped_entries_are_listed_but_never_checked():
    text = "% checkcitation: skip\n@BOOK{Jeffreys1939, title = {Theory of Probability}, year = {1939}}\n\n" + BIB
    with tempfile.TemporaryDirectory() as tmp:
        payload = session(tmp, MISMATCH, text=text).payload()
        skipped = [entry for entry in payload["entries"] if entry["skip"]]
        assert [entry["key"] for entry in skipped] == ["Jeffreys1939"]
        assert skipped[0]["status"] == "SKIPPED" and payload["skipped"] == 1
        assert skipped[0]["auto"] is False and skipped[0]["issueish"] is False


def test_tex_crosscheck_reports_both_directions():
    with tempfile.TemporaryDirectory() as tmp:
        tex = Path(tmp) / "paper.tex"
        tex.write_text("\\citet{Smith2020} \\citep*{Missing2001} % \\cite{Ignored1999}\n")
        payload = session(tmp, MISMATCH, tex=[tex]).payload()["tex"]
        assert payload["undefined"] == ["Missing2001"], payload["undefined"]
        assert payload["uncited"] == ["Jones2019"], payload["uncited"]
        assert payload["unreadable"] == []


def test_an_unreadable_tex_file_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        payload = session(tmp, MISMATCH, tex=[Path(tmp) / "gone.tex"]).payload()["tex"]
        assert len(payload["unreadable"]) == 1 and "gone.tex" in payload["unreadable"][0]


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
