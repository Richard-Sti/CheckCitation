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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from threading import Event, Lock
from typing import TypeVar

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


# ponytail: raw ANSI, no dependency; honour NO_COLOR and non-tty output.
PROMPT_COLOR = "\033[1;36m"  # bold cyan: interactive questions
UNAVAILABLE_COLOR = "\033[1;91m"  # bold bright red: no replacement available
_RESET = "\033[0m"


def colorize(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


ADS_API = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_BIBTEX_API = "https://api.adsabs.harvard.edu/v1/export/bibtex"
# ADS's own citation matcher: it takes a reference string built from author,
# year, journal, volume and page - the coordinates a Solr query never uses.
ADS_REFERENCE_API = "https://api.adsabs.harvard.edu/v1/reference/text"
ADS_UI = "https://ui.adsabs.harvard.edu"
DEFAULT_JOBS = 1
DEFAULT_SLEEP = 0.1
DEFAULT_RETRIES = 4
DEFAULT_RETRY_WAIT = 10.0
MAX_RETRY_WAIT = 60.0
DEFAULT_CACHE_PATH = Path(__file__).with_name(".ads_cache.json")
DEFAULT_CACHE_TTL = 30 * 24 * 60 * 60
# A resolved ADS record does not change, but a miss stops being a miss the
# moment ADS indexes the paper. Same cache, two expiries.
MISS_TTL = 60 * 60
# One export request carries many bibcodes; ADS allows far more than this.
EXPORT_BATCH = 100
BIBCODE_RE = re.compile(r"/abs/([^/?#]+)")
BLOCK_RE = re.compile(r"@(?P<kind>[A-Za-z]+)\s*{", re.M)
ENTRY_RE = re.compile(r"@(?P<kind>[A-Za-z]+)\s*{\s*(?P<key>[^,\s]+)\s*,", re.M)
FIELD_RE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=", re.M)
SKIP_DIRECTIVE_RE = re.compile(r"^\s*%+\s*checkcitation:\s*skip\b", re.I)
LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+\s*")
# `\i` and `\ss` are letters, not commands. Stripping them alongside \ensuremath
# turns Antol{\'\i}nez into "antolnez", which then agrees with no citation key and
# quietly lowers every title score it appears in. The lookahead keeps \lambda and
# \odot out of it.
LATEX_LETTER_RE = re.compile(r"\\(AE|OE|ae|oe|ss|aa|AA|i|j|l|L|o|O)(?![A-Za-z])")
LATEX_LETTERS = {"aa": "a", "AA": "A"}  # \aa is a-ring; the rest already spell themselves
NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
AUTHOR_SPLIT_RE = re.compile(r"\s+and\s+", re.I)
# "1105--1134" cites page 1105; "A6" is already the whole locator.
FIRST_PAGE_RE = re.compile(r"^[A-Za-z]?\d+")
# `0.8 2020A&A...641A...6P -- Planck Collaboration 2020, A&A, 641, A6`
RESOLVED_RE = re.compile(r"^(?P<score>[\d.]+)\s+(?P<bibcode>\S+)\s+--\s")
# Surname plus a four-digit year, optionally separated and disambiguated:
# Dutton2007a, Riess_2022, Stiskalek_2026B. The separator is not cosmetic - a
# whole bibliography written `Surname_Year` otherwise reads as zero keys, and
# switches off the only check independent of the entry's own fields.
CITATION_KEY_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z'\-]*)[_.\-]?(?P<year>\d{4})[A-Za-z]?$")
CITE_RE = re.compile(r"\\[A-Za-z]*cite[A-Za-z]*\*?\s*(?:\[[^\]]*\]\s*)*\{(?P<keys>[^}]*)\}")
TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*")
# ponytail: 0.85 on an alphanumeric reduction; tighten only if real mismatches slip through.
TITLE_SIMILARITY_THRESHOLD = 0.85
# A strict roman numeral, so "civil" and "mid" are not read as series numbers.
ROMAN_RE = re.compile(r"^m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")
WORD_RE = re.compile(r"[0-9a-z]+")
# `@comment{@ARTICLE{Old2019, ...}}` is how an entry is disabled; the inner
# entry must stay disabled, and @string/@preamble are not entries at all.
NON_ENTRY_KINDS = {"comment", "string", "preamble"}
# Only these statuses resolved to a real ADS record, so only these can make a
# duplicate. For AMBIGUOUS or ERROR, matches[0] is just the top hit.
RESOLVED_STATUSES = {
    "OK",
    "PREPRINT_PUBLISHED",
    "ADS_BIBTEX_MISMATCH",
    "NON_ADS_BIBTEX",
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_MISMATCH",
}
DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", re.I)
ARXIV_PREFIX_RE = re.compile(r"^arxiv:", re.I)
ARXIV_VERSION_RE = re.compile(r"v\d+$")
STATUS_ORDER = (
    "OK",
    "PREPRINT_PUBLISHED",
    "ADS_BIBTEX_MISMATCH",
    "NON_ADS_BIBTEX",
    "ADS_RECORD_CONFLICT",
    "CITATION_KEY_CONFLICT",
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
    "PREPRINT_PUBLISHED",
    "ADS_BIBTEX_MISMATCH",
    "NON_ADS_BIBTEX",
    "ADS_RECORD_CONFLICT",
    "CITATION_KEY_CONFLICT",
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
    "PREPRINT_PUBLISHED": "the entry cites an arXiv preprint, and ADS holds the published paper under a separate record",
    "ADS_BIBTEX_MISMATCH": "the local entry has ADS provenance, but its BibTeX fields differ from the current ADS export",
    "NON_ADS_BIBTEX": "the entry resolves to ADS, but it has no local ADS bibcode or adsurl",
    "ADS_RECORD_CONFLICT": "the resolved ADS record disagrees with the local entry on title, author, DOI, eprint, or citation key",
    "CITATION_KEY_CONFLICT": "the entry itself matches the ADS record, but its citation key names a different first author or year",
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
    "PREPRINT_PUBLISHED": "use --replace to review the published record and cite it instead, or keep the preprint on purpose",
    "ADS_BIBTEX_MISMATCH": "use --replace to review the ADS-exported replacement, or edit the local entry manually",
    "NON_ADS_BIBTEX": "use --replace to review adding the ADS-exported entry while keeping the citation key",
    "ADS_RECORD_CONFLICT": "use --replace to review the ADS-exported replacement, paste a manual replacement, or skip",
    "CITATION_KEY_CONFLICT": "rename the citation key and every cite to it, or paste the record the key actually names; replacing the entry body would change nothing",
    "IDENTIFIER_CONFLICT": "use --replace to paste a reviewed replacement after deciding which identifier is intended",
    "BIBCODE_MISMATCH": "use --replace to paste a reviewed replacement after deciding whether the local ADS record or identifier-resolved record is intended",
    "IDENTIFIER_MISMATCH": "use --replace to review the ADS-exported replacement, paste a manual replacement, or skip",
    "ADS_UNVERIFIED_RATE_LIMITED": "rerun after the ADS cooldown expires; automatic replacement is disabled",
    "RATE_LIMITED": "rerun after the ADS cooldown expires, preferably with fewer workers or a longer --sleep",
    "MISSING": "no ADS route matched; open the search link, find the record by hand, and paste it",
    "AMBIGUOUS": "open the search link to pick the intended record, or add a DOI, arXiv ID, or ADS bibcode/adsurl",
    "NO_IDENTIFIER": "open the search link, or add a DOI, arXiv ID, ADS bibcode/adsurl, or title plus year",
    "ERROR": "rerun later; if this repeats, inspect the reported request error",
}
AUTOMATIC_REPLACEMENT_STATUSES = {"ADS_BIBTEX_MISMATCH", "NON_ADS_BIBTEX"}
# Where a preprint is worth a second look: the record it names is not in dispute,
# so a published counterpart is news rather than one more thing already wrong.
PREPRINT_UPGRADE_STATUSES = AUTOMATIC_REPLACEMENT_STATUSES | {"OK"}
ADS_REPLACEMENT_STATUSES = AUTOMATIC_REPLACEMENT_STATUSES | {
    "PREPRINT_PUBLISHED",
    "ADS_RECORD_CONFLICT",
    "IDENTIFIER_MISMATCH",
}
# Nothing resolved, so the tool has done all it can and the user takes over.
HANDOFF_STATUSES = {"MISSING", "AMBIGUOUS", "NO_IDENTIFIER"}
MANUAL_REPLACEMENT_STATUSES = {
    "PREPRINT_PUBLISHED",
    "ADS_RECORD_CONFLICT",
    "CITATION_KEY_CONFLICT",
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
    skip: bool = False


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


def is_empty_result(value: object) -> bool:
    """True for a cached "nothing found": no documents, no export, no bibcode."""
    return value is None or value == [] or value == ""


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
            # Every namespace, not a hard-coded two: `reference` was written on
            # every run and dropped on every load, so the resolver was re-queried
            # for each MISSING entry for ever.
            for name, value in raw.items():
                if name != "version" and isinstance(value, dict):
                    self.data[name] = value

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
            value = item.get("value")
            ttl = min(self.ttl, MISS_TTL) if is_empty_result(value) else self.ttl
            if ttl >= 0 and time.time() - float(stored_at) > ttl:
                return None
            return value

    def set(self, namespace: str, key: str, value: object) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.namespace(namespace)[key] = {"stored_at": time.time(), "value": value}
            self.write_locked()

    def set_many(self, namespace: str, items: dict[str, object]) -> None:
        """Store a batch behind one file write, not one write per record."""
        if not self.enabled or not items:
            return
        with self.lock:
            space = self.namespace(namespace)
            now = time.time()
            for key, value in items.items():
                space[key] = {"stored_at": now, "value": value}
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
    except BaseException as exc:
        with ADS_IN_FLIGHT_LOCK:
            in_flight.exception = exc
            ADS_IN_FLIGHT.pop(request_key, None)
            in_flight.event.set()
        raise

    try:
        store(value)
    except OSError as exc:
        # A cache that cannot be written is a slow run, not a failed one.
        print(f"Could not write the ADS cache: {exc}", file=sys.stderr)

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
    while len(value) >= 2:
        if value[0] == "{" and value[-1] == "}":
            value = value[1:-1].strip()
        elif value[0] == '"' and value[-1] == '"':
            value = value[1:-1].strip()
        else:
            break
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
    consumed = 0
    for block in BLOCK_RE.finditer(text):
        # Anything inside a block already consumed is not a top-level entry. That
        # is what keeps `@comment{@ARTICLE{...}}` disabled instead of parsing the
        # inner entry, sending it to ADS, and rewriting the comment in place.
        if block.start() < consumed:
            continue
        kind = block.group("kind").lower()
        match = ENTRY_RE.match(text, block.start())
        if kind not in NON_ENTRY_KINDS and match is None:
            continue
        try:
            end = find_entry_end(text, block.start())
        except ValueError as exc:
            if kind in NON_ENTRY_KINDS:
                continue
            line = text.count("\n", 0, match.start()) + 1
            raise ValueError(f"could not parse entry {match.group('key')} at line {line}: {exc}") from exc
        consumed = end
        if kind in NON_ENTRY_KINDS:
            continue
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
                skip=has_skip_directive(text, match.start()),
            )
        )
    return entries


