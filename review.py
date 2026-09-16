#!/usr/bin/env python3
"""Browser review app for check_ads_bib: work through ADS issues and apply replacements.

    ./check_ads_bib.sh ref.bib --review            # check, then open the app
    ./check_ads_bib.sh ref.bib --review --no-open  # don't launch a browser

Stdlib only, bound to 127.0.0.1, no auth. The one file it writes is the .bib you
pointed it at, always behind a backup and an atomic replace, and never while the
file on disk has changed under the open tab.
"""
import json
import sys
import threading
import time
import webbrowser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import check_ads_bib as ads

PORT = 8766
# Every request reads or writes the .bib; serialise them.
LOCK = threading.RLock()
HTML = Path(__file__).resolve().parent / "review.html"
MAX_BODY = 10_000_000
ADS_FIELDS = ("title", "author", "year", "doi", "eprint")
# An entry you have looked at and accepted stays accepted for as long as an ADS
# response stays cached. Long enough not to be asked again while writing a paper.
ACCEPTED_TTL = ads.DEFAULT_CACHE_TTL


# One store for every .bib, in the tool's own directory rather than beside each
# file: reviewing a paper's ref.bib must not leave an untracked file in the paper's
# repository. Entries are keyed by absolute path, so two ref.bib files never mix.
CHECKED_PATH = Path(__file__).resolve().parent / ".checked.json"


def fingerprint(entry):
    """What was accepted, exactly. Edit the entry and the judgement no longer applies."""
    return sha256(entry.raw.encode("utf-8")).hexdigest()


