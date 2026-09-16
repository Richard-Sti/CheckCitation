# Tool to check citations against ADS

Small CLI for checking whether BibTeX entries resolve on NASA ADS and match the
BibTeX currently exported by ADS.

## Setup

Create an ADS API token from your ADS account and expose it as:

```sh
export ADS_API_TOKEN="..."
```

To make the token available in every new shell, add the same line to your shell
startup file. For example, zsh users can add it to `~/.zshrc`:

```sh
echo 'export ADS_API_TOKEN="..."' >> ~/.zshrc
source ~/.zshrc
```

For bash, use `~/.bashrc` instead:

```sh
echo 'export ADS_API_TOKEN="..."' >> ~/.bashrc
source ~/.bashrc
```

ADS documents its current API rate-limit policy at
<https://ui.adsabs.harvard.edu/help/policies/rate-limits>.

The optional progress bar uses `tqdm`:

```sh
python3 -m pip install tqdm
```

If you use a virtual environment, either activate it before running the tool or
point the wrapper at its Python interpreter:

```sh
export CHECK_ADS_BIB_PYTHON=/path/to/venv/bin/python3
```

## Usage

Check a bibliography, and review what came back:

```sh
./check_ads_bib.sh path/to/ref.bib
```

That prints the report and then opens the browser review app, which is where
replacements are made. Redirect the output and it prints the report and stops
instead, so `... | less` and any CI step behave as they always did:

```sh
./check_ads_bib.sh path/to/ref.bib | less
./check_ads_bib.sh path/to/ref.bib --no-review     # same, from a terminal
./check_ads_bib.sh path/to/ref.bib --no-open       # serve it, but don't launch a browser
./check_ads_bib.sh path/to/ref.bib --review        # force the app on even when piped
```

You can also call the Python script directly:

```sh
python3 check_ads_bib.py path/to/ref.bib
```

Replacements can also be made from the terminal, one prompt at a time, instead of
in the browser:

```sh
./check_ads_bib.sh path/to/ref.bib --replace
```

Also cross-check which keys your paper actually cites:

```sh
./check_ads_bib.sh path/to/ref.bib --tex paper.tex sections/*.tex
```

Useful options:

```sh
./check_ads_bib.sh path/to/ref.bib --jobs 4
./check_ads_bib.sh path/to/ref.bib --refresh-cache
./check_ads_bib.sh path/to/ref.bib --no-cache
./check_ads_bib.sh path/to/ref.bib --no-progress
```

## The review app

The app is how you work through what the report found, so it opens by default:
the usual check runs, the usual report prints, and then a local page is served at
<http://localhost:8766>. Stdlib only, bound to `127.0.0.1`, no build step
and nothing to install. The only file it writes is the `.bib` you pointed it at.

Three views under a stats strip. **Every stat is a way in**: click *Checked and
fine* to see exactly which entries you accepted, *Needs a decision* for what is
still open, *Replaced* to jump to this session's replacements. **Clear decisions**
in the header appears whenever there is something to forget, asks once, and drops
every recorded verdict for this bibliography — the `.bib` itself is never touched,
and no other bibliography's decisions are affected.

- **Review** — one card per entry ADS disagrees with, in file order. Each card shows
  the issue in the report's own words, the local entry against the ADS export field
  by field with the disagreeing fields marked, the ADS records that matched, and the
  proposed BibTeX in an editable box. `->`/`l` replaces, `<-`/`h` keeps the local
  entry, `space` defers it to the back of the queue, `u` undoes the last replacement
  by writing the entry back exactly as it was.
- **All** — every entry with its status and line, filterable to issues, entries that
  agree with ADS, ones you have accepted, or skipped ones, sortable by key, line or
  status. `Review` on any row sends it to the front of the card stack, and `↺ checked`
  withdraws an acceptance.
- **Cross-check** — duplicates, warnings, and, when `--tex` is given, the keys cited
  but undefined and the entries defined but never cited.

Every card that resolved arrives with the proposed BibTeX already in the box and
every ADS record linked to its abstract page. A card that resolved nowhere says so
and offers the ADS search to run instead, because at that point the tool has done
what it can and finding the record is yours.

