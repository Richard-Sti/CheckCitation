#!/usr/bin/env python3
"""Check whether BibTeX entries can be resolved on NASA ADS."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import TypeVar

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


ADS_API = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_BIBTEX_API = "https://api.adsabs.harvard.edu/v1/export/bibtex"
DEFAULT_JOBS = 1
DEFAULT_SLEEP = 0.1
DEFAULT_RETRIES = 4
DEFAULT_RETRY_WAIT = 10.0
MAX_RETRY_WAIT = 60.0
DEFAULT_CACHE_PATH = Path(__file__).with_name(".ads_cache.json")
DEFAULT_CACHE_TTL = 24 * 60 * 60
BIBCODE_RE = re.compile(r"/abs/([^/?#]+)")
ENTRY_RE = re.compile(r"@(?P<kind>[A-Za-z]+)\s*{\s*(?P<key>[^,\s]+)\s*,", re.M)
FIELD_RE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=", re.M)
STATUS_ORDER = (
    "OK",
    "ADS_BIBTEX_MISMATCH",
    "NON_ADS_BIBTEX",
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_CONFLICT",
    "BIBCODE_MISMATCH",
    "IDENTIFIER_MISMATCH",
    "ADS_UNVERIFIED_RATE_LIMITED",
    "RATE_LIMITED",
    "MISSING",
    "AMBIGUOUS",
    "NO_IDENTIFIER",
    "ERROR",
)
ISSUE_STATUSES = {
    "ADS_BIBTEX_MISMATCH",
    "NON_ADS_BIBTEX",
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_CONFLICT",
    "BIBCODE_MISMATCH",
    "IDENTIFIER_MISMATCH",
    "ADS_UNVERIFIED_RATE_LIMITED",
    "RATE_LIMITED",
    "MISSING",
    "AMBIGUOUS",
    "NO_IDENTIFIER",
    "ERROR",
}
ISSUE_DESCRIPTIONS = {
    "ADS_BIBTEX_MISMATCH": "the local entry has ADS provenance, but its BibTeX fields differ from the current ADS export",
    "NON_ADS_BIBTEX": "the entry resolves to ADS, but it has no local ADS bibcode or adsurl",
    "ADS_RECORD_CONFLICT": "the local ADS bibcode points to an ADS export whose title, DOI, or eprint disagrees with the local entry",
    "IDENTIFIER_CONFLICT": "different identifiers in this entry resolve to different ADS records",
    "BIBCODE_MISMATCH": "the local ADS bibcode and DOI/arXiv/title lookup point to different ADS records",
    "IDENTIFIER_MISMATCH": "at least one identifier resolves to ADS, but another identifier in the same entry does not",
    "ADS_UNVERIFIED_RATE_LIMITED": "ADS rate-limited the live check before this ADS-linked entry could be freshly verified",
    "RATE_LIMITED": "ADS rate-limited the live check before this entry could be resolved",
    "MISSING": "ADS returned no records for the available lookup queries",
    "AMBIGUOUS": "ADS returned more than one possible record for the lookup query",
    "NO_IDENTIFIER": "the entry has no bibcode, adsurl, DOI, eprint, or title+year lookup",
    "ERROR": "an ADS request or local worker failed before the entry could be checked",
}
ISSUE_ACTIONS = {
    "ADS_BIBTEX_MISMATCH": "use --replace to review the ADS-exported replacement, or edit the local entry manually",
    "NON_ADS_BIBTEX": "use --replace to review adding the ADS-exported entry while keeping the citation key",
    "ADS_RECORD_CONFLICT": "use --replace to review the ADS-exported replacement, paste a manual replacement, or skip",
    "IDENTIFIER_CONFLICT": "use --replace to paste a reviewed replacement after deciding which identifier is intended",
    "BIBCODE_MISMATCH": "use --replace to paste a reviewed replacement after deciding whether the local ADS record or identifier-resolved record is intended",
    "IDENTIFIER_MISMATCH": "use --replace to review the ADS-exported replacement, paste a manual replacement, or skip",
    "ADS_UNVERIFIED_RATE_LIMITED": "rerun after the ADS cooldown expires; automatic replacement is disabled",
    "RATE_LIMITED": "rerun after the ADS cooldown expires, preferably with fewer workers or a longer --sleep",
    "MISSING": "use --replace to paste a replacement, or edit the DOI/arXiv/title/year fields manually",
    "AMBIGUOUS": "use --replace to paste the intended record, or add a DOI, arXiv ID, or ADS bibcode/adsurl",
    "NO_IDENTIFIER": "use --replace to paste a replacement, or add a DOI, arXiv ID, ADS bibcode/adsurl, or title plus year",
    "ERROR": "rerun later; if this repeats, inspect the reported request error",
}
AUTOMATIC_REPLACEMENT_STATUSES = {"ADS_BIBTEX_MISMATCH", "NON_ADS_BIBTEX"}
ADS_REPLACEMENT_STATUSES = AUTOMATIC_REPLACEMENT_STATUSES | {
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_MISMATCH",
}
MANUAL_REPLACEMENT_STATUSES = {
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_CONFLICT",
    "BIBCODE_MISMATCH",
    "IDENTIFIER_MISMATCH",
    "MISSING",
    "AMBIGUOUS",
    "NO_IDENTIFIER",
}
IGNORED_COMPARISON_FIELDS = {
    "abstract",
    "adsnote",
    "annotation",
    "file",
    "keywords",
    "note",
    "url",
    "urldate",
}
T = TypeVar("T")


@dataclass(frozen=True)
class BibEntry:
    kind: str
    key: str
    fields: dict[str, str]
    line: int
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class AdsResult:
    status: str
    query: str
    matches: list[dict[str, object]]
    message: str = ""
    ads_bibtex: str = ""


@dataclass
class InFlightCall:
    event: Event
    value: object | None = None
    exception: BaseException | None = None


class AdsRateLimitError(Exception):
    def __init__(self, wait: float):
        super().__init__(f"ADS requested a {format_duration(wait)} Retry-After cooldown")
        self.wait = wait


class AdsCache:
    def __init__(self, path: Path, ttl: float, enabled: bool = True, refresh: bool = False):
        self.path = path
        self.ttl = ttl
        self.enabled = enabled
        self.refresh = refresh
        self.lock = Lock()
        self.data: dict[str, object] = {"version": 1, "search": {}, "bibtex": {}}
        if self.enabled:
            self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read ADS cache {self.path}: {exc}; starting with an empty cache.", file=sys.stderr)
            return
        if isinstance(raw, dict):
            self.data["search"] = raw.get("search", {}) if isinstance(raw.get("search"), dict) else {}
            self.data["bibtex"] = raw.get("bibtex", {}) if isinstance(raw.get("bibtex"), dict) else {}

    def namespace(self, name: str) -> dict[str, object]:
        value = self.data.setdefault(name, {})
        if not isinstance(value, dict):
            value = {}
            self.data[name] = value
        return value

    def get(self, namespace: str, key: str) -> object | None:
        if not self.enabled or self.refresh:
            return None
        with self.lock:
            item = self.namespace(namespace).get(key)
            if not isinstance(item, dict):
                return None
            stored_at = item.get("stored_at")
            if not isinstance(stored_at, (int, float)):
                return None
            if self.ttl >= 0 and time.time() - float(stored_at) > self.ttl:
                return None
            return item.get("value")

    def set(self, namespace: str, key: str, value: object) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.namespace(namespace)[key] = {"stored_at": time.time(), "value": value}
            self.write_locked()

    def write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise


ADS_CACHE: AdsCache | None = None
ADS_RATE_LIMIT_EXPIRY: float | None = None
ADS_RATE_LIMIT_LOCK = Lock()
ADS_IN_FLIGHT_LOCK = Lock()
ADS_IN_FLIGHT: dict[tuple[str, str], InFlightCall] = {}
ADS_RUN_CACHE: dict[tuple[str, str], object] = {}


def reset_ads_run_cache() -> None:
    with ADS_IN_FLIGHT_LOCK:
        ADS_IN_FLIGHT.clear()
        ADS_RUN_CACHE.clear()


def ads_cached_value(namespace: str, key: str) -> object | None:
    cached = ADS_CACHE.get(namespace, key) if ADS_CACHE is not None else None
    if cached is not None:
        return cached
    with ADS_IN_FLIGHT_LOCK:
        return ADS_RUN_CACHE.get((namespace, key))


def fetch_ads_once(
    namespace: str,
    key: str,
    fetch: Callable[[], object],
    store: Callable[[object], None],
) -> object:
    request_key = (namespace, key)
    with ADS_IN_FLIGHT_LOCK:
        cached = ADS_RUN_CACHE.get(request_key)
        if cached is not None:
            return cached
        in_flight = ADS_IN_FLIGHT.get(request_key)
        if in_flight is None:
            in_flight = InFlightCall(Event())
            ADS_IN_FLIGHT[request_key] = in_flight
            owner = True
        else:
            owner = False

    if not owner:
        in_flight.event.wait()
        if in_flight.exception is not None:
            raise in_flight.exception
        return in_flight.value

    try:
        value = fetch()
        store(value)
    except BaseException as exc:
        with ADS_IN_FLIGHT_LOCK:
            in_flight.exception = exc
            ADS_IN_FLIGHT.pop(request_key, None)
            in_flight.event.set()
        raise

    with ADS_IN_FLIGHT_LOCK:
        ADS_RUN_CACHE[request_key] = value
        in_flight.value = value
        ADS_IN_FLIGHT.pop(request_key, None)
        in_flight.event.set()
    return value


def note_ads_rate_limit(wait: float) -> None:
    global ADS_RATE_LIMIT_EXPIRY

    with ADS_RATE_LIMIT_LOCK:
        ADS_RATE_LIMIT_EXPIRY = time.time() + wait


def active_ads_rate_limit() -> float | None:
    with ADS_RATE_LIMIT_LOCK:
        if ADS_RATE_LIMIT_EXPIRY is None:
            return None
        remaining = ADS_RATE_LIMIT_EXPIRY - time.time()
        return remaining if remaining > 0 else None


def strip_wrappers(value: str) -> str:
    value = value.strip().rstrip(",").strip()
    if len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return " ".join(value.replace("\n", " ").split())


def find_balanced_end(text: str, start: int) -> int:
    opener = text[start]
    depth = 0
    escaped = False
    first_pos = start + 1 if opener == '"' else start
    for pos in range(first_pos, len(text)):
        char = text[pos]
        if opener == '"':
            if char == '"' and not escaped:
                return pos + 1
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos + 1
    raise ValueError("unterminated braced value")


def parse_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    pos = 0
    while match := FIELD_RE.search(body, pos):
        name = match.group("name").lower()
        pos = match.end()
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break
        if body[pos] in '{"':
            end = find_balanced_end(body, pos)
            raw = body[pos:end]
            pos = end
        else:
            end = pos
            while end < len(body) and body[end] not in ",\n":
                end += 1
            raw = body[pos:end]
            pos = end
        fields[name] = strip_wrappers(raw)
    return fields


def find_entry_end(text: str, start: int) -> int:
    brace = text.find("{", start)
    if brace == -1:
        raise ValueError("entry has no opening brace")
    return find_balanced_end(text, brace)


def parse_bibtex_text(text: str) -> list[BibEntry]:
    entries: list[BibEntry] = []
    for match in ENTRY_RE.finditer(text):
        try:
            end = find_entry_end(text, match.start())
        except ValueError as exc:
            line = text.count("\n", 0, match.start()) + 1
            raise ValueError(f"could not parse entry {match.group('key')} at line {line}: {exc}") from exc
        body = text[match.end() : end - 1]
        line = text.count("\n", 0, match.start()) + 1
        raw = text[match.start() : end]
        entries.append(
            BibEntry(
                kind=match.group("kind"),
                key=match.group("key"),
                fields=parse_fields(body),
                line=line,
                start=match.start(),
                end=end,
                raw=raw,
            )
        )
    return entries


def parse_bibtex(path: Path) -> list[BibEntry]:
    return parse_bibtex_text(path.read_text(encoding="utf-8"))


def ads_bibcode(entry: BibEntry) -> str | None:
    if "bibcode" in entry.fields:
        return entry.fields["bibcode"]
    if "adsurl" in entry.fields:
        match = BIBCODE_RE.search(entry.fields["adsurl"])
        if match:
            return urllib.parse.unquote(match.group(1))
    return None


def escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def candidate_queries(entry: BibEntry, include_bibcode: bool = True) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if include_bibcode and (bibcode := ads_bibcode(entry)):
        queries.append(("bibcode", f'bibcode:"{escape_query_value(bibcode)}"'))
    if doi := entry.fields.get("doi"):
        queries.append(("doi", f'doi:"{escape_query_value(doi)}"'))
        queries.append(("doi_identifier", f'identifier:"{escape_query_value(doi)}"'))
    if eprint := entry.fields.get("eprint"):
        arxiv_id = eprint.removeprefix("arXiv:").strip()
        queries.append(("arxiv", f'identifier:"arXiv:{escape_query_value(arxiv_id)}"'))
    title = entry.fields.get("title")
    year = entry.fields.get("year")
    if title and year:
        clean_title = title.replace("{", "").replace("}", "")
        queries.append(("title", f'title:"{escape_query_value(clean_title)}" year:{year}'))
    return queries


def identifier_query_groups(entry: BibEntry, include_bibcode: bool) -> list[tuple[str, list[tuple[str, str]]]]:
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    if include_bibcode and (bibcode := ads_bibcode(entry)):
        groups.append(("bibcode", [("bibcode", f'bibcode:"{escape_query_value(bibcode)}"')]))
    if doi := entry.fields.get("doi"):
        groups.append(
            (
                "doi",
                [
                    ("doi", f'doi:"{escape_query_value(doi)}"'),
                    ("doi_identifier", f'identifier:"{escape_query_value(doi)}"'),
                ],
            )
        )
    if eprint := entry.fields.get("eprint"):
        arxiv_id = eprint.removeprefix("arXiv:").strip()
        groups.append(("arxiv", [("arxiv", f'identifier:"arXiv:{escape_query_value(arxiv_id)}"')]))
    return groups


def run_queries(
    queries: list[tuple[str, str]],
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    messages: list[str] = []
    for label, query in queries:
        try:
            matches = ads_search(query, token, rows=rows, timeout=timeout, sleep=sleep)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return AdsResult("ERROR", query, [], f"ADS HTTP 429 rate limit for {label} query; rerun later with --jobs 1 --sleep 3")
            return AdsResult("ERROR", query, [], f"ADS HTTP {exc.code} for {label} query")
        except urllib.error.URLError as exc:
            return AdsResult("ERROR", query, [], f"ADS request failed for {label} query: {exc.reason}")
        except TimeoutError:
            return AdsResult("ERROR", query, [], f"ADS request timed out for {label} query")

        if len(matches) == 1:
            return AdsResult("OK", query, matches)
        if len(matches) > 1:
            return AdsResult("AMBIGUOUS", query, matches)
        messages.append(f"{label}:0")

    query = queries[-1][1] if queries else ""
    return AdsResult("MISSING", query, [], ", ".join(messages))


def retry_wait_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return DEFAULT_RETRY_WAIT * (2 ** attempt)


def urlopen_with_retries(request: urllib.request.Request, timeout: float) -> bytes:
    for attempt in range(DEFAULT_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            wait = retry_wait_seconds(exc, attempt)
            if attempt == DEFAULT_RETRIES or wait > MAX_RETRY_WAIT:
                note_ads_rate_limit(wait)
                raise AdsRateLimitError(wait) from exc
            print(f"ADS rate limit hit; retrying in {format_duration(wait)} ({attempt + 1}/{DEFAULT_RETRIES})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable retry state")


def ads_search(query: str, token: str, rows: int, timeout: float, sleep: float = 0.0) -> list[dict[str, object]]:
    cache_key = json.dumps(
        {"fl": "bibcode,title,year,doi,identifier", "query": query, "rows": rows},
        sort_keys=True,
    )
    cached = ads_cached_value("search", cache_key)
    if isinstance(cached, list):
        return [item for item in cached if isinstance(item, dict)]

    def fetch() -> object:
        if (wait := active_ads_rate_limit()) is not None:
            raise AdsRateLimitError(wait)

        if sleep:
            time.sleep(sleep)
        if (wait := active_ads_rate_limit()) is not None:
            raise AdsRateLimitError(wait)

        params = urllib.parse.urlencode(
            {
                "q": query,
                "fl": "bibcode,title,year,doi,identifier",
                "rows": str(rows),
            }
        )
        request = urllib.request.Request(
            f"{ADS_API}?{params}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "check-ads-bib/0.1"},
        )
        payload = json.loads(urlopen_with_retries(request, timeout).decode("utf-8"))
        docs = payload.get("response", {}).get("docs", [])
        return docs if isinstance(docs, list) else []

    def store(value: object) -> None:
        if ADS_CACHE is not None:
            ADS_CACHE.set("search", cache_key, value)

    docs = fetch_ads_once("search", cache_key, fetch, store)
    if not isinstance(docs, list):
        return []
    return [item for item in docs if isinstance(item, dict)]


def ads_export_bibtex(bibcode: str, token: str, timeout: float) -> str:
    cached = ads_cached_value("bibtex", bibcode)
    if isinstance(cached, str) and cached.strip():
        return cached

    def fetch() -> object:
        if (wait := active_ads_rate_limit()) is not None:
            raise AdsRateLimitError(wait)

        quoted_bibcode = urllib.parse.quote(bibcode, safe="")
        request = urllib.request.Request(
            f"{ADS_BIBTEX_API}/{quoted_bibcode}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "check-ads-bib/0.1"},
        )
        body = urlopen_with_retries(request, timeout).decode("utf-8")
        try:
            payload = json.loads(body)
            export = str(payload.get("export", "")).strip()
        except json.JSONDecodeError:
            export = body.strip()
        if not export:
            raise RuntimeError(f"ADS returned no BibTeX export for {bibcode}")
        return export

    def store(value: object) -> None:
        if ADS_CACHE is not None:
            ADS_CACHE.set("bibtex", bibcode, value)

    export = fetch_ads_once("bibtex", bibcode, fetch, store)
    if not isinstance(export, str) or not export.strip():
        raise RuntimeError(f"ADS returned no BibTeX export for {bibcode}")
    return export


def normalize_field_value(value: str) -> str:
    return " ".join(value.split())


def comparable_fields(entry: BibEntry) -> dict[str, str]:
    return {
        key: normalize_field_value(value)
        for key, value in entry.fields.items()
        if key not in IGNORED_COMPARISON_FIELDS
    }


def parsed_ads_entry(entry: BibEntry, ads_bibtex: str) -> BibEntry | None:
    exported = parse_bibtex_text(replace_bibtex_key(ads_bibtex, entry.key))
    if len(exported) != 1:
        return None
    return exported[0]


def normalized_identity_value(value: str) -> str:
    value = value.replace("{", "").replace("}", "")
    value = value.replace("\\&", "&")
    return " ".join(value.casefold().split())


def identity_conflicts(entry: BibEntry, ads_entry: BibEntry) -> list[str]:
    conflicts: list[str] = []
    for field in ("title", "doi", "eprint"):
        local = entry.fields.get(field)
        ads = ads_entry.fields.get(field)
        if local and ads and normalized_identity_value(local) != normalized_identity_value(ads):
            conflicts.append(field)
    return conflicts


def identifier_consensus(
    groups: list[tuple[str, list[tuple[str, str]]]],
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    resolved: list[tuple[str, str, AdsResult]] = []
    missing: list[tuple[str, AdsResult]] = []
    all_matches: list[dict[str, object]] = []

    for label, queries in groups:
        result = run_queries(queries, token, rows, timeout, sleep)
        if result.status in {"ERROR", "AMBIGUOUS"}:
            return result
        if result.status == "MISSING":
            missing.append((label, result))
            continue
        if result.status == "OK":
            bibcode = str(result.matches[0].get("bibcode", ""))
            resolved.append((label, bibcode, result))
            all_matches.extend(result.matches)

    unique_bibcodes = {bibcode for _, bibcode, _ in resolved if bibcode}
    if len(unique_bibcodes) > 1:
        details = "; ".join(f"{label}={bibcode}" for label, bibcode, _ in resolved)
        query = resolved[0][2].query if resolved else ""
        return AdsResult(
            "IDENTIFIER_CONFLICT",
            query,
            all_matches,
            f"ADS identifiers do not agree: {details}; manual review required",
        )

    if resolved and missing:
        details = "; ".join(label for label, _ in missing)
        query = missing[0][1].query
        return AdsResult(
            "IDENTIFIER_MISMATCH",
            query,
            all_matches,
            f"some identifiers resolve to {resolved[0][1]}, but {details} lookup failed; manual review required",
        )

    if resolved:
        return resolved[0][2]

    if missing:
        messages = ", ".join(f"{label}:{result.message}" for label, result in missing)
        return AdsResult("MISSING", missing[-1][1].query, [], messages)

    return AdsResult("NO_IDENTIFIER", "", [], "no bibcode, DOI, or eprint")


def bibtex_matches_ads(entry: BibEntry, ads_entry: BibEntry) -> bool:
    if entry.kind.lower() != ads_entry.kind.lower():
        return False
    return comparable_fields(entry) == comparable_fields(ads_entry)


def verify_ads_bibtex(entry: BibEntry, bibcode: str, result: AdsResult, token: str, timeout: float) -> AdsResult:
    try:
        ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        return AdsResult("ERROR", result.query, result.matches, f"could not fetch ADS BibTeX for {bibcode}: {exc}")

    ads_entry = parsed_ads_entry(entry, ads_bibtex)
    if ads_entry is None:
        return AdsResult("ERROR", result.query, result.matches, f"could not parse ADS BibTeX for {bibcode}")

    conflicts = identity_conflicts(entry, ads_entry)
    if conflicts:
        return AdsResult(
            "ADS_RECORD_CONFLICT",
            result.query,
            result.matches,
            f"local {'/'.join(conflicts)} differs from ADS export for {bibcode}; manual review required",
            ads_bibtex=ads_bibtex,
        )

    if bibtex_matches_ads(entry, ads_entry):
        return AdsResult("OK", result.query, result.matches, ads_bibtex=ads_bibtex)
    return AdsResult(
        "ADS_BIBTEX_MISMATCH",
        result.query,
        result.matches,
        f"local BibTeX differs from ADS export for {bibcode}",
        ads_bibtex=ads_bibtex,
    )


def rate_limited_result(entry: BibEntry, wait: float) -> AdsResult:
    if local_bibcode := ads_bibcode(entry):
        return AdsResult(
            "ADS_UNVERIFIED_RATE_LIMITED",
            f'bibcode:"{escape_query_value(local_bibcode)}"',
            [],
            (
                f"entry has local ADS bibcode {local_bibcode}, but ADS requested a "
                f"{format_duration(wait)} cooldown before it could be freshly verified; replacement is disabled"
            ),
        )
    return AdsResult(
        "RATE_LIMITED",
        "",
        [],
        (
            f"ADS requested a {format_duration(wait)} cooldown before this entry could be resolved, "
            "and the entry has no local ADS bibcode/adsurl"
        ),
    )


def check_entry_live(entry: BibEntry, token: str, rows: int, timeout: float, sleep: float) -> AdsResult:
    local_bibcode = ads_bibcode(entry)
    consensus_groups = identifier_query_groups(entry, include_bibcode=bool(local_bibcode))
    fallback_queries = candidate_queries(entry, include_bibcode=False)
    if not consensus_groups and not fallback_queries:
        return AdsResult("NO_IDENTIFIER", "", [], "no bibcode, adsurl, DOI, eprint, or title+year")

    if not local_bibcode:
        result = identifier_consensus(consensus_groups, token, rows, timeout, sleep) if consensus_groups else run_queries(fallback_queries, token, rows, timeout, sleep)
        if result.status == "OK":
            bibcode = str(result.matches[0].get("bibcode", ""))
            try:
                ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
            except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                return AdsResult("ERROR", result.query, result.matches, f"could not fetch ADS BibTeX for {bibcode}: {exc}")
            return AdsResult(
                "NON_ADS_BIBTEX",
                result.query,
                result.matches,
                f"entry resolves to ADS bibcode {bibcode}, but has no local adsurl/bibcode",
                ads_bibtex=ads_bibtex,
            )
        return result

    consensus = identifier_consensus(consensus_groups, token, rows, timeout, sleep)
    if consensus.status == "OK":
        return verify_ads_bibtex(entry, local_bibcode, consensus, token, timeout)
    return consensus


def check_entry(
    entry: BibEntry,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    try:
        return check_entry_live(entry, token, rows, timeout, sleep)
    except AdsRateLimitError as exc:
        return rate_limited_result(entry, exc.wait)


def format_match(match: dict[str, object]) -> str:
    bibcode = str(match.get("bibcode", ""))
    year = str(match.get("year", ""))
    title_value = match.get("title") or [""]
    title = title_value[0] if isinstance(title_value, list) and title_value else str(title_value)
    return f"{bibcode} {year} {title}".strip()


def print_detail(label: str, value: str, indent: str = "    ", label_width: int = 11) -> None:
    prefix = f"{indent}{label:<{label_width}}: "
    continuation = " " * len(prefix)
    lines = textwrap.wrap(
        value,
        width=100,
        initial_indent=prefix,
        subsequent_indent=continuation,
        break_long_words=False,
        break_on_hyphens=False,
    )
    print("\n".join(lines) if lines else prefix)


def ordered_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    ordered = [(status, counts[status]) for status in STATUS_ORDER if counts.get(status)]
    extras = sorted((status, count) for status, count in counts.items() if status not in STATUS_ORDER)
    return ordered + extras


def format_duration(seconds: float) -> str:
    total = max(int(math.ceil(seconds)), 0)
    days, remainder = divmod(total, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def mismatch_bibcodes(message: str) -> tuple[str | None, str | None]:
    match = re.search(r"local bibcode ([^;]+); DOI/arXiv/title resolves to (\S+)", message)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def print_issue_overview(result: AdsResult) -> None:
    if description := ISSUE_DESCRIPTIONS.get(result.status):
        print_detail("Issue", description)


def print_issue_action(result: AdsResult) -> None:
    if action := ISSUE_ACTIONS.get(result.status):
        print_detail("Action", action)


def match_bibcode(match: dict[str, object]) -> str | None:
    bibcode = str(match.get("bibcode", ""))
    return bibcode or None


def replacement_bibcode(result: AdsResult) -> str | None:
    if bibcode := latest_bibcode_for_result(result):
        return bibcode
    if result.status == "ADS_RECORD_CONFLICT" and result.matches:
        return match_bibcode(result.matches[0])
    return None


def print_replacement_suggestion(entry: BibEntry, result: AdsResult) -> None:
    if bibcode := ads_replacement_bibcode(result):
        print_detail(
            "Suggestion",
            f"review the ADS export for {bibcode}; run with --replace to choose ADS, paste manual, or skip while keeping key {entry.key}",
        )
        return

    if result.status == "ADS_RECORD_CONFLICT":
        if bibcode := replacement_bibcode(result):
            print_detail(
                "Suggestion",
                f"ADS export for {bibcode} is available, but compare it manually before replacing because title, DOI, or eprint conflicts",
            )
        return

    if result.status in {"IDENTIFIER_MISMATCH", "BIBCODE_MISMATCH"} and result.matches:
        if match_bibcode(result.matches[0]):
            print_detail("Suggestion", f"candidate ADS record: {format_match(result.matches[0])}; review before replacing")
        return

    if result.status in {"IDENTIFIER_CONFLICT", "AMBIGUOUS"} and result.matches:
        print_detail("Suggestion", "choose the intended ADS record from the candidates below, then fix the local identifiers")


def print_result(entry: BibEntry, result: AdsResult) -> None:
    print(f"  {entry.key} (line {entry.line})")
    print_detail("Status", result.status)
    print_issue_overview(result)

    if result.status == "BIBCODE_MISMATCH":
        local_bibcode, identifier_bibcode = mismatch_bibcodes(result.message)
        if local_bibcode:
            print_detail("Local code", local_bibcode)
        if identifier_bibcode:
            print_detail("Lookup code", identifier_bibcode)
        if result.matches:
            print_detail("Lookup rec", format_match(result.matches[0]))
        if len(result.matches) > 1:
            print_detail("Local rec", format_match(result.matches[1]))
        if result.query:
            print_detail("Query", result.query)
        print_replacement_suggestion(entry, result)
        print_issue_action(result)
        return

    if result.status == "ADS_BIBTEX_MISMATCH":
        if result.message:
            print_detail("Reason", result.message)
        if result.matches:
            print_detail("ADS", format_match(result.matches[0]))
        if result.query:
            print_detail("Query", result.query)
        print_replacement_suggestion(entry, result)
        print_issue_action(result)
        return

    if result.status == "ADS_RECORD_CONFLICT":
        if result.message:
            print_detail("Reason", result.message)
        if result.matches:
            print_detail("ADS", format_match(result.matches[0]))
        if result.query:
            print_detail("Query", result.query)
        print_replacement_suggestion(entry, result)
        print_issue_action(result)
        return

    if result.status == "IDENTIFIER_CONFLICT":
        if result.message:
            print_detail("Reason", result.message)
        print_replacement_suggestion(entry, result)
        for index, match in enumerate(result.matches[:5], start=1):
            print_detail(f"Record {index}", format_match(match))
        if result.query:
            print_detail("Query", result.query)
        print_issue_action(result)
        return

    if result.status == "NON_ADS_BIBTEX":
        if result.message:
            print_detail("Reason", result.message)
        if result.matches:
            print_detail("ADS", format_match(result.matches[0]))
        if result.query:
            print_detail("Query", result.query)
        print_replacement_suggestion(entry, result)
        print_issue_action(result)
        return

    if result.status in {"ADS_UNVERIFIED_RATE_LIMITED", "RATE_LIMITED"}:
        if result.message:
            print_detail("Reason", result.message)
        if result.query:
            print_detail("Local ADS", result.query)
        print_issue_action(result)
        return

    if result.message:
        label = "Attempts" if result.status == "MISSING" else "Reason"
        print_detail(label, result.message)
    if result.query and result.query != "local":
        label = "Last query" if result.status == "MISSING" else "Query"
        print_detail(label, result.query)

    if result.status == "AMBIGUOUS":
        print_replacement_suggestion(entry, result)
        for index, match in enumerate(result.matches[:5], start=1):
            print_detail(f"Candidate {index}", format_match(match))
    elif result.matches:
        print_detail("ADS", format_match(result.matches[0]))
        print_replacement_suggestion(entry, result)
    print_issue_action(result)


def replace_bibtex_key(bibtex: str, key: str) -> str:
    return ENTRY_RE.sub(lambda match: f"@{match.group('kind')}{{{key},", bibtex, count=1)


def latest_bibcode_for_result(result: AdsResult) -> str | None:
    if result.status in {"ADS_BIBTEX_MISMATCH", "NON_ADS_BIBTEX"} and result.matches:
        return str(result.matches[0].get("bibcode", "")) or None
    return None


def ads_replacement_bibcode(result: AdsResult) -> str | None:
    if result.status not in ADS_REPLACEMENT_STATUSES:
        return None
    if bibcode := latest_bibcode_for_result(result):
        return bibcode
    bibcodes = {match_bibcode(match) for match in result.matches}
    bibcodes.discard(None)
    if len(bibcodes) == 1:
        return next(iter(bibcodes))
    return None


def prompt_replacement_choice(entry_key: str, has_ads_replacement: bool) -> str:
    while True:
        print(f"\nReplacement choice for {entry_key}:")
        if has_ads_replacement:
            print("  1. Use ADS replacement [default]")
            print("  2. Paste manual replacement")
            print("  3. Skip")
            answer = input("Select 1, 2, or 3 [1]: ").strip().lower()
            if answer in {"", "1", "ads", "replace", "r", "y", "yes"}:
                return "ads"
            if answer in {"2", "manual", "m", "paste", "p"}:
                return "manual"
            if answer in {"3", "skip", "s", "n", "no"}:
                return "skip"
            print("Please enter 1 for ADS, 2 for manual, or 3 to skip.")
        else:
            print("  1. Paste manual replacement [default]")
            print("  2. Skip")
            answer = input("Select 1 or 2 [1]: ").strip().lower()
            if answer in {"", "1", "manual", "m", "paste", "p", "replace", "r", "y", "yes"}:
                return "manual"
            if answer in {"2", "skip", "s", "n", "no"}:
                return "skip"
            print("Please enter 1 for manual replacement or 2 to skip.")


def prompt_manual_replacement_choice(entry_key: str) -> bool:
    while True:
        print(f"\nUse pasted replacement for {entry_key}?")
        print("  1. Replace [default]")
        print("  2. Skip")
        answer = input("Select 1 or 2 [1]: ").strip().lower()
        if answer in {"", "1", "replace", "r", "y", "yes"}:
            return True
        if answer in {"2", "skip", "s", "n", "no"}:
            return False
        print("Please enter 1 to replace or 2 to skip.")


def prompt_replacement_session() -> bool:
    while True:
        answer = input("\nProceed with replacement? [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please enter y to proceed or n to stop.")


def prompt_manual_bibtex(entry_key: str) -> str | None:
    print(f"\nPaste replacement BibTeX for {entry_key}.")
    print("  Press Enter on a blank line after the pasted entry to submit it.")
    print("  You can also end the pasted entry with a line containing only '.'.")
    print("  Type 'skip' or press Enter on the first line to skip this entry.")

    first_line = input("BibTeX> ")
    if not first_line.strip():
        return None
    if first_line.strip().lower() in {"s", "skip"}:
        return None

    lines = [first_line]
    while True:
        line = input("... ")
        if line.strip() == ".":
            break
        if not line.strip():
            text = "\n".join(lines).strip()
            try:
                if len(parse_bibtex_text(text)) == 1:
                    return text
            except ValueError as exc:
                print(f"Pasted BibTeX is not complete yet: {exc}")
                continue
            print("Expected exactly one complete BibTeX entry before the blank line.")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def print_bibtex_block(title: str, bibtex: str) -> None:
    print(f"\n    {title}")
    print(f"    {'-' * len(title)}")
    for line in bibtex.strip().splitlines():
        print(f"    {line}")


def print_replacement_separator(label: str) -> None:
    width = 100
    text = f" {label} "
    left = max((width - len(text)) // 2, 0)
    right = max(width - len(text) - left, 0)
    print("\n" + "=" * left + text + "=" * right)


def apply_replacements(text: str, replacements: list[tuple[BibEntry, str]]) -> str:
    updated = text
    for entry, replacement in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        updated = updated[: entry.start] + replacement.strip() + updated[entry.end :]
    return updated


def backup_path_for(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak{index}")
        if not candidate.exists():
            return candidate
        index += 1


def ensure_backup(path: Path, existing_backup: Path | None) -> Path:
    if existing_backup is not None:
        return existing_backup
    backup_path = backup_path_for(path)
    shutil.copy2(path, backup_path)
    print(f"Backup written to {backup_path}.")
    return backup_path


def write_text_atomically(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(text)
    try:
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def replace_outdated_entries(
    bibfile: Path,
    results: list[tuple[BibEntry, AdsResult]],
    token: str,
    timeout: float,
) -> int:
    candidates: list[tuple[BibEntry, AdsResult, str | None]] = []
    for entry, result in results:
        bibcode = ads_replacement_bibcode(result)
        if bibcode is not None or result.status in MANUAL_REPLACEMENT_STATUSES:
            candidates.append((entry, result, bibcode))

    if not candidates:
        print("\nReplacement")
        print("  No automatic or manual replacement candidates were found.")
        return 0

    counts: dict[str, int] = {}
    for _, result, _ in candidates:
        counts[result.status] = counts.get(result.status, 0) + 1

    ads_count = sum(1 for _, _, bibcode in candidates if bibcode is not None)
    manual_only_count = len(candidates) - ads_count

    print("\nReplacement")
    print(f"  {ads_count} ADS replacement candidate(s) found.")
    print(f"  {manual_only_count} manual-only replacement candidate(s) found.")
    for status, count in ordered_counts(counts):
        print(f"  {status:<19} {count}")

    print("\n  Candidates")
    for entry, result, bibcode in candidates:
        detail = format_match(result.matches[0]) if result.matches else result.query or result.message
        mode = "ADS or manual" if bibcode is not None else "manual"
        print(f"  - {entry.key} line {entry.line}: {result.status} ({mode}) -> {detail}")

    print("\n  Each accepted replacement keeps the existing BibTeX key and writes a backup before editing.")

    if not prompt_replacement_session():
        print("\nReplacement skipped; file unchanged.")
        return 0

    original_text = bibfile.read_text(encoding="utf-8")
    backup_path: Path | None = None
    replacement_count = 0

    total_candidates = len(candidates)
    for index, (entry, result, bibcode) in enumerate(candidates, start=1):
        remaining_after = total_candidates - index
        current_entries = {current.key: current for current in parse_bibtex_text(original_text)}
        current_entry = current_entries.get(entry.key)
        if current_entry is None:
            print(f"\nSkipping {entry.key}: entry no longer exists in {bibfile}.")
            continue

        current = original_text[current_entry.start : current_entry.end]
        print_replacement_separator(
            f"{current_entry.key} | line {current_entry.line} | {index}/{total_candidates}, {remaining_after} remaining"
        )
        print_detail("Progress", f"{index}/{total_candidates}; {remaining_after} remaining after this item", indent="  ")
        print_detail("Status", result.status, indent="  ")
        if description := ISSUE_DESCRIPTIONS.get(result.status):
            print_detail("Issue", description, indent="  ")
        if result.message:
            label = "Attempts" if result.status == "MISSING" else "Reason"
            print_detail(label, result.message, indent="  ")
        if result.query:
            label = "Last query" if result.status == "MISSING" else "Query"
            print_detail(label, result.query, indent="  ")
        for candidate_index, match in enumerate(result.matches[:5], start=1):
            print_detail(f"Candidate {candidate_index}", format_match(match), indent="  ")
        print_bibtex_block("Current BibTeX", current)

        ads_replacement: str | None = None
        if bibcode is not None:
            ads_bibtex = result.ads_bibtex
            if not ads_bibtex:
                try:
                    ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
                except AdsRateLimitError as exc:
                    print(f"\nADS replacement unavailable for {current_entry.key}: ADS API rate limit is active: {exc}")
                except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                    print(f"\nADS replacement unavailable for {current_entry.key}: could not fetch ADS BibTeX for {bibcode}: {exc}")
            if ads_bibtex:
                ads_replacement = replace_bibtex_key(ads_bibtex, current_entry.key)
                print_detail("ADS bibcode", bibcode, indent="  ")
                print_bibtex_block("ADS Replacement", ads_replacement)
        print("\n" + "-" * 100)

        choice = prompt_replacement_choice(current_entry.key, ads_replacement is not None)
        if choice == "skip":
            print(f"Skipped {current_entry.key}.")
            continue

        if choice == "ads" and ads_replacement is not None:
            backup_path = ensure_backup(bibfile, backup_path)
            original_text = apply_replacements(original_text, [(current_entry, ads_replacement)])
            write_text_atomically(bibfile, original_text)
            replacement_count += 1
            print(f"Updated {current_entry.key} in {bibfile}.")
            continue

        while True:
            pasted = prompt_manual_bibtex(current_entry.key)
            if pasted is None:
                print(f"Skipped {current_entry.key}.")
                break

            try:
                pasted_entries = parse_bibtex_text(pasted)
            except ValueError as exc:
                print(f"Could not parse pasted BibTeX: {exc}")
                continue
            if len(pasted_entries) != 1:
                print(f"Expected exactly one BibTeX entry, got {len(pasted_entries)}.")
                continue

            replacement = replace_bibtex_key(pasted, current_entry.key)
            print_bibtex_block("Pasted Replacement", replacement)
            print("\n" + "-" * 100)
            if not prompt_manual_replacement_choice(current_entry.key):
                print(f"Skipped {current_entry.key}.")
                break

            backup_path = ensure_backup(bibfile, backup_path)
            original_text = apply_replacements(original_text, [(current_entry, replacement)])
            write_text_atomically(bibfile, original_text)
            replacement_count += 1
            print(f"Updated {current_entry.key} in {bibfile}.")
            break

    if replacement_count == 0:
        print("\nNo replacements accepted; file unchanged.")
        return 0

    print(f"\nApplied {replacement_count} replacement(s) to {bibfile}.")
    return replacement_count


def print_report(results: list[tuple[BibEntry, AdsResult]], verbose: bool) -> int:
    counts: dict[str, int] = {}
    for _, result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    total = len(results)
    print("\nSummary")
    print(f"  {'entries':<13} {total}")
    for status, count in ordered_counts(counts):
        print(f"  {status:<13} {count}")

    issue_results = [(entry, result) for entry, result in results if result.status in ISSUE_STATUSES]
    if issue_results:
        print("\nIssues")
        for index, (entry, result) in enumerate(issue_results):
            if index:
                print()
            print_result(entry, result)
    else:
        print("\nIssues")
        print("  None")

    if verbose:
        ok_results = [(entry, result) for entry, result in results if result.status not in ISSUE_STATUSES]
        if ok_results:
            print("\nResolved")
            for entry, result in ok_results:
                print_result(entry, result)

    failing = ISSUE_STATUSES
    return 1 if any(result.status in failing for _, result in results) else 0


def progress_iter(
    items: Iterable[T],
    total: int,
    enabled: bool,
    description: str,
) -> Iterator[T]:
    if not enabled:
        yield from items
        return
    if tqdm is None:
        print("tqdm is not installed; continuing without a progress bar.", file=sys.stderr)
        yield from items
        return
    yield from tqdm(items, total=total, desc=description, unit="entry")


def check_entries_parallel(
    entries: list[BibEntry],
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
    jobs: int,
    progress: bool,
) -> list[tuple[BibEntry, AdsResult]]:
    if jobs == 1:
        results: list[tuple[BibEntry, AdsResult]] = []
        for entry in progress_iter(entries, len(entries), progress, "Checking ADS"):
            results.append((entry, check_entry(entry, token, rows=rows, timeout=timeout, sleep=sleep)))
        return results

    results: list[tuple[BibEntry, AdsResult] | None] = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures: dict[Future[AdsResult], tuple[int, BibEntry]] = {
            executor.submit(check_entry, entry, token, rows, timeout, sleep): (index, entry)
            for index, entry in enumerate(entries)
        }
        completed = progress_iter(
            as_completed(futures),
            len(futures),
            progress,
            f"Checking ADS ({jobs} workers)",
        )
        for future in completed:
            index, entry = futures[future]
            try:
                result = future.result()
            except AdsRateLimitError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                result = AdsResult("ERROR", "", [], f"worker failed: {exc}")
            results[index] = (entry, result)

    return [result for result in results if result is not None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check that BibTeX entries resolve on NASA ADS.",
    )
    parser.add_argument("bibfile", type=Path, help="Path to a .bib file.")
    parser.add_argument(
        "--token",
        default=os.environ.get("ADS_API_TOKEN"),
        help="ADS API token. Defaults to ADS_API_TOKEN.",
    )
    parser.add_argument("--rows", type=int, default=5, help="Maximum ADS rows per query.")
    parser.add_argument("--timeout", type=float, default=20.0, help="ADS request timeout in seconds.")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="Delay before each uncached ADS search per worker.")
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"ADS response cache path. Defaults to {DEFAULT_CACHE_PATH}.",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=DEFAULT_CACHE_TTL,
        help=f"ADS cache expiry in seconds. Defaults to {DEFAULT_CACHE_TTL} ({format_duration(DEFAULT_CACHE_TTL)}).",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable the persistent local ADS response cache.")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore existing cached responses, but store fresh successful ADS responses.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Maximum parallel ADS checks. Defaults to {DEFAULT_JOBS}.",
    )
    parser.add_argument("--no-progress", action="store_true", help="Disable the tqdm progress bar.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Interactively replace ADS_BIBTEX_MISMATCH/NON_ADS_BIBTEX entries.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every entry, not only problems.")
    return parser


def main(argv: list[str] | None = None) -> int:
    global ADS_CACHE, ADS_RATE_LIMIT_EXPIRY

    args = build_parser().parse_args(argv)
    try:
        entries = parse_bibtex(args.bibfile)
    except OSError as exc:
        print(f"Could not read {args.bibfile}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Could not parse {args.bibfile}: {exc}", file=sys.stderr)
        return 2
    if not entries:
        print(f"No BibTeX entries found in {args.bibfile}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("--jobs must be at least 1.", file=sys.stderr)
        return 2
    if args.cache_ttl < 0:
        print("--cache-ttl must be at least 0.", file=sys.stderr)
        return 2

    if not args.token:
        print("ADS_API_TOKEN is required.", file=sys.stderr)
        return 2

    ADS_CACHE = AdsCache(
        args.cache.expanduser(),
        ttl=args.cache_ttl,
        enabled=not args.no_cache,
        refresh=args.refresh_cache,
    )
    ADS_RATE_LIMIT_EXPIRY = None
    reset_ads_run_cache()

    try:
        results = check_entries_parallel(
            entries,
            args.token,
            rows=args.rows,
            timeout=args.timeout,
            sleep=args.sleep,
            jobs=args.jobs,
            progress=not args.no_progress,
        )
    except AdsRateLimitError as exc:
        print(f"\nADS API rate limit is active: {exc}", file=sys.stderr)
        if args.no_cache:
            print("The local ADS cache is disabled, so no cached fallback was available.", file=sys.stderr)
        elif args.refresh_cache:
            print("Existing cached ADS responses were ignored because --refresh-cache was used.", file=sys.stderr)
        else:
            print(
                f"Cached ADS responses younger than {format_duration(args.cache_ttl)} were used first; "
                "the rate-limited request was not available in cache.",
                file=sys.stderr,
            )
        print("No replacements were attempted.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; no further checks or replacements were attempted.", file=sys.stderr)
        return 130
    exit_code = print_report(results, args.verbose)
    if args.replace and any(result.status == "ERROR" for _, result in results):
        print("\nReplacement skipped because ADS errors occurred during checking.")
        print("Rerun later, or use a gentler command such as: --jobs 1 --sleep 3")
    elif args.replace:
        replace_outdated_entries(args.bibfile, results, args.token, args.timeout)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
