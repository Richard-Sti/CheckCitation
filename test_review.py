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


def session(tmp, statuses, text=BIB, tex=(), name="ref.bib"):
    path = Path(tmp) / name
    path.write_text(text)
    # The store lives with the tool, so tests must not touch the real one.
    item = review.Review(path, "token", tex=tex, store=Path(tmp) / ".checked.json")
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


def test_accepting_an_entry_outlives_the_tab():
    """The point of the file: a decision must not die when the browser closes."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            payload = post("/api/accept", {"key": "Smith2020", "accepted": True})
        finally:
            stop()
        smith = {e["key"]: e for e in payload["entries"]}["Smith2020"]
        assert smith["accepted"] is True and smith["issueish"] is True, "still an issue, just not an open one"
        assert item.store.exists() and not (item.path.parent / "ref.checked.json").exists(), \
            "nothing may be written beside the .bib; that is someone else's repository"
        assert str(item.path) in json.loads(item.store.read_text())["files"], "keyed by the .bib's own path"

        # A fresh session on the same file, as if the tool were re-run tomorrow.
        again = session(tmp, MISMATCH)
        assert again.is_accepted(again.entries[0]), "the acceptance did not survive a restart"
        assert again.accepted_error == ""


def test_editing_an_accepted_entry_raises_it_again():
    """The judgement was about that text. Change the text and it no longer applies."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        item.accept("Smith2020")
        assert item.is_accepted(item.entries[0])
        item.path.write_text(BIB.replace("A study of galaxies", "A different study entirely"))
        item.refresh()
        assert not item.is_accepted(item.entries[0]), "an edited entry must come back to the queue"


def test_an_acceptance_can_be_withdrawn():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        item.accept("Smith2020")
        item.accept("Smith2020", accepted=False)
        assert not item.is_accepted(item.entries[0])
        assert json.loads(item.store.read_text())["files"] == {}


def test_an_acceptance_expires():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        item.accept("Smith2020")
        item.accepted["Smith2020"]["at"] -= review.ACCEPTED_TTL + 1
        assert not item.is_accepted(item.entries[0]), "a stale acceptance must be re-asked"


def test_a_damaged_checked_file_is_reported_not_overwritten():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        path = item.store
        path.write_text("{not json")
        again = session(tmp, MISMATCH)
        assert again.accepted_error and "checked.json" in again.accepted_error
        assert again.payload()["accepted_error"] == again.accepted_error, "the page has to say so"
        again.accept("Smith2020")
        assert path.read_text() == "{not json", "a file that could not be read must not be replaced"


def test_accepting_an_unknown_key_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            for body, code in (({"key": "Nope2020"}, 400), ({}, 400)):
                try:
                    post("/api/accept", body)
                except HTTPError as exc:
                    assert exc.code == code, (body, exc.code)
                else:
                    assert False, f"accepted {body}"
        finally:
            stop()


def test_two_bibliographies_never_share_acceptances():
    """Same stem, different directory, different paper - and one store for both."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / ".checked.json"
        one, two = Path(tmp) / "a", Path(tmp) / "b"
        sessions = []
        for folder in (one, two):
            folder.mkdir()
            path = folder / "ref.bib"
            path.write_text(BIB)
            item = review.Review(path, "token", store=store)
            with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
                item.refresh()
            sessions.append(item)

        sessions[0].accept("Smith2020")
        assert sessions[0].is_accepted(sessions[0].entries[0])

        reopened = review.Review(two / "ref.bib", "token", store=store)
        with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
            reopened.refresh()
        assert not reopened.is_accepted(reopened.entries[0]), "the other paper's decision leaked"

        # And saving the second must not drop the first.
        sessions[1].accept("Jones2019")
        files = json.loads(store.read_text())["files"]
        first, second = str((one / "ref.bib").resolve()), str((two / "ref.bib").resolve())
        assert set(files) == {first, second}, files
        assert list(files[first]) == ["Smith2020"], "saving the second dropped the first"
        assert list(files[second]) == ["Jones2019"]


def test_replacing_an_entry_never_puts_it_straight_back_in_the_queue():
    """Some conflicts no rewrite can clear, so acting on one has to settle it."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
            notice = item.replace("Smith2020", ADS_EXPORT)
        smith = next(e for e in item.entries if e.key == "Smith2020")
        assert item.is_accepted(smith), "the card would come straight back"
        assert "marked checked" in notice, notice
        payload = {e["key"]: e for e in item.payload()["entries"]}["Smith2020"]
        assert payload["accepted"] is True
        # And the acceptance is about the new text, so a later edit re-opens it.
        item.path.write_text(item.path.read_text().replace("A study of galaxies", "Something else"))
        item.refresh()
        assert not item.is_accepted(next(e for e in item.entries if e.key == "Smith2020"))