def has_skip_directive(text: str, start: int) -> bool:
    """True when the last non-blank line before an entry is `% checkcitation: skip`."""
    preceding = text[:start].rstrip()
    if not preceding:
        return False
    return bool(SKIP_DIRECTIVE_RE.match(preceding.rsplit("\n", 1)[-1]))


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


def ads_search_url(query: str) -> str:
    """The failed query, ready to run by hand at ADS."""
    return f"{ADS_UI}/search/q={urllib.parse.quote(query, safe='')}"


def ads_abstract_url(bibcode: str) -> str:
    return f"{ADS_UI}/abs/{urllib.parse.quote(bibcode, safe='')}/abstract"


def escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def bare_doi(value: str) -> str:
    """A DOI without the `https://doi.org/` or `doi:` wrapper.

    ADS exports the bare form and indexes only that, so querying the wrapper
    resolves nothing and the entry is reported MISSING - which is how a correct
    citation ends up looking broken.
    """
    return DOI_PREFIX_RE.sub("", value.strip(), count=1)


def bare_arxiv(value: str) -> str:
    """An arXiv id without its `arXiv:` prefix, in whatever case it was written."""
    return ARXIV_VERSION_RE.sub("", ARXIV_PREFIX_RE.sub("", value.strip(), count=1))


def candidate_queries(entry: BibEntry, include_bibcode: bool = True) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if include_bibcode and (bibcode := ads_bibcode(entry)):
        queries.append(("bibcode", f'bibcode:"{escape_query_value(bibcode)}"'))
    if doi := entry.fields.get("doi"):
        doi = bare_doi(doi)
        queries.append(("doi", f'doi:"{escape_query_value(doi)}"'))
        queries.append(("doi_identifier", f'identifier:"{escape_query_value(doi)}"'))
    if eprint := entry.fields.get("eprint"):
        arxiv_id = bare_arxiv(eprint)
        queries.append(("arxiv", f'identifier:"arXiv:{escape_query_value(arxiv_id)}"'))
    title = entry.fields.get("title")
    year = entry.fields.get("year")
    if title and year:
        # Solr tokenises `\\sc` and `\\ensuremath` as words and matches nothing;
        # normalising strips them the same way the comparison does.
        clean_title = normalized_identity_value(title)
        queries.append(("title", f'title:"{escape_query_value(clean_title)}" year:{year}'))
    return queries


def local_identifiers(entry: BibEntry) -> list[tuple[str, str]]:
    """The identifiers the entry claims about itself, as (label, comparable token)."""
    found: list[tuple[str, str]] = []
    if bibcode := ads_bibcode(entry):
        found.append(("bibcode", normalized_identity_value(bibcode)))
    if doi := entry.fields.get("doi"):
        found.append(("doi", normalized_identity_value(bare_doi(doi))))
    if eprint := entry.fields.get("eprint"):
        found.append(("arxiv", normalized_identity_value(bare_arxiv(eprint))))
    return found


def combined_identifier_query(entry: BibEntry) -> str:
    """One query covering every identifier the entry carries.

    ADS's `identifier` field indexes bibcodes, DOIs and arXiv ids together, and a
    returned record lists all of its own. So one round trip answers what used to
    take one per identifier - and answers it exactly, by set membership, rather
    than by how many rows happened to come back.
    """
    terms: list[str] = []
    if bibcode := ads_bibcode(entry):
        terms.append(f'identifier:"{escape_query_value(bibcode)}"')
    if doi := entry.fields.get("doi"):
        bare = escape_query_value(bare_doi(doi))
        # Both index paths: a DOI is not always reachable through `identifier`.
        terms.append(f'doi:"{bare}"')
        terms.append(f'identifier:"{bare}"')
    if eprint := entry.fields.get("eprint"):
        terms.append(f'identifier:"arXiv:{escape_query_value(bare_arxiv(eprint))}"')
    return " OR ".join(terms)


def match_identity_tokens(match: dict[str, object]) -> set[str]:
    """Every identifier a returned ADS record answers to."""
    values = [str(match.get("bibcode", ""))]
    for field in ("identifier", "doi"):
        raw = match.get(field) or []
        values.extend(str(item) for item in (raw if isinstance(raw, list) else [raw]))
    tokens: set[str] = set()
    for value in values:
        if normalized := normalized_identity_value(value):
            tokens.update({normalized, bare_doi(normalized), bare_arxiv(normalized)})
    return tokens