class Review:
    """One review session: the file, the ADS settings, and the current results.

    The .bib file is the only state. There is no decisions cache, because every
    decision here is either a write to that file or a dismissal that means
    nothing once the tab is closed.
    """

    def __init__(self, path, token, rows=5, timeout=20.0, sleep=ads.DEFAULT_SLEEP, jobs=1, tex=(), store=CHECKED_PATH):
        self.path = Path(path).resolve()
        self.store = Path(store)
        self.token = token
        self.rows = rows
        self.timeout = timeout
        self.sleep = sleep
        self.jobs = jobs
        self.tex = [Path(p) for p in tex]
        self.entries = []
        self.results = []
        self.backup = None
        self.replaced = 0
        self.accepted, self.accepted_error = self._load_accepted()

    def _load_accepted(self):
        """Entries already reviewed and accepted, and why the file could not be read.

        A damaged file is reported and then left alone: overwriting it is the one
        way to lose the judgements it holds.
        """
        data, error = self._read_store()
        if error:
            return {}, error
        accepted = data.get("files", {}).get(str(self.path))
        return accepted if isinstance(accepted, dict) else {}, ""

    def _read_store(self):
        """The whole store, or why it could not be read."""
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "files": {}}, ""
        except (OSError, ValueError) as exc:
            return {}, f"Could not read {self.store.name} ({exc}); accepted entries are not being recorded."
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return {}, f"{self.store.name} is not in the expected shape; accepted entries are not being recorded."
        return data, ""

    def _save_accepted(self):
        """Rewrite this file's slice of the store, leaving every other file's alone."""
        if self.accepted_error:
            return
        data, error = self._read_store()
        if error:
            # Something damaged the store since startup; never overwrite it.
            self.accepted_error = error
            return
        files = data.setdefault("files", {})
        if self.accepted:
            files[str(self.path)] = self.accepted
        else:
            files.pop(str(self.path), None)
        data["version"] = 1
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            if not self.store.exists():
                self.store.touch()
            ads.write_text_atomically(self.store, json.dumps(data, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            # Never fail the request over this - the .bib write may already have
            # happened - but the page must not report a decision that is not on disk.
            self.accepted_error = f"Could not write {self.store.name} ({exc}); decisions are not being recorded."

    def is_accepted(self, entry):
        record = self.accepted.get(entry.key)
        if not isinstance(record, dict) or record.get("hash") != fingerprint(entry):
            return False
        at = record.get("at")
        return isinstance(at, (int, float)) and time.time() - at <= ACCEPTED_TTL

    def clear_accepted(self):
        """Forget every decision recorded for this .bib. Other files keep theirs."""
        count = len(self.accepted)
        self.accepted = {}
        self._save_accepted()
        return f"Cleared {count} decision{'' if count == 1 else 's'} for {self.path.name}."

    def accept(self, key, accepted=True):
        """Record, or withdraw, "I looked at this one and it is fine"."""
        entry = self.entry_for(key)
        if entry is None:
            raise KeyError(f"no entry with key {key}")
        if accepted:
            status = next((r.status for e, r in self.results if e.key == key), "")
            self.accepted[key] = {"hash": fingerprint(entry), "at": time.time(), "status": status}
        else:
            self.accepted.pop(key, None)
        self._save_accepted()
        return f"{key} {'accepted as correct' if accepted else 'put back in the queue'}"

    def revision(self):
        """Hash of the file the open tab is reviewing.

        Entry offsets are byte positions into this exact text, so an edit made in
        an editor while the app is open invalidates every pending replacement.
        """
        return sha256(self.path.read_bytes()).hexdigest()

    def adopt(self, results):
        """Take the results the CLI already computed, so startup costs no ADS calls."""
        self.entries = ads.parse_bibtex(self.path)
        self.results = list(results)

    def refresh(self, recheck=frozenset()):
        """Re-parse the file and re-check only what changed.

        One rewrite moves every later entry's offsets, so a write always re-parses.
        An entry whose raw text is untouched keeps its result and costs no ADS call;
        `recheck` forces the ones that must go back to ADS anyway.
        """
        self.entries = ads.parse_bibtex(self.path)
        previous = {(entry.key, entry.raw): result for entry, result in self.results}
        checked = [entry for entry in self.entries if not entry.skip]
        # Positions, not keys: a .bib with two entries under one key still has to
        # come back with one result per entry.
        stale = [
            index
            for index, entry in enumerate(checked)
            if entry.key in recheck or (entry.key, entry.raw) not in previous
        ]
        results = [previous.get((entry.key, entry.raw)) for entry in checked]
        for index, result in zip(stale, self._check([checked[index] for index in stale])):
            results[index] = result
        self.results = list(zip(checked, results))

    def _check(self, entries):
        if not entries:
            return []
        try:
            return [
                result
                for _, result in ads.check_entries_parallel(
                    entries,
                    self.token,
                    rows=self.rows,
                    timeout=self.timeout,
                    sleep=self.sleep,
                    jobs=self.jobs,
                    progress=False,
                )
            ]
        except ads.AdsRateLimitError as exc:
            return [ads.rate_limited_result(entry, exc.wait) for entry in entries]

    def entry_for(self, key):
        """The one entry with this key, or a ValueError naming why there isn't one.

        Two entries sharing a key is a real .bib bug, and a replacement by key
        would silently pick one of them; refuse instead.
        """
        matches = [entry for entry in self.entries if entry.key == key]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} entries share the key {key}; remove the duplicate first")
        return matches[0]

    def replace(self, key, bibtex, settle=True):
        """Write one reviewed replacement into the .bib, keeping the citation key.

        `settle` is false for an undo: putting the old text back is rejecting the
        replacement, so it must also withdraw the acceptance the replacement made,
        or the entry would be silently suppressed in the state you just restored.
        """
        entry = self.entry_for(key)
        if entry is None:
            raise KeyError(f"no entry with key {key}")
        replacement = ads.replace_bibtex_key(bibtex.strip(), key)
        parsed = ads.parse_bibtex_text(replacement)
        if len(parsed) != 1:
            raise ValueError(f"replacement must be exactly one BibTeX entry, not {len(parsed)}")
        # replace_bibtex_key rewrites the first `@kind{key,` it sees, which may be a
        # `@comment{...}` wrapper that parsing then drops - leaving the real entry
        # under the pasted key and every \cite{} to it broken.
        if parsed[0].key != key:
            raise ValueError(
                f"the replacement would rename {key} to {parsed[0].key}; "
                "remove any @comment or @string before the entry"
            )
        text = self.path.read_text(encoding="utf-8")
        if text[entry.start : entry.end] != entry.raw:
            raise ValueError("the .bib changed on disk; reload before applying edits")
        self.backup = ads.ensure_backup(self.path, self.backup)
        ads.write_text_atomically(self.path, ads.apply_replacements(text, [(entry, replacement)]))
        self.replaced += 1
        self.refresh(recheck={key})
        # You have acted on this entry. If ADS still disagrees after the rewrite,
        # it must not come straight back to the top of the queue - some conflicts
        # no replacement can clear, because the citation key is kept by design.
        if not settle:
            self.accept(key, accepted=False)
            return f"{key} restored to what was in the file"
        settled = ""
        if any(e.key == key and r.status in ads.ISSUE_STATUSES for e, r in self.results):
            self.accept(key)
            settled = "; it still differs from ADS, so it is marked checked rather than re-queued"
        return f"{key} replaced; backup at {self.backup.name}{settled}"

    def payload(self, notice=""):
        counts = {}
        for _, result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        skipped = [entry for entry in self.entries if entry.skip]
        return {
            "source": self.path.name,
            "path": str(self.path),
            "revision": self.revision(),
            "notice": notice,
            "replaced": self.replaced,
            "backup": self.backup.name if self.backup else "",
            "accepted_error": self.accepted_error,
            "entries": [describe(entry, result, self.is_accepted(entry)) for entry, result in self.results]
            + [skipped_entry(entry) for entry in skipped],
            "counts": ads.ordered_counts(counts),
            "skipped": len(skipped),
            "duplicates": duplicates(self.results),
            "warnings": warnings(self.entries),
            "tex": tex_crosscheck(self.entries, self.tex),
            "statuses": list(ads.STATUS_ORDER),
        }


def gui_action(status):
    """The report's advice, without the flag that names the other interface.

    `ISSUE_ACTIONS` is written for the CLI, and the card has buttons for exactly
    what it describes; telling someone to run --replace inside the app is noise.
    """
    action = ads.ISSUE_ACTIONS.get(status, "")
    prefix = "use --replace to "
    if action.startswith(prefix):
        action = action[len(prefix):]
        return action[:1].upper() + action[1:]
    return action


def describe(entry, result, accepted=False):
    """Everything the CLI prints about one entry, plus what it only computes and drops."""
    ads_entry = ads.parsed_ads_entry(entry, result.ads_bibtex) if result.ads_bibtex else None
    conflicts = ads.identity_conflicts(entry, ads_entry) if ads_entry else []
    candidate = ads.ads_replacement_bibcode(result)
    return {
        "key": entry.key,
        "kind": entry.kind,
        "line": entry.line,
        "raw": entry.raw,
        "status": result.status,
        "message": result.message,
        "query": result.query,
        "issue": ads.ISSUE_DESCRIPTIONS.get(result.status, ""),
        "action": gui_action(result.status),
        "local": {field: entry.fields.get(field, "") for field in ADS_FIELDS},
        "bibcode": ads.ads_bibcode(entry) or "",
        "ads": {field: ads_entry.fields.get(field, "") for field in ADS_FIELDS} if ads_entry else None,
        "ads_bibtex": ads.replace_bibtex_key(result.ads_bibtex, entry.key) if result.ads_bibtex else "",
        "conflicts": conflicts,
        "matches": [
            {
                "bibcode": str(match.get("bibcode", "")),
                "year": str(match.get("year", "")),
                "title": ads.match_title(match),
                "doi": ", ".join(str(item) for item in match.get("doi", []) or []),
                "url": ads.ads_abstract_url(str(match.get("bibcode", ""))),
            }
            for match in result.matches
        ],
        "candidate": candidate or "",
        # Set only for the statuses where no route resolved, so the page can hand
        # the entry over to a human instead of showing an empty card.
        "search_url": ads.issue_search_url(entry, result) if result.status in ads.HANDOFF_STATUSES else "",
        # The CLI's own rule: one keypress when the ADS export is in hand and
        # disagrees with nothing. Anything that may be a different paper, or whose
        # export has not been fetched yet, needs a click.
        "auto": bool(candidate) and ads_entry is not None and not conflicts,
        "manual": result.status in ads.MANUAL_REPLACEMENT_STATUSES,
        "issueish": result.status in ads.ISSUE_STATUSES,
        "accepted": accepted,
        "skip": False,
    }


def skipped_entry(entry):
    return {
        "key": entry.key,
        "kind": entry.kind,
        "line": entry.line,
        "raw": entry.raw,
        "status": "SKIPPED",
        "message": "'% checkcitation: skip' directive on the line above",
        "query": "",
        "issue": "",
        "action": "",
        "local": {field: entry.fields.get(field, "") for field in ADS_FIELDS},
        "bibcode": ads.ads_bibcode(entry) or "",
        "ads": None,
        "ads_bibtex": "",
        "conflicts": [],
        "matches": [],
        "candidate": "",
        "search_url": "",
        "auto": False,
        "manual": False,
        "issueish": False,
        "accepted": False,
        "skip": True,
    }


def duplicates(results):
    return [
        {"bibcode": identity[0], "title": identity[1], "keys": [entry.key for entry in entries]}
        for identity, entries in ads.duplicate_groups(results)
    ]


def warnings(entries):
    return [
        {
            "key": entry.key,
            "line": entry.line,
            "issue": "author field contains a literal 'et al.', which renders as '(Smith & et al. 2020)'",
        }
        for entry in entries
        if ads.malformed_author(entry)
    ]


def tex_crosscheck(entries, tex_paths):
    if not tex_paths:
        return None
    cited = set()
    unreadable = []
    for path in tex_paths:
        try:
            cited |= ads.cited_keys(path.read_text(encoding="utf-8"))
        except OSError as exc:
            unreadable.append(f"{path}: {exc}")
    defined = {entry.key for entry in entries}
    return {
        "sources": [str(path) for path in tex_paths],
        "unreadable": unreadable,
        "undefined": sorted(cited - defined - {"*"}),
        "uncited": sorted(defined - cited),
    }


def handler_for(review):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype, no_store=False):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if no_store or ctype == "application/json":
                self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def _error(self, code, message):
            self._json(code, {"error": message})

        def _state(self, notice=""):
            self._json(200, review.payload(notice))

        def guarded(self, fn):
            """Turn a bad .bib, a missing key or an unreadable file into a readable 400."""
            try:
                return fn()
            except (ValueError, TypeError, KeyError, OSError) as exc:
                detail = exc.args[0] if exc.args else type(exc).__name__
                return self._error(400, str(detail))

        def do_GET(self):
            return self.guarded(self._get)

        def do_POST(self):
            with LOCK:
                return self.guarded(self._post)

        def _get(self):
            url = urlsplit(self.path)
            if url.path == "/":
                # no-store: the page is read from disk on every request, so a
                # cached copy in the browser is the one way to see stale UI.
                return self._send(200, HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8", no_store=True)
            if url.path == "/api/state":
                with LOCK:
                    # Re-parse, or "Reload from disk" hands back the previous parse
                    # with a fresh revision: If-Match then passes while every entry
                    # offset is stale, and the write is refused for ever. An entry
                    # whose own text is unchanged keeps its verdict, so an edit
                    # somewhere else in the file costs no ADS request.
                    review.refresh()
                    return self._state()
            if url.path == "/api/bibtex":
                query = parse_qs(url.query)
                bibcode = (query.get("bibcode") or [""])[0]
                key = (query.get("key") or [""])[0]
                if not bibcode or not key:
                    return self._error(400, "bibcode and key are both required")
                try:
                    export = ads.ads_export_bibtex(bibcode, review.token, review.timeout)
                except Exception as exc:  # noqa: BLE001 - any ADS failure is the same answer here
                    return self._error(502, f"could not fetch the ADS export for {bibcode}: {exc}")
                bibtex = ads.replace_bibtex_key(export, key)
                entry = review.entry_for(key)
                ads_entry = ads.parsed_ads_entry(entry, export) if entry else None
                conflicts = ads.identity_conflicts(entry, ads_entry) if ads_entry else ["unparsed"]
                # Same gate as a result that arrived with its export attached.
                return self._json(200, {"bibtex": bibtex, "conflicts": conflicts, "auto": not conflicts})
            return self._send(404, "not found", "text/plain")

        def _post(self):
            url = urlsplit(self.path)
            if url.path not in {"/api/replace", "/api/recheck", "/api/accept", "/api/clear"}:
                return self._send(404, "not found", "text/plain")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._error(400, "expected Content-Type: application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._error(400, "bad Content-Length")
            if not 0 <= length <= MAX_BODY:
                return self._error(400, "request body is too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                return self._error(400, f"body is not JSON: {exc}")
            if not isinstance(body, dict):
                return self._error(400, "body must be a JSON object")

            if url.path == "/api/recheck":
                # No If-Match: re-reading the file is how a stale tab recovers.
                review.refresh(recheck={entry.key for entry in review.entries})
                return self._state("Re-checked every entry against ADS.")

            if self.headers.get("If-Match") != review.revision():
                return self._error(409, "The .bib changed since this tab loaded it. Reload, then retry the edit.")

            if url.path == "/api/clear":
                return self._state(review.clear_accepted())

            if url.path == "/api/accept":
                key = body.get("key")
                if not isinstance(key, str):
                    return self._error(400, "accept needs a key")
                return self._state(review.accept(key, bool(body.get("accepted", True))))

            key = body.get("key")
            bibtex = body.get("bibtex")
            if not isinstance(key, str) or not isinstance(bibtex, str) or not bibtex.strip():
                return self._error(400, "replace needs a key and a non-empty bibtex string")
            return self._state(review.replace(key, bibtex, settle=not body.get("undo")))

        def log_message(self, *args):
            pass  # a request per keypress; the log is noise

    return Handler


def serve(review, open_browser=True):
    # Claim the port before printing anything, so a failed start cannot look
    # like it half worked.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), handler_for(review))
    except OSError as exc:
        sys.exit(
            f"port {PORT} is already in use.\n"
            f"  if the review app is already running, it is at http://localhost:{PORT}\n"
            f"  otherwise find the stray process and stop it:\n"
            f"    lsof -nP -iTCP:{PORT} -sTCP:LISTEN\n"
            f"  ({exc})"
        )
    print(f"\nReviewing {review.path}")
    print(f"http://localhost:{PORT}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Every applied replacement is already on disk.")
    finally:
        server.server_close()