def test_undoing_a_replacement_also_undoes_the_acceptance():
    """Undo is rejecting the replacement, so it must not leave the entry suppressed."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        original = next(e for e in item.entries if e.key == "Smith2020").raw
        with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
            item.replace("Smith2020", ADS_EXPORT)
            assert item.is_accepted(next(e for e in item.entries if e.key == "Smith2020"))
            notice = item.replace("Smith2020", original, settle=False)
        smith = next(e for e in item.entries if e.key == "Smith2020")
        assert smith.raw == original, "the file was not restored"
        assert not item.is_accepted(smith), "an undone entry must come back to the queue"
        assert "restored" in notice, notice


def test_clearing_decisions_touches_only_this_bibliography():
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / ".checked.json"
        other = Path(tmp) / "other"
        other.mkdir()
        (other / "ref.bib").write_text(BIB)
        neighbour = review.Review(other / "ref.bib", "token", store=store)
        with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
            neighbour.refresh()
        neighbour.accept("Smith2020")

        path = Path(tmp) / "ref.bib"
        path.write_text(BIB)
        item = review.Review(path, "token", store=store)
        with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
            item.refresh()
        item.accept("Smith2020")
        item.accept("Jones2019")

        base, get, post, stop = serving(item)
        try:
            payload = post("/api/clear", {})
        finally:
            stop()
        assert "Cleared 2 decisions" in payload["notice"], payload["notice"]
        assert not any(e["accepted"] for e in payload["entries"])
        assert item.path.read_text() == BIB, "clearing decisions must not touch the .bib"
        files = json.loads(store.read_text())["files"]
        assert list(files) == [str((other / "ref.bib").resolve())], "the other bibliography lost its decisions"


def test_the_page_is_not_told_to_run_a_command_line_flag():
    """The card has buttons for exactly what ISSUE_ACTIONS describes."""
    assert review.gui_action("ADS_RECORD_CONFLICT").startswith("Review the ADS-exported")
    assert "--replace" not in review.gui_action("ADS_RECORD_CONFLICT")
    assert all("--replace" not in review.gui_action(s) for s in check_ads_bib.STATUS_ORDER)
    # Advice that is not about the flag is passed through untouched.
    assert review.gui_action("RATE_LIMITED") == check_ads_bib.ISSUE_ACTIONS["RATE_LIMITED"]


def test_reloading_really_re_reads_the_file():
    """"Reload from disk" used to hand back the previous parse with a fresh hash.

    If-Match then passed while every offset was stale, so the write was refused
    for ever and nothing but a full Re-check could escape.
    """
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            item.path.write_text("% an edit made in an editor\n" + BIB)
            with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
                reloaded = get("/api/state")
                assert reloaded["entries"][0]["line"] == 2, "the reload did not re-read the file"
                payload = post("/api/replace", {"key": "Smith2020", "bibtex": ADS_EXPORT},
                               revision=reloaded["revision"])
            assert payload["replaced"] == 1, "the write was still refused after reloading"
            assert item.path.read_text().startswith("% an edit made in an editor"), "the edit was clobbered"
        finally:
            stop()


def test_a_replacement_may_not_rename_the_entry():
    """`replace_bibtex_key` rewrites the first `@kind{key,`, which may be a @comment."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        before = item.path.read_bytes()
        try:
            item.replace("Smith2020", "@comment{note,}\n@ARTICLE{2020ApJ...900....1S, title = {T}}")
        except ValueError as exc:
            assert "would rename Smith2020" in str(exc), exc
        else:
            assert False, "the citation key was silently changed"
        assert item.path.read_bytes() == before, "the file was touched anyway"