def match_title(match: dict[str, object]) -> str:
    title = match.get("title") or [""]
    return title[0] if isinstance(title, list) and title else str(title)


def is_arxiv_bibcode(bibcode: str) -> bool:
    return len(bibcode) >= 9 and bibcode[4:9] == "arXiv"


def preferred_match(matches: list[dict[str, object]]) -> dict[str, object] | None:
    """Collapse an arXiv/refereed pair of the same paper to the refereed record.

    Returns None whenever the candidates are not plainly the same paper, so
    genuinely ambiguous lookups still surface as AMBIGUOUS.
    """
    if len({alphanumeric_key(match_title(match)) for match in matches}) != 1:
        return None
    refereed = [match for match in matches if not is_arxiv_bibcode(str(match.get("bibcode", "")))]
    return refereed[0] if len(refereed) == 1 else None


def ads_search_guarded(
    label: str,
    query: str,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> tuple[list[dict[str, object]], AdsResult | None]:
    """Search, or the ERROR result that explains why not."""
    try:
        return ads_search(query, token, rows=rows, timeout=timeout, sleep=sleep), None
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return [], AdsResult("ERROR", query, [], f"ADS HTTP 429 rate limit for {label} query; rerun later with --jobs 1 --sleep 3")
        return [], AdsResult("ERROR", query, [], f"ADS HTTP {exc.code} for {label} query")
    except urllib.error.URLError as exc:
        return [], AdsResult("ERROR", query, [], f"ADS request failed for {label} query: {exc.reason}")
    except TimeoutError:
        return [], AdsResult("ERROR", query, [], f"ADS request timed out for {label} query")


def run_queries(
    queries: list[tuple[str, str]],
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    messages: list[str] = []
    for label, query in queries:
        matches, failure = ads_search_guarded(label, query, token, rows, timeout, sleep)
        if failure is not None:
            return failure

        if len(matches) == 1:
            return AdsResult("OK", query, matches)
        if len(matches) > 1:
            if (preferred := preferred_match(matches)) is not None:
                return AdsResult("OK", query, [preferred])
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
        except TimeoutError:
            if attempt == DEFAULT_RETRIES:
                raise
            print(f"ADS request timed out; retrying ({attempt + 1}/{DEFAULT_RETRIES})", file=sys.stderr)
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


ADS_SEARCH_FIELDS = "bibcode,title,year,doi,identifier,pub"


def ads_search(query: str, token: str, rows: int, timeout: float, sleep: float = 0.0) -> list[dict[str, object]]:
    cache_key = json.dumps(
        {"fl": ADS_SEARCH_FIELDS, "query": query, "rows": rows},
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
                "fl": ADS_SEARCH_FIELDS,
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
    """The ADS BibTeX for one record, through the same request shape as a batch.

    Not the per-bibcode GET: that endpoint truncates the author list to ten names
    plus a literal `et al.`, which is the malformed author this tool warns about,
    and which it would then offer as a replacement. The POST form returns the full
    list, so one record and a hundred records come back identical either way.
    """
    cached = ads_cached_value("bibtex", bibcode)
    if isinstance(cached, str) and cached.strip():
        return cached

    def fetch() -> object:
        if (wait := active_ads_rate_limit()) is not None:
            raise AdsRateLimitError(wait)
        exports = ads_export_bibtex_many([bibcode], token, timeout)
        export = exports.get(bibcode, "")
        if not export.strip() and len(exports) == 1:
            # ADS keys each entry by its canonical bibcode, which is not
            # necessarily the alias that was asked for.
            export = next(iter(exports.values()))
        if not export.strip():
            raise RuntimeError(f"ADS returned no BibTeX export for {bibcode}")
        return export

    def store(value: object) -> None:
        if ADS_CACHE is not None:
            ADS_CACHE.set("bibtex", bibcode, value)

    export = fetch_ads_once("bibtex", bibcode, fetch, store)
    if not isinstance(export, str) or not export.strip():
        raise RuntimeError(f"ADS returned no BibTeX export for {bibcode}")
    return export


def first_author_name(author: str) -> str:
    """The first author as written, braces dropped: `{Riess}, Adam G.` -> `Riess, Adam G.`"""
    first = AUTHOR_SPLIT_RE.split(author.strip(), maxsplit=1)[0]
    return " ".join(first.replace("{", "").replace("}", "").split())


def reference_string(entry: BibEntry) -> str:
    """A citation string for the ADS resolver, or "" when there is too little to try.

    The resolver wants bibliographic coordinates, not a title: it refuses a
    reference "with no year and volume". Journal macros are left as written -
    `\\aap` resolves, just one confidence step below `A&A`, and every candidate is
    identity-checked afterwards anyway, so the extra points buy nothing.
    """
    author = first_author_name(entry.fields.get("author", ""))
    year = entry.fields.get("year", "").strip()
    if not author or not year:
        return ""
    journal = entry.fields.get("journal", "").strip()
    volume = entry.fields.get("volume", "").strip()
    if journal and volume:
        page = entry.fields.get("eid", "") or entry.fields.get("pages", "")
        page_match = FIRST_PAGE_RE.match(page.strip())
        parts = [journal, volume] + ([page_match.group(0)] if page_match else [])
        return f"{author} {year}, " + ", ".join(parts)
    # No coordinates: a book or a preprint. Worth one try on the title.
    if title := normalized_identity_value(entry.fields.get("title", "")):
        return f"{author} {year}, {title}"
    return ""


def ads_resolve_reference(reference: str, token: str, timeout: float) -> str | None:
    """Ask ADS to match a reference string, and return its bibcode or None.

    The reported score is deliberately ignored. A reference with one wrong page
    still comes back at 0.7 - pointing at a different author's paper - so the
    verdict has to come from the identity check, exactly as it does for a bibcode.
    """
    cached = ads_cached_value("reference", reference)
    if isinstance(cached, str):
        return cached or None

    def fetch() -> object:
        if (wait := active_ads_rate_limit()) is not None:
            raise AdsRateLimitError(wait)
        request = urllib.request.Request(
            ADS_REFERENCE_API,
            data=json.dumps({"reference": [reference]}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "check-ads-bib/0.1",
            },
        )
        body = urlopen_with_retries(request, timeout).decode("utf-8")
        line = body.strip().splitlines()[0] if body.strip() else ""
        if isinstance(parsed := _resolver_payload(body), str):
            line = parsed
        match = RESOLVED_RE.match(line)
        if not match or float(match.group("score")) <= 0:
            return ""
        bibcode = match.group("bibcode")
        # A failed match comes back as a row of dots, not a bibcode.
        return "" if set(bibcode) <= {"."} else bibcode

    def store(value: object) -> None:
        if ADS_CACHE is not None:
            ADS_CACHE.set("reference", reference, value)

    bibcode = fetch_ads_once("reference", reference, fetch, store)
    return bibcode if isinstance(bibcode, str) and bibcode else None


def _resolver_payload(body: str) -> object:
    """The endpoint answers with a bare line, or with {"resolved": line}."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        resolved = payload.get("resolved")
        if isinstance(resolved, list) and resolved:
            resolved = resolved[0]
        if isinstance(resolved, dict):
            return f"{resolved.get('score', 0)} {resolved.get('bibcode', '')} -- "
        return resolved if isinstance(resolved, str) else None
    return None


def ads_export_bibtex_many(bibcodes: list[str], token: str, timeout: float) -> dict[str, str]:
    """Export many records in one request. ADS keys each entry by its own bibcode."""
    request = urllib.request.Request(
        ADS_BIBTEX_API,
        data=json.dumps({"bibcode": list(bibcodes)}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "check-ads-bib/0.1",
        },
    )
    body = urlopen_with_retries(request, timeout).decode("utf-8")
    try:
        export = str(json.loads(body).get("export", ""))
    except json.JSONDecodeError:
        export = body
    return {entry.key: entry.raw for entry in parse_bibtex_text(export)}


def prefetch_exports(entries: list[BibEntry], token: str, timeout: float) -> None:
    """Warm the export cache in bulk, so the per-entry path makes no export call.

    Only the bibcodes the file already names can be known before resolving - but
    that is most of them, because ADS's own export writes an adsurl into every
    entry. One request per hundred replaces one per entry.
    """
    if (wait := active_ads_rate_limit()) is not None:
        return
    wanted = sorted({bibcode for entry in entries if (bibcode := ads_bibcode(entry))})
    missing = [bibcode for bibcode in wanted if ads_cached_value("bibtex", bibcode) is None]
    for index in range(0, len(missing), EXPORT_BATCH):
        batch = missing[index : index + EXPORT_BATCH]
        try:
            exports = ads_export_bibtex_many(batch, token, timeout)
        except Exception as exc:  # noqa: BLE001 - any failure just means the old path
            print(f"Bulk ADS export failed ({exc}); falling back to one request per entry.", file=sys.stderr)
            return
        if ADS_CACHE is not None:
            try:
                ADS_CACHE.set_many("bibtex", exports)
            except OSError as exc:
                print(f"Could not write the ADS cache: {exc}", file=sys.stderr)
        with ADS_IN_FLIGHT_LOCK:
            for bibcode, text in exports.items():
                ADS_RUN_CACHE[("bibtex", bibcode)] = text


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
    value = LATEX_LETTER_RE.sub(lambda m: LATEX_LETTERS.get(m.group(1), m.group(1)), value)
    value = LATEX_COMMAND_RE.sub(" ", value)  # \ensuremath, \sc, \textit, \approx
    value = re.sub(r"\\(.)", r"\1", value)  # \&, \_, \%
    for char in "{}$":
        value = value.replace(char, " ")
    # Decompose, then drop the combining marks: a literal "í" is otherwise thrown
    # away whole by NON_ALNUM_RE, exactly like the LaTeX spelling of it was.
    value = "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))
    return " ".join(value.casefold().split())


def alphanumeric_key(value: str) -> str:
    return NON_ALNUM_RE.sub("", normalized_identity_value(value))


def normalized_identifier(field: str, value: str) -> str:
    """An identifier as ADS exports it, whatever wrapper the local entry used.

    `eprint = {arXiv:2001.00001}` and `doi = {https://doi.org/10.1/x}` are both
    ordinary in the wild and name the same record as the bare form.
    """
    value = normalized_identity_value(value)
    if field == "doi":
        return DOI_PREFIX_RE.sub("", value)
    # `1807.06209v2` is the same paper as `1807.06209`.
    return ARXIV_VERSION_RE.sub("", ARXIV_PREFIX_RE.sub("", value))


def series_tokens(title: str) -> list[str]:
    """The digits and roman numerals in a title, in order.

    Papers in one series differ by these and by almost nothing else, so a
    character ratio cannot separate them: `Paper I` against `Paper II` scores
    0.99, and `Planck 2015 results. XIII` against `Planck 2018 results. VI`
    scores 0.93 - both far above the threshold, both the wrong paper.
    """
    words = WORD_RE.findall(normalized_identity_value(title))
    return [word for word in words if word.isdigit() or ROMAN_RE.match(word)]


def titles_conflict(local: str, ads: str) -> bool:
    """True when two titles should not be taken for the same paper."""
    left_key, right_key = alphanumeric_key(local), alphanumeric_key(ads)
    if not left_key or not right_key:
        return False
    # The same title once markup and punctuation are gone, however each side
    # happens to break into words: `H\\,{\\sc i}` against `HI`, `3-D` against `3D`.
    if left_key == right_key:
        return False
    # Otherwise the numbering has to match, and only when both sides carry any:
    # `Paper I` against `Paper II` is the case this exists for.
    left, right = series_tokens(local), series_tokens(ads)
    if left and right and left != right:
        return True
    return title_similarity(local, ads) < TITLE_SIMILARITY_THRESHOLD


def title_similarity(local: str, ads: str) -> float:
    """Similarity of two titles after LaTeX stripping, ignoring punctuation and markup."""
    left = alphanumeric_key(local)
    right = alphanumeric_key(ads)
    if not left or not right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def first_author_surname(author: str) -> str:
    """Alphanumeric surname of the first author.

    Falls back to the whole name when there is no `Surname, Given` comma, so that
    corporate authors ("Planck Collaboration") stay matchable by substring.
    """
    first = AUTHOR_SPLIT_RE.split(author.strip(), maxsplit=1)[0]
    if "," in first:
        first = first.split(",", 1)[0]
    return alphanumeric_key(first)


def names_agree(left: str, right: str) -> bool:
    """Substring match either way, so `vanDenBosch` and `Planck` survive."""
    if not left or not right:
        return True
    return left in right or right in left


def key_year_drift(key: str, ads_entry: BibEntry) -> int:
    """Years between a `Surname2020` key and the record it resolves to, or 0.

    Keying by the journal year while the entry points at the arXiv preprint is a
    naming choice, not a wrong citation, so this is reported as a warning and
    never as a conflict. +/-1 is absorbed entirely.
    """
    match = CITATION_KEY_RE.match(key)
    ads_year = ads_entry.fields.get("year", "") if ads_entry else ""
    if not match or not ads_year.isdigit():
        return 0
    drift = int(match.group("year")) - int(ads_year)
    return drift if abs(drift) > 1 else 0


def key_conflicts(key: str, ads_entry: BibEntry) -> bool:
    """True when a `Surname2020`-style key names someone other than the first author.

    This is the only signal independent of the entry's own fields, so it is what
    catches an internally consistent entry that is simply the wrong paper. Only the
    surname: a year that disagrees is a warning, because the entry is still that
    record and renaming the key is a decision about the .tex, not about the .bib.
    """
    match = CITATION_KEY_RE.match(key)
    if not match:
        return False
    name = NON_ALNUM_RE.sub("", match.group("name").casefold())
    if not names_agree(name, first_author_surname(ads_entry.fields.get("author", ""))):
        # A survey or collaboration key names the project, not the first author:
        # `CosmoVerse2025` resolves to a paper by Di Valentino. The project name is
        # in the title, so look there before calling it the wrong paper - otherwise
        # the entry conflicts forever, and replacing it can never help, because the
        # citation key is deliberately kept.
        # ponytail: 4 characters, so a short surname like `Li` cannot be waved
        # through by a chance substring of some title.
        # Whole words only. On the concatenated title `ross` matches inside
        # `cross-correlation`, which silently switched off the one check that
        # catches an internally consistent entry naming the wrong paper.
        title_words = set(WORD_RE.findall(normalized_identity_value(ads_entry.fields.get("title", ""))))
        return len(name) < 4 or name not in title_words
    return False


def identity_conflicts(entry: BibEntry, ads_entry: BibEntry) -> list[str]:
    conflicts: list[str] = []

    local_title = entry.fields.get("title")
    ads_title = ads_entry.fields.get("title")
    if local_title and ads_title and titles_conflict(local_title, ads_title):
        conflicts.append("title")

    for field in ("doi", "eprint"):
        local = entry.fields.get(field)
        ads = ads_entry.fields.get(field)
        if local and ads and normalized_identifier(field, local) != normalized_identifier(field, ads):
            conflicts.append(field)

    local_author = entry.fields.get("author")
    ads_author = ads_entry.fields.get("author")
    if local_author and ads_author and not names_agree(first_author_surname(local_author), first_author_surname(ads_author)):
        conflicts.append("author")

    if key_conflicts(entry.key, ads_entry):
        conflicts.append("key")

    return conflicts


def cites_a_preprint(entry: BibEntry) -> bool:
    """True when the entry names an arXiv record rather than a published one."""
    if is_arxiv_bibcode(ads_bibcode(entry) or ""):
        return True
    if bare_doi(entry.fields.get("doi", "")).casefold().startswith("10.48550/arxiv"):
        return True
    return alphanumeric_key(entry.fields.get("journal", "")) == "arxiveprints"


def record_venue(match: dict[str, object]) -> str:
    """Where ADS says the record was published, spelled out rather than as `\\aap`."""
    return " ".join(str(match.get(field, "")).strip() for field in ("pub", "year")).strip()


def malformed_author(entry: BibEntry) -> bool:
    """True for a literal `{et al.}` author, which renders as `(Smith & et al. 2020)`."""
    author = entry.fields.get("author", "")
    return any(alphanumeric_key(part) == "etal" for part in AUTHOR_SPLIT_RE.split(author))


def identifier_consensus(
    entry: BibEntry,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    """Resolve every identifier the entry carries, in one ADS request.

    Each returned record is matched against each local identifier by set
    membership, so "the DOI and the bibcode name different papers" is read off
    one response rather than inferred from two.
    """
    identifiers = local_identifiers(entry)
    query = combined_identifier_query(entry)
    if not identifiers or not query:
        return AdsResult("NO_IDENTIFIER", "", [], "no bibcode, DOI, or eprint")

    matches, failure = ads_search_guarded("identifier", query, token, rows, timeout, sleep)
    if failure is not None:
        return failure

    resolved: list[tuple[str, str, dict[str, object]]] = []
    missing: list[str] = []
    for label, wanted in identifiers:
        hits = [match for match in matches if wanted in match_identity_tokens(match)]
        if not hits:
            missing.append(label)
            continue
        if len(hits) > 1:
            if (preferred := preferred_match(hits)) is None:
                return AdsResult("AMBIGUOUS", query, hits, f"the {label} matches {len(hits)} ADS records")
            hits = [preferred]
        resolved.append((label, str(hits[0].get("bibcode", "")), hits[0]))

    all_matches = [match for _, _, match in resolved]
    unique_bibcodes = {bibcode for _, bibcode, _ in resolved if bibcode}
    if len(unique_bibcodes) > 1:
        details = "; ".join(f"{label}={bibcode}" for label, bibcode, _ in resolved)
        return AdsResult(
            "IDENTIFIER_CONFLICT",
            query,
            all_matches,
            f"ADS identifiers do not agree: {details}; manual review required",
        )

    if resolved and missing:
        return AdsResult(
            "IDENTIFIER_MISMATCH",
            query,
            all_matches,
            f"some identifiers resolve to {resolved[0][1]}, but {'; '.join(missing)} lookup failed; manual review required",
        )

    if resolved:
        return AdsResult("OK", query, all_matches)

    return AdsResult("MISSING", query, [], ", ".join(f"{label}:0" for label in missing))


def bibtex_matches_ads(entry: BibEntry, ads_entry: BibEntry) -> bool:
    if entry.kind.lower() != ads_entry.kind.lower():
        return False
    return comparable_fields(entry) == comparable_fields(ads_entry)


def verify_ads_bibtex(
    entry: BibEntry,
    bibcode: str,
    result: AdsResult,
    token: str,
    timeout: float,
    local_bibcode: bool = True,
) -> AdsResult:
    """Fetch the ADS export for `bibcode` and gate it behind the identity check.

    Every path that resolves an entry to an ADS record goes through here, so an
    entry that resolves to the wrong paper cannot be offered as a replacement.
    """
    try:
        ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        return AdsResult("ERROR", result.query, result.matches, f"could not fetch ADS BibTeX for {bibcode}: {exc}")

    ads_entry = parsed_ads_entry(entry, ads_bibtex)
    if ads_entry is None:
        return AdsResult("ERROR", result.query, result.matches, f"could not parse ADS BibTeX for {bibcode}")

    conflicts = identity_conflicts(entry, ads_entry)
    if conflicts == ["key"] and bibtex_matches_ads(entry, ads_entry):
        # The entry matches the record; only its key does not. Replacement cannot
        # help - the key is kept by design - so this must not be offered as one.
        return AdsResult(
            "CITATION_KEY_CONFLICT",
            result.query,
            result.matches,
            (
                f"the entry matches ADS record {bibcode}, but the citation key {entry.key} "
                f"names a different first author or year; renaming the key is the fix"
            ),
            ads_bibtex=ads_bibtex,
        )
    if conflicts and not set(conflicts) - {"doi", "eprint"} and cites_a_preprint(entry) and not is_arxiv_bibcode(bibcode):
        # ADS merged the preprint into the journal record, so the entry's own
        # identifiers land on it and only the arXiv DOI/eprint disagree. That is
        # the same news as an unmerged pair, not a hint of a different paper.
        venue = record_venue(result.matches[0]) if result.matches else ""
        return AdsResult(
            "PREPRINT_PUBLISHED",
            result.query,
            result.matches,
            f"the entry cites the arXiv preprint, but ADS resolves it to the published record {bibcode}"
            + (f" ({venue})" if venue else ""),
            ads_bibtex=ads_bibtex,
        )
    if conflicts:
        return AdsResult(
            "ADS_RECORD_CONFLICT",
            result.query,
            result.matches,
            f"local {'/'.join(conflicts)} differs from ADS export for {bibcode}; manual review required",
            ads_bibtex=ads_bibtex,
        )

    if not local_bibcode:
        return AdsResult(
            "NON_ADS_BIBTEX",
            result.query,
            result.matches,
            f"entry resolves to ADS bibcode {bibcode}, but has no local adsurl/bibcode",
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


def propose_replacement(
    entry: BibEntry,
    bibcode: str,
    result: AdsResult,
    token: str,
    timeout: float,
    reason: str,
) -> AdsResult:
    """Attach the ADS export to a record the entry's own identifiers did not find.

    The point is to hand over a BibTeX to look at rather than a dead end. A real
    disagreement still comes back as ADS_RECORD_CONFLICT; otherwise the status is
    IDENTIFIER_MISMATCH, which is the truth - the record is right, the entry's own
    identifiers are not - and which is never a one-keypress replacement unless the
    export itself agrees on title, author, DOI, eprint and key.
    """
    verified = verify_ads_bibtex(entry, bibcode, result, token, timeout, local_bibcode=False)
    if verified.status in {"ERROR", "ADS_RECORD_CONFLICT", "CITATION_KEY_CONFLICT"}:
        return verified
    return AdsResult("IDENTIFIER_MISMATCH", verified.query, verified.matches, reason, ads_bibtex=verified.ads_bibtex)


def resolve_by_fallback(
    entry: BibEntry,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> tuple[str, str, AdsResult] | None:
    """Find the record without trusting the entry's identifiers. (route, bibcode, result).

    Title+year first, then ADS's own reference resolver on author/year/journal/
    volume/page - the coordinates no Solr query here uses, and the only route that
    works when the title has been reworded or truncated.
    """
    title_queries = [pair for pair in candidate_queries(entry, include_bibcode=False) if pair[0] == "title"]
    if title_queries:
        result = run_queries(title_queries, token, rows, timeout, sleep)
        if result.status == "OK":
            return ("title and year", str(result.matches[0].get("bibcode", "")), result)

    if reference := reference_string(entry):
        try:
            bibcode = ads_resolve_reference(reference, token, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError):
            # A separate microservice. If it is down, keep the MISSING verdict and
            # its handoff link rather than replacing both with ERROR.
            bibcode = None
        if bibcode:
            lookup = run_queries([("bibcode", f'bibcode:"{escape_query_value(bibcode)}"')], token, rows, timeout, sleep)
            matches = lookup.matches if lookup.status == "OK" else [{"bibcode": bibcode}]
            return ("the ADS reference resolver", bibcode, AdsResult("OK", reference, matches))
    return None


def with_fallback(
    entry: BibEntry,
    missing: AdsResult,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    """Nothing the entry claims about itself resolved; try the routes that ignore it."""
    if missing.status != "MISSING":
        return missing
    found = resolve_by_fallback(entry, token, rows, timeout, sleep)
    if found is None:
        return missing
    route, bibcode, result = found
    return propose_replacement(
        entry,
        bibcode,
        result,
        token,
        timeout,
        f"no bibcode/DOI/eprint lookup resolved, but {route} matched {bibcode}; review before replacing",
    )


def published_upgrade(
    entry: BibEntry,
    result: AdsResult,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult | None:
    """The refereed record for an entry still citing the preprint, or None.

    ADS eventually folds an arXiv record into the journal one, and `preferred_match`
    handles that case for free. Until it does, both records exist separately and the
    preprint resolves perfectly: every identifier agrees, every field agrees, and
    nothing at all says the paper is out. Only a title search finds the other record.
    """
    if result.status not in PREPRINT_UPGRADE_STATUSES or not result.matches:
        return None
    if not is_arxiv_bibcode(str(result.matches[0].get("bibcode", ""))):
        return None
    title = entry.fields.get("title", "")
    if not title:
        return None

    query = f'title:"{escape_query_value(normalized_identity_value(title))}"'
    try:
        matches, failure = ads_search_guarded("published", query, token, rows, timeout, sleep)
        if failure is not None:
            return None
        published = [
            match
            for match in matches
            if not is_arxiv_bibcode(str(match.get("bibcode", "")))
            and alphanumeric_key(match_title(match)) == alphanumeric_key(title)
        ]
        if len(published) != 1:
            return None
        bibcode = str(published[0].get("bibcode", ""))
        ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
    except (AdsRateLimitError, RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        # A verdict the entry has already earned must not be downgraded because
        # the extra lookup failed; the next run asks again.
        return None

    ads_entry = parsed_ads_entry(entry, ads_bibtex)
    if ads_entry is None:
        return None
    # The DOI and eprint are meant to differ - that is the whole point - so only a
    # disagreement that publication cannot explain rules the record out.
    if [conflict for conflict in identity_conflicts(entry, ads_entry) if conflict not in {"doi", "eprint"}]:
        return None

    venue = record_venue(published[0])
    return AdsResult(
        "PREPRINT_PUBLISHED",
        query,
        published,
        f"the entry cites the arXiv preprint, but this paper is published as {bibcode}"
        + (f" ({venue})" if venue else "")
        + "; ADS has not merged the two records",
        ads_bibtex=ads_bibtex,
    )


def check_entry_live(entry: BibEntry, token: str, rows: int, timeout: float, sleep: float) -> AdsResult:
    local_bibcode = ads_bibcode(entry)
    identifiers = local_identifiers(entry)
    fallback_queries = candidate_queries(entry, include_bibcode=False)
    if not identifiers and not fallback_queries:
        return AdsResult("NO_IDENTIFIER", "", [], "no bibcode, adsurl, DOI, eprint, or title+year")

    if not identifiers:
        result = run_queries(fallback_queries, token, rows, timeout, sleep)
        if result.status == "OK":
            bibcode = str(result.matches[0].get("bibcode", ""))
            return verify_ads_bibtex(entry, bibcode, result, token, timeout, local_bibcode=False)
        return with_fallback(entry, result, token, rows, timeout, sleep)

    consensus = identifier_consensus(entry, token, rows, timeout, sleep)
    if consensus.status == "OK":
        # The record's own bibcode, not the entry's: an adsurl often names the
        # preprint alias of a record ADS has since merged, and only the canonical
        # bibcode exports. Citing the preprint of a now-published paper is the
        # case this tool exists for.
        resolved = str(consensus.matches[0].get("bibcode", "")) if consensus.matches else ""
        bibcode = resolved or local_bibcode
        return verify_ads_bibtex(entry, bibcode, consensus, token, timeout, local_bibcode=bool(local_bibcode))
    # Always arrive with something to look at: a partial resolve still gets its export.
    if consensus.status == "IDENTIFIER_MISMATCH" and (bibcode := ads_replacement_bibcode(consensus)):
        return propose_replacement(entry, bibcode, consensus, token, timeout, consensus.message)
    return with_fallback(entry, consensus, token, rows, timeout, sleep)


def check_entry(
    entry: BibEntry,
    token: str,
    rows: int,
    timeout: float,
    sleep: float,
) -> AdsResult:
    try:
        result = check_entry_live(entry, token, rows, timeout, sleep)
        return published_upgrade(entry, result, token, rows, timeout, sleep) or result
    except AdsRateLimitError as exc:
        return rate_limited_result(entry, exc.wait)


def format_match(match: dict[str, object]) -> str:
    bibcode = str(match.get("bibcode", ""))
    year = str(match.get("year", ""))
    return f"{bibcode} {year} {match_title(match)}".strip()


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


def issue_search_url(entry: BibEntry, result: AdsResult) -> str:
    """A deliberately loose ADS search, for the entries a human has to finish.

    Replaying the query that just returned nothing helps nobody. Dropping the
    phrase quotes and widening the year by one is what actually finds a record
    whose title was reworded between the preprint and the journal.
    """
    terms: list[str] = []
    # Punctuation only narrows a search a human is about to eyeball.
    title = " ".join(NON_ALNUM_RE.sub(" ", normalized_identity_value(entry.fields.get("title", ""))).split())
    if title:
        terms.append(f"title:({escape_query_value(title)})")
    if author := first_author_name(entry.fields.get("author", "")):
        terms.append(f'author:"{escape_query_value(author.split(",")[0].strip())}"')
    year = entry.fields.get("year", "").strip()
    if year.isdigit():
        terms.append(f"year:{int(year) - 1}-{int(year) + 1}")
    if terms:
        return ads_search_url(" ".join(terms))
    return ads_search_url(result.query) if result.query and result.query != "local" else ""


def print_issue_action(result: AdsResult) -> None:
    if action := ISSUE_ACTIONS.get(result.status):
        print_detail("Action", action)


def print_issue_search(entry: BibEntry, result: AdsResult) -> None:
    if result.status in HANDOFF_STATUSES and (url := issue_search_url(entry, result)):
        print_detail("Search", url)


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
    if result.status == "ADS_RECORD_CONFLICT":
        if bibcode := replacement_bibcode(result):
            print_detail(
                "Suggestion",
                f"ADS export for {bibcode} is available, but compare it manually before replacing because it may be a different paper",
            )
        return

    if bibcode := ads_replacement_bibcode(result):
        print_detail(
            "Suggestion",
            f"review the ADS export for {bibcode}; run with --replace to choose ADS, paste manual, or skip while keeping key {entry.key}",
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
    print_issue_search(entry, result)


def replace_bibtex_key(bibtex: str, key: str) -> str:
    return ENTRY_RE.sub(lambda match: f"@{match.group('kind')}{{{key},", bibtex, count=1)


def validated_replacement(bibtex: str, key: str) -> str:
    replacement = replace_bibtex_key(bibtex.strip(), key)
    parsed = parse_bibtex_text(replacement)
    if len(parsed) != 1:
        raise ValueError(f"replacement must be exactly one BibTeX entry, not {len(parsed)}")
    if parsed[0].key != key:
        raise ValueError(f"the replacement would rename {key} to {parsed[0].key}; remove any @comment or @string before the entry")
    return replacement


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


def prompt_risky_replacement_choice(entry_key: str, conflicts: list[str]) -> str:
    """Replacement menu for a candidate that may be a different paper.

    Defaults to skip and demands a typed confirmation, so a formatting refresh
    stays one keypress away while overwriting a mismatched entry does not.
    """
    while True:
        print(f"\nReplacement choice for {entry_key}:")
        print(colorize(f"  The ADS record disagrees on {', '.join(conflicts)}; it may be a different paper.", UNAVAILABLE_COLOR))
        print("  1. Skip [default]")
        print("  2. Paste manual replacement")
        print("  3. Use ADS replacement (requires typed confirmation)")
        answer = input(colorize("Select 1, 2, or 3 [1]: ", PROMPT_COLOR)).strip().lower()
        if answer in {"", "1", "skip", "s", "n", "no"}:
            return "skip"
        if answer in {"2", "manual", "m", "paste", "p"}:
            return "manual"
        if answer in {"3", "ads"}:
            confirm = input(colorize(f"Type 'replace' to overwrite {entry_key} with the ADS record: ", PROMPT_COLOR)).strip().lower()
            if confirm == "replace":
                return "ads"
            print("Not confirmed; returning to the menu.")
            continue
        print("Please enter 1 to skip, 2 for a manual replacement, or 3 for the ADS record.")


def print_identity_comparison(entry: BibEntry, ads_entry: BibEntry, conflicts: list[str]) -> None:
    print(colorize(f"\n    Identity check: local and ADS disagree on {', '.join(conflicts)}", UNAVAILABLE_COLOR))
    for field in ("author", "title", "year"):
        print_detail(f"local {field}", entry.fields.get(field, "-"), label_width=12)
        print_detail(f"ADS {field}", ads_entry.fields.get(field, "-"), label_width=12)


def prompt_replacement_choice(entry_key: str, has_ads_replacement: bool) -> str:
    while True:
        print(f"\nReplacement choice for {entry_key}:")
        if has_ads_replacement:
            print("  1. Use ADS replacement [default]")
            print("  2. Paste manual replacement")
            print("  3. Skip")
            answer = input(colorize("Select 1, 2, or 3 [1]: ", PROMPT_COLOR)).strip().lower()
            if answer in {"", "1", "ads", "replace", "r", "y", "yes"}:
                return "ads"
            if answer in {"2", "manual", "m", "paste", "p"}:
                return "manual"
            if answer in {"3", "skip", "s", "n", "no"}:
                return "skip"
            print("Please enter 1 for ADS, 2 for manual, or 3 to skip.")
        else:
            print("  1. Paste manual replacement [default]")
            print(colorize("  2. Use ADS replacement (unavailable)", UNAVAILABLE_COLOR))
            print("  3. Skip")
            answer = input(colorize("Select 1 or 3 [1]: ", PROMPT_COLOR)).strip().lower()
            if answer in {"", "1", "manual", "m", "paste", "p", "replace", "r", "y", "yes"}:
                return "manual"
            if answer in {"3", "skip", "s", "n", "no"}:
                return "skip"
            if answer == "2":
                print(colorize("ADS replacement is unavailable for this entry.", UNAVAILABLE_COLOR))
                continue
            print("Please enter 1 for manual replacement or 3 to skip.")


def prompt_manual_replacement_choice(entry_key: str) -> str:
    while True:
        print(f"\nUse pasted replacement for {entry_key}?")
        print("  1. Replace [default]")
        print("  2. Paste again")
        print("  3. Skip")
        answer = input(colorize("Select 1, 2, or 3 [1]: ", PROMPT_COLOR)).strip().lower()
        if answer in {"", "1", "replace", "r", "y", "yes"}:
            return "replace"
        if answer in {"2", "again", "retry", "manual", "m", "paste", "p"}:
            return "again"
        if answer in {"3", "skip", "s", "n", "no"}:
            return "skip"
        print("Please enter 1 to replace, 2 to paste again, or 3 to skip.")


def prompt_replacement_session() -> bool:
    while True:
        answer = input(colorize("\nProceed with replacement? [y/N]: ", PROMPT_COLOR)).strip().lower()
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

    first_line = input(colorize("BibTeX> ", PROMPT_COLOR))
    if not first_line.strip():
        return None
    if first_line.strip().lower() in {"s", "skip"}:
        return None

    lines = [first_line]
    while True:
        line = input(colorize("... ", PROMPT_COLOR))
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
    """Replace a file's contents in one step, through its symlink and mode.

    Without resolving, a .bib symlinked from a shared master is silently turned
    into a private 0600 copy and the master never changes.
    """
    path = path.resolve()
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
        if path.exists():
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def write_replacements(path: Path, text: str, replacements: list[tuple[BibEntry, str]], backup: Path | None) -> Path:
    """Refuse a snapshot changed while the user was reviewing it."""
    if path.read_text(encoding="utf-8") != text:
        raise ValueError("the .bib changed on disk; reload before writing")
    backup = ensure_backup(path, backup)
    write_text_atomically(path, apply_replacements(text, replacements))
    return backup


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
        print(colorize("  No automatic or manual replacement candidates were found.", UNAVAILABLE_COLOR))
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
        # Re-read every time: an edit saved in an editor between two accepts
        # would otherwise be overwritten by the text this loop started with.
        original_text = bibfile.read_text(encoding="utf-8")
        matching = [current for current in parse_bibtex_text(original_text) if current.key == entry.key]
        if not matching:
            print(f"\nSkipping {entry.key}: entry no longer exists in {bibfile}.")
            continue
        if len(matching) > 1:
            print(f"\nSkipping {entry.key}: {len(matching)} entries share this key; remove the duplicate first.")
            continue
        current_entry = matching[0]

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
        conflicts: list[str] = []
        if bibcode is not None:
            ads_bibtex = result.ads_bibtex
            if not ads_bibtex:
                try:
                    ads_bibtex = ads_export_bibtex(bibcode, token, timeout)
                except AdsRateLimitError as exc:
                    print(colorize(f"\nADS replacement unavailable for {current_entry.key}: ADS API rate limit is active: {exc}", UNAVAILABLE_COLOR))
                except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                    print(colorize(f"\nADS replacement unavailable for {current_entry.key}: could not fetch ADS BibTeX for {bibcode}: {exc}", UNAVAILABLE_COLOR))
            if ads_bibtex:
                ads_replacement = replace_bibtex_key(ads_bibtex, current_entry.key)
                print_detail("ADS bibcode", bibcode, indent="  ")
                print_bibtex_block("ADS Replacement", ads_replacement)
                ads_entry = parsed_ads_entry(current_entry, ads_bibtex)
                if ads_entry is not None and (conflicts := identity_conflicts(current_entry, ads_entry)):
                    print_identity_comparison(current_entry, ads_entry, conflicts)
        print("\n" + "-" * 100)

        if ads_replacement is not None and conflicts:
            choice = prompt_risky_replacement_choice(current_entry.key, conflicts)
        else:
            choice = prompt_replacement_choice(current_entry.key, ads_replacement is not None)
        if choice == "skip":
            print(f"Skipped {current_entry.key}.")
            continue

        if choice == "ads" and ads_replacement is not None:
            try:
                backup_path = write_replacements(bibfile, original_text, [(current_entry, ads_replacement)], backup_path)
            except ValueError as exc:
                print(f"Skipping {current_entry.key}: {exc}")
                continue
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

            try:
                replacement = validated_replacement(pasted, current_entry.key)
            except ValueError as exc:
                print(f"Could not use pasted BibTeX: {exc}")
                continue
            print_bibtex_block("Pasted Replacement", replacement)
            print("\n" + "-" * 100)
            manual_choice = prompt_manual_replacement_choice(current_entry.key)
            if manual_choice == "again":
                continue
            if manual_choice == "skip":
                print(f"Skipped {current_entry.key}.")
                break

            try:
                backup_path = write_replacements(bibfile, original_text, [(current_entry, replacement)], backup_path)
            except ValueError as exc:
                print(f"Skipping {current_entry.key}: {exc}")
                break
            replacement_count += 1
            print(f"Updated {current_entry.key} in {bibfile}.")
            break

    if replacement_count == 0:
        print("\nNo replacements accepted; file unchanged.")
        return 0

    print(f"\nApplied {replacement_count} replacement(s) to {bibfile}.")
    return replacement_count


def resolved_identity(entry: BibEntry, result: AdsResult) -> tuple[str, str] | None:
    """A stable identity for duplicate grouping: the resolved bibcode, else the local DOI."""
    if result.status in RESOLVED_STATUSES and result.matches and (bibcode := str(result.matches[0].get("bibcode", ""))):
        return ("bibcode", bibcode)
    if doi := entry.fields.get("doi"):
        return ("doi", normalized_identity_value(doi))
    return None


def duplicate_groups(results: list[tuple[BibEntry, AdsResult]]) -> list[tuple[tuple[str, str], list[BibEntry]]]:
    groups: dict[tuple[str, str], list[BibEntry]] = {}
    for entry, result in results:
        if identity := resolved_identity(entry, result):
            groups.setdefault(identity, []).append(entry)
    return [(identity, entries) for identity, entries in groups.items() if len(entries) > 1]


def print_duplicates(results: list[tuple[BibEntry, AdsResult]]) -> int:
    duplicates = duplicate_groups(results)
    print("\nDuplicates")
    if not duplicates:
        print("  None")
        return 0
    for (kind, value), entries in duplicates:
        keys = ", ".join(f"{entry.key} (line {entry.line})" for entry in entries)
        print(f"  {kind} {value}")
        print_detail("Keys", keys)
    return len(duplicates)


def entry_warnings(results: list[tuple[BibEntry, AdsResult]]) -> list[tuple[BibEntry, str, str]]:
    """Advisory notes: real, worth knowing, and not something to decide in the app."""
    notes: list[tuple[BibEntry, str, str]] = []
    for entry, result in results:
        if malformed_author(entry):
            notes.append((
                entry,
                "author field contains a literal 'et al.', which renders as '(Smith & et al. 2020)'",
                "replace it with the remaining author names, or with BibTeX's 'and others'",
            ))
        ads_entry = parsed_ads_entry(entry, result.ads_bibtex) if result.ads_bibtex else None
        if ads_entry is None:
            continue
        if drift := key_year_drift(entry.key, ads_entry):
            notes.append((
                entry,
                f"the citation key says {CITATION_KEY_RE.match(entry.key).group('year')}, but this record is "
                f"{ads_entry.fields.get('year', '')} ({abs(drift)} years {'later' if drift > 0 else 'earlier'})",
                "usually the key names the journal year while the entry is the preprint; rename the key, "
                "or point the entry at the published record, or leave it",
            ))
    return notes


def print_warnings(results: list[tuple[BibEntry, AdsResult]]) -> None:
    notes = entry_warnings(results)
    print("\nWarnings")
    if not notes:
        print("  None")
        return
    for entry, issue, action in notes:
        print(f"  {entry.key} (line {entry.line})")
        print_detail("Issue", issue)
        print_detail("Action", action)


def cited_keys(text: str) -> set[str]:
    text = TEX_COMMENT_RE.sub("", text)
    keys: set[str] = set()
    for match in CITE_RE.finditer(text):
        keys.update(key.strip() for key in match.group("keys").split(",") if key.strip())
    return keys


def print_tex_crosscheck(entries: list[BibEntry], tex_paths: list[Path]) -> int:
    cited: set[str] = set()
    for path in tex_paths:
        try:
            cited |= cited_keys(path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"Could not read {path}: {exc}", file=sys.stderr)
            return 1

    defined = {entry.key for entry in entries}
    undefined = sorted(cited - defined - {"*"})
    uncited = sorted(defined - cited)

    print("\nTeX cross-check")
    print_detail("Sources", ", ".join(str(path) for path in tex_paths))
    if undefined:
        print_detail("Undefined", f"{len(undefined)} key(s) cited in the .tex but absent from the .bib")
        for key in undefined:
            print(f"      - {key}")
    if uncited:
        print_detail("Uncited", f"{len(uncited)} entry(ies) defined in the .bib but never cited: {', '.join(uncited)}")
    if not undefined and not uncited:
        print("  Every cited key is defined and every entry is cited.")
    return len(undefined)


def print_report(results: list[tuple[BibEntry, AdsResult]], verbose: bool) -> int:
    counts: dict[str, int] = {}
    for _, result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    total = len(results)
    print("\nSummary")
    width = max([13, *(len(status) for status in counts)])
    print(f"  {'entries':<{width}} {total}")
    for status, count in ordered_counts(counts):
        print(f"  {status:<{width}} {count}")

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

    duplicate_count = print_duplicates(results)
    print_warnings(results)

    if verbose:
        ok_results = [(entry, result) for entry, result in results if result.status not in ISSUE_STATUSES]
        if ok_results:
            print("\nResolved")
            for entry, result in ok_results:
                print_result(entry, result)

    failing = ISSUE_STATUSES
    return 1 if duplicate_count or any(result.status in failing for _, result in results) else 0


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
    prefetch_exports(entries, token, timeout)
    if jobs == 1:
        results: list[tuple[BibEntry, AdsResult]] = []
        for entry in progress_iter(entries, len(entries), progress, "Checking ADS"):
            # Same guard as the worker pool below: a truncated response or a
            # captive-portal HTML page must cost one entry, not the whole run.
            try:
                result = check_entry(entry, token, rows=rows, timeout=timeout, sleep=sleep)
            except AdsRateLimitError:
                raise
            except Exception as exc:
                result = AdsResult("ERROR", "", [], f"check failed: {exc}")
            results.append((entry, result))
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
    parser.add_argument(
        "--review",
        action="store_true",
        help="Open the browser review app even when the output is piped.",
    )
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="Print the report and stop; do not open the browser review app.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Serve the review app, but do not launch a browser.",
    )
    parser.add_argument(
        "--tex",
        type=Path,
        nargs="+",
        default=[],
        metavar="PATH",
        help="Cross-check .tex sources for cited-but-undefined and defined-but-uncited keys.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every entry, not only problems.")
    return parser


def wants_review(args: argparse.Namespace, interactive: bool) -> bool:
    """The app is the default way to work through issues; the report alone is for pipes.

    Same rule as `colorize`: a terminal gets the rich thing, anything redirected
    gets plain text. Without it `check_ads_bib.sh ref.bib | less` and any CI step
    would hang on a server nobody is going to open.
    """
    if args.no_review or args.replace:
        return False
    return args.review or interactive


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
    # Skipped entries stay visible to the TeX cross-check; they just are not sent to ADS.
    all_entries = entries
    entries = [entry for entry in all_entries if not entry.skip]
    skipped_count = len(all_entries) - len(entries)
    if not entries:
        print(f"Every entry in {args.bibfile} is marked '% checkcitation: skip'.", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("--jobs must be at least 1.", file=sys.stderr)
        return 2
    if args.replace and args.review:
        print("Use --replace or --review, not both.", file=sys.stderr)
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
    if skipped_count:
        print(f"\n{skipped_count} entry(ies) not checked because of a '% checkcitation: skip' directive.")
    if args.tex and print_tex_crosscheck(all_entries, args.tex):
        exit_code = 1
    if args.replace:
        replace_outdated_entries(args.bibfile, results, args.token, args.timeout)
    if wants_review(args, sys.stdout.isatty()):
        import review

        session = review.Review(
            args.bibfile,
            args.token,
            rows=args.rows,
            timeout=args.timeout,
            sleep=args.sleep,
            jobs=args.jobs,
            tex=args.tex,
        )
        session.adopt(results)
        review.serve(session, open_browser=not args.no_open)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