Acting on an entry settles it: if ADS still disagrees after the write, the entry is
marked checked rather than put straight back at the top of the queue.
Some conflicts no rewrite can clear — a citation key is kept by design, so a key
the record disagrees with would otherwise be raised for ever.

**Nothing touches your `.bib` until you say so.** Working through the cards stages
decisions; the header counts them (`ref.bib · 6 staged, not written`) and a **Write
6 changes to ref.bib** button appears. That one click takes a single backup and
makes a single atomic write, and it is all-or-nothing: if any staged edit no longer
applies, none of them are written, so the file is never left holding half a review.

Staged edits are kept in the browser against the bibliography's full path, so
closing the tab does not lose them, and `Unstage` takes one back off the pile.
Marking an entry **checked** is not a file edit and still saves immediately — that
store exists precisely so a judgement survives a restart.

The citation key is always kept, whatever the pasted or exported BibTeX says. One
backup per session is written before the first replacement, and every write is
atomic. A replacement is only ever one keypress when the ADS export is in hand and
disagrees with nothing; anything that might be a different paper takes a button and
an inline confirmation, which is the browser's version of the CLI's typed `replace`.
If the `.bib` changes on disk while a tab is open, that tab's next write is refused
with a reload prompt instead of overwriting the edit.

## What It Does

For each BibTeX entry, the tool tries to resolve an ADS record using the local
`bibcode`/`adsurl`, DOI, and arXiv ID. If none of those resolve, it does not give
up: it falls back to a title-plus-year search, and then to ADS's own reference
resolver, which matches on author, year, journal, volume and page — the
coordinates no query here uses, and the only route that still works when the
title was reworded between the preprint and the journal. Either fallback route
reports `IDENTIFIER_MISMATCH`, because the record is right and the entry's own
identifiers are not.

A fallback match is never taken on trust. The resolver reports a confidence
score, and that score is ignored: `Riess, A. G. 2022, ApJ, 934, L7` with the page
mistyped as `L9` comes back at 0.7 pointing at a different author's paper. Every
candidate, however it was found, goes through the same identity check as a
bibcode, so a typo'd DOI on an otherwise correct entry resolves and is flagged
`ADS_RECORD_CONFLICT` for the `doi` field rather than silently replaced.

When nothing matches at all, the entry is reported `MISSING` with a `Search` link:
a deliberately loose ADS query built from the title words, first author and a
±1 year range. Replaying the query that just returned nothing helps nobody.

Whichever route resolves, the ADS record is then checked against the local entry
on title, first-author surname, DOI, eprint, and the citation key. Titles are
compared fuzzily after LaTeX markup is stripped, so `H\,{\sc i}` and `H I` agree,
but their series numbering has to match exactly: `Paper I` and `Paper II` score
0.99 on a character ratio and are not the same paper.
The citation key check is the only signal independent of the entry's own fields:
a key of the form `Surname2020` is compared against the record's **first author**,
which is what catches an entry that is internally consistent but is simply the
wrong paper. When that is the only disagreement the entry is reported
`CITATION_KEY_CONFLICT` and **no replacement is offered**: the citation key is kept
by design, so rewriting the body could only ever produce the same file. Renaming
the key means renaming every `\cite{}` to it, so that decision stays yours — or
paste the record the key actually names, which is still allowed.

A key whose **year** disagrees is a *warning*, not an issue. `Hoffman2014` pointing
at the 2011 arXiv preprint means the key names the journal year and the entry is
the preprint — the entry still is that record, so there is nothing to decide in the
app and nothing to fail a build over. It is listed under `Warnings` with what you
might do about it, and it does not affect the exit code. A survey or collaboration key names the project rather than the
author — `CosmoVerse2025` resolves to a paper by Di Valentino — so a key of four
characters or more that appears in the record's title is accepted as well. Disagreement is reported as `ADS_RECORD_CONFLICT` and the entry
is never offered as a one-keypress replacement.

