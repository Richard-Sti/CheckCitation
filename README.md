# CheckCitation

Small CLI for checking whether entries in a BibTeX file resolve on NASA ADS.

## Behaviour

The tool reads every entry in the `.bib` file and checks whether it can be
resolved unambiguously on ADS.

For each entry it tries, in order:

1. ADS bibcode from `bibcode` or `adsurl`.
2. DOI.
3. DOI through the ADS `identifier` field.
4. arXiv ID from `eprint`.
5. `title` plus `year`.

The check succeeds only when ADS returns exactly one record and the local BibTeX
entry matches the ADS-exported BibTeX for that record. The final report prints
an ordered summary and an issues section for entries marked
`ADS_BIBTEX_MISMATCH`, `NON_ADS_BIBTEX`, `BIBCODE_MISMATCH`,
`ADS_RECORD_CONFLICT`, `IDENTIFIER_CONFLICT`, `IDENTIFIER_MISMATCH`,
`ADS_UNVERIFIED_RATE_LIMITED`, `RATE_LIMITED`, `MISSING`, `AMBIGUOUS`,
`NO_IDENTIFIER`, or `ERROR`. Each issue includes the BibTeX key, line number,
relevant ADS query, and matching ADS records when available.

`ADS_BIBTEX_MISMATCH` means the entry has ADS provenance through `adsurl` or
`bibcode`, but the local citation-bearing BibTeX fields differ from the BibTeX
currently exported by ADS for that bibcode. Metadata/local-management fields are
ignored for this comparison: `keywords`, `adsnote`, `abstract`, `annotation`,
`file`, `url`, `urldate`, and `note`.

`NON_ADS_BIBTEX` means the entry resolves to exactly one ADS record through DOI,
arXiv, or title lookup, but the local BibTeX entry does not contain ADS
provenance through `adsurl` or `bibcode`. These entries exist on ADS but should
still be replaced if you want every bibliography item to come from ADS.

`ADS_RECORD_CONFLICT` means the local entry has ADS provenance, but the `title`,
`doi`, or `eprint` field disagrees with the ADS export for that bibcode. The
tool reports this but disables automatic replacement, because the local entry
may be pointing at the wrong ADS record or may contain mixed fields from two
different papers. Author-list differences alone are treated as ordinary BibTeX
mismatches rather than record conflicts.

`IDENTIFIER_CONFLICT` means multiple identifiers present in the same entry
(`adsurl`/`bibcode`, DOI, and/or arXiv ID) resolve to different ADS records. The
tool reports this for manual review and disables automatic replacement.

`BIBCODE_MISMATCH` means the local ADS bibcode and DOI/arXiv/title lookup point
to different ADS records. The tool reports this but does not offer automatic
replacement, because this can reflect a genuine choice between, for example, a
published proceedings record and an arXiv record.

`IDENTIFIER_MISMATCH` means at least one identifier resolves, but another
identifier present in the same BibTeX entry does not resolve to ADS. This often
indicates a typo or stale identifier in an otherwise ADS-linked entry.

`ADS_UNVERIFIED_RATE_LIMITED` means ADS rate-limited an uncached request, but
the entry has a local ADS bibcode or `adsurl`. The tool records that local ADS
identifier and continues without making further uncached ADS requests. This is
not treated as a verified ADS export, and automatic replacement is disabled for
that entry.

`RATE_LIMITED` means ADS rate-limited an uncached request before the entry could
be resolved, and the entry has no local ADS bibcode or `adsurl` to report.

Live ADS checks are parallelised with `--jobs`. Results are printed in the
original BibTeX order, even though individual ADS requests finish out of order.
The default is conservative: `--jobs 1` and a 0.1-second sleep before each ADS
search request in each worker when the response is not already cached. If ADS
returns HTTP 429, the tool retries with backoff; if ADS asks for a long cooldown,
the tool stops live ADS requests, continues with cached responses, and flags
uncached ADS-linked entries as `ADS_UNVERIFIED_RATE_LIMITED`. If replacement
mode sees ADS errors, it skips replacement for that run.

ADS search responses and ADS-exported BibTeX records are cached locally in
`.ads_cache.json` next to the script. Cached records expire after 24 hours by
default. During a rate limit, entries already present in the cache can still be
checked without contacting ADS; if a required response is not cached, the entry
is flagged as rate-limited and the run continues without further uncached ADS
requests. Within a single run, workers also share in-memory ADS responses so
duplicate entries do not trigger overlapping live requests. Use
`--refresh-cache` to ignore cached file responses and write fresh successful
responses, or `--no-cache` to disable the persistent cache file.

The exit code is `0` only when every checked entry resolves cleanly. This makes
the command suitable for a paper build script or CI check.

## Setup

Create an ADS API token from your ADS account and expose it as:

```sh
export ADS_API_TOKEN="..."
```

The wrapper uses `/Users/rstiskalek/Tools/venv_tools`. Install `tqdm` there for
the progress bar:

```sh
/Users/rstiskalek/Tools/venv_tools/bin/python3 -m pip install tqdm
```

## Usage

Run a strict ADS check:

```sh
python3 check_ads_bib.py /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Or use the wrapper that runs through `/Users/rstiskalek/Tools/venv_tools`:

```sh
./check_ads_bib.sh /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

The wrapper also forwards flags:

```sh
./check_ads_bib.sh --jobs 8 /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Print every entry rather than only problems:

```sh
python3 check_ads_bib.py -v /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Disable the progress bar:

```sh
python3 check_ads_bib.py --no-progress /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Control parallelism:

```sh
python3 check_ads_bib.py --jobs 8 /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Control the ADS response cache:

```sh
python3 check_ads_bib.py --cache-ttl 86400 /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
python3 check_ads_bib.py --refresh-cache /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
python3 check_ads_bib.py --no-cache /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Interactively replace entries whose local BibTeX does not come from ADS:

```sh
python3 check_ads_bib.py --replace /Users/rstiskalek/Papers/tSZ-Olympics/ref.bib
```

Replacement mode first prints a summary of entries with ADS replacements and
manual-only replacements, then asks whether to proceed. If the check found a
single ADS record, including for statuses such as `ADS_BIBTEX_MISMATCH`,
`NON_ADS_BIBTEX`, `ADS_RECORD_CONFLICT`, or `IDENTIFIER_MISMATCH`, the per-entry
prompt offers three choices: use the ADS export, paste a manual replacement, or
skip. Manual-only paste replacements are offered for unresolved or multi-record
conflicts such as `MISSING`, `IDENTIFIER_CONFLICT`, and `AMBIGUOUS`. Every
accepted replacement keeps the existing citation key. For manual replacements,
paste one BibTeX entry and press Enter on a blank line to submit it; you can
also finish with a line containing only `.`. When a replacement is accepted, the
`.bib` file is backed up once and then updated atomically before the next
candidate is shown.

Exit code is `0` only when every entry resolves unambiguously and matches ADS
export. Any reported issue returns exit code `1`.