def test_a_store_that_cannot_be_written_is_not_reported_as_saved():
    """Telling someone a decision is recorded when it is not loses their work."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        item.store.parent.joinpath("blocked").mkdir()
        item.store = item.store.parent / "blocked"  # a directory: the write must fail
        item.accept("Smith2020")
        assert item.accepted_error, "the failure was swallowed"
        assert "not being recorded" in item.accepted_error
        assert item.payload()["accepted_error"] == item.accepted_error, "the page has to be told"


def test_an_entry_that_already_is_the_ads_export_offers_no_replacement():
    """Clicking Replace here rewrites the same bytes: nothing changes, and the real
    disagreement - a citation key, a bad eprint - is not something a body can fix."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ref.bib"
        path.write_text(BIB)
        entry = next(e for e in check_ads_bib.parse_bibtex(path) if e.key == "Smith2020")
        # The export is exactly what is already in the file.
        export = entry.raw.replace("@ARTICLE{Smith2020,", "@ARTICLE{2020ApJ...900....1S,")
        statuses = {
            "Smith2020": result("ADS_RECORD_CONFLICT", matches=[{"bibcode": "2020ApJ...900....1S"}], ads_bibtex=export),
            "Jones2019": result("OK", matches=[{"bibcode": "2019ApJ...800....2J"}]),
        }
        item = session(tmp, statuses)
        smith = {e["key"]: e for e in item.payload()["entries"]}["Smith2020"]
        assert smith["identical"] is True, "the proposal is the file; the card must say so"
        assert smith["ads_bibtex"].strip() == entry.raw.strip()

        # An entry whose export really does differ is unaffected.
        other = {e["key"]: e for e in session(tmp, MISMATCH).payload()["entries"]}["Smith2020"]
        assert other["identical"] is False


def test_a_commit_writes_every_staged_edit_in_one_pass():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        jones = ADS_EXPORT.replace("2020ApJ...900....1S", "2019ApJ...800....2J").replace(
            "A study of galaxies", "Another study")
        base, get, post, stop = serving(item)
        try:
            with patch.object(check_ads_bib, "check_entries_parallel", canned(MISMATCH)):
                payload = post("/api/commit", {"edits": {"Smith2020": ADS_EXPORT, "Jones2019": jones}})
        finally:
            stop()
        text = item.path.read_text()
        assert "@ARTICLE{Smith2020," in text and "@ARTICLE{Jones2019," in text, "keys must survive"
        assert "2020ApJ...900....1S," not in text and "2019ApJ...800....2J," not in text
        assert payload["replaced"] == 2
        assert "Wrote 2 changes" in payload["notice"], payload["notice"]
        backups = sorted(q.name for q in item.path.parent.glob("ref.bib.bak*"))
        assert backups == ["ref.bib.bak"], f"one write, one backup, not {backups}"


def test_one_bad_edit_writes_none_of_them():
    """The file must never be left holding half a review."""
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        before = item.path.read_bytes()
        base, get, post, stop = serving(item)
        try:
            for edits in (
                {"Smith2020": ADS_EXPORT, "Nope2020": ADS_EXPORT},                       # unknown key
                {"Smith2020": ADS_EXPORT, "Jones2019": ADS_EXPORT + ADS_EXPORT},         # two entries
                {"Smith2020": "@comment{note,}\n@ARTICLE{2020ApJ...900....1S, title = {T}}"},  # renames
                {},                                                                       # nothing staged
            ):
                try:
                    post("/api/commit", {"edits": edits})
                except HTTPError as exc:
                    assert exc.code == 400, (edits, exc.code)
                else:
                    assert False, f"accepted {list(edits)}"
                assert item.path.read_bytes() == before, "a rejected commit touched the file"
            assert not list(item.path.parent.glob("ref.bib.bak*")), "a rejected commit left a backup"
        finally:
            stop()


def test_a_stale_tab_cannot_commit():
    with tempfile.TemporaryDirectory() as tmp:
        item = session(tmp, MISMATCH)
        base, get, post, stop = serving(item)
        try:
            stale = get("/api/state")["revision"]
            item.path.write_text(BIB.replace("Another study", "Another study, revised"))
            before = item.path.read_bytes()
            try:
                post("/api/commit", {"edits": {"Smith2020": ADS_EXPORT}}, revision=stale)
            except HTTPError as exc:
                assert exc.code == 409, exc.code
            else:
                assert False, "a stale tab committed"
            assert item.path.read_bytes() == before
        finally:
            stop()


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