The report prints a summary and then clear issue blocks with the entry key,
line number, reason, matching ADS records when available, and suggested action.
It then lists `Duplicates` (two keys resolving to the same record) and
`Warnings` (such as a literal `{et al.}` author, which renders as
`(Koribalski & et al. 2020)`).

Books and conference proceedings often resolve through the reference resolver —
`Jeffreys, H. 1939, Theory of Probability` finds `1939thpr.book.....J` — so try a
run before reaching for a skip directive.

Entries that genuinely have no ADS record, such as software or unpublished notes,
can be excluded by putting a directive on the line directly above them:

```bibtex
% checkcitation: skip
@BOOK{Jeffreys1939, ...}
```

Skipped entries are still included in the `--tex` cross-check, which reports keys
cited in the `.tex` but missing from the `.bib`, and entries defined but never
cited.

With `--replace`, the tool first prints a replacement summary and asks whether
to proceed. For each candidate it can:

- use the ADS-exported BibTeX,
- accept a pasted manual BibTeX replacement,
- skip the entry.

The existing citation key is always kept. Accepted replacements create one
backup file and then update the `.bib` file atomically.

When the proposed ADS record disagrees with the local entry, the prompt shows
author, title, and year side by side, defaults to skipping, and requires typing
`replace` to confirm. A formatting refresh stays one keypress away; overwriting
an entry with a different paper does not.

## Tests

```sh
python3 test_check_ads_bib.py   # parsing, comparison, replacement safety
python3 test_review.py          # the review server's API and what it writes
node test_review.js             # the page's script and its risk gate
```

The tests are offline and use no framework. `test_review.py` stubs ADS and runs a
real server on an ephemeral port; `test_review.js` runs the page's own `<script>`
in a `vm` context against a fake document.

ADS responses are cached in `.ads_cache.json`. A record that resolved keeps for a
month, because a published record does not change; a lookup that found nothing
keeps for an hour, because it stops being nothing the moment ADS indexes the
paper. `--cache-ttl` sets the first, `--refresh-cache` ignores both.

Your own verdicts are kept too. **Checked, it is fine** on a card records the entry
in `.checked.json`, and it is not raised again for a month — on this run, the next
one, or after a restart. It is keyed to the entry's exact text, so editing the entry
withdraws the acceptance and puts it back in the queue; `↺ checked` in the **All**
view withdraws one by hand, **Clear decisions** drops them all, and undoing a
replacement withdraws the acceptance that replacement recorded. A damaged store is reported and left alone rather than
overwritten.

That file lives **beside `check_ads_bib.py`, not beside your `.bib`**, with one
section per bibliography keyed by absolute path. Reviewing a paper's `ref.bib` must
not leave an untracked file in the paper's repository, and two `ref.bib` files in
different directories never share a verdict.

## The ADS call budget

One token gets **5000 requests a day**, shared across search, export and the
reference resolver — so parallelism (`--jobs`) buys wall-clock time, not headroom.
Eight workers reach the ceiling eight times faster, not later. What buys headroom
is spending fewer requests per entry:

- **One search per entry, not three.** ADS's `identifier` field indexes bibcodes,
  DOIs and arXiv ids together, and a returned record lists all of its own. So one
  `identifier:(…) OR doi:"…"` query resolves every identifier the entry carries,
  and "the DOI and the bibcode name different papers" is read off one response by
  set membership rather than inferred from three.
- **One export request per hundred entries, not one per entry.** Every bibcode the
  file already names is exported in bulk before the check starts, and the per-entry
  path then finds it in the cache.
- **Re-runs cost nothing** inside the one-month window, which is the loop that matters
  while you work through the issues.

Measured on 10 real entries exported from ADS, cache cold: **1.1 requests per
entry**, down from 4.0. A 300-entry bibliography goes from ~1200 requests to ~330,
which is roughly 15 full runs a day instead of 4.

The bulk export is also the more correct one. The per-bibcode `GET` truncates long
author lists to ten names and a literal `et al.` — the malformed author this tool
warns about — so it both reported false `ADS_BIBTEX_MISMATCH`es against full author
lists and offered `et al.` as a replacement. Every export now goes through the
`POST` form, which returns the list in full.
