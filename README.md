# CheckCitation

Small CLI for checking whether BibTeX entries resolve on NASA ADS and match the
BibTeX currently exported by ADS.

## Setup

Create an ADS API token from your ADS account and expose it as:

```sh
export ADS_API_TOKEN="..."
```

The optional progress bar uses `tqdm`:

```sh
python3 -m pip install tqdm
```

## Usage

Check a bibliography:

```sh
./check_ads_bib.sh path/to/ref.bib
```

Review and replace problematic entries interactively:

```sh
./check_ads_bib.sh path/to/ref.bib --replace
```

Useful options:

```sh
./check_ads_bib.sh path/to/ref.bib --jobs 4
./check_ads_bib.sh path/to/ref.bib --refresh-cache
./check_ads_bib.sh path/to/ref.bib --no-cache
./check_ads_bib.sh path/to/ref.bib --no-progress
```

## What It Does

For each BibTeX entry, the tool tries to resolve an ADS record using the local
`bibcode`/`adsurl`, DOI, arXiv ID, or title plus year.

The report prints a summary and then clear issue blocks with the entry key,
line number, reason, matching ADS records when available, and suggested action.

With `--replace`, the tool first prints a replacement summary and asks whether
to proceed. For each candidate it can:

- use the ADS-exported BibTeX,
- accept a pasted manual BibTeX replacement,
- skip the entry.

The existing citation key is always kept. Accepted replacements create one
backup file and then update the `.bib` file atomically.

ADS search responses and ADS BibTeX exports are cached in `.ads_cache.json` for
24 hours by default. Use `--refresh-cache` to fetch fresh ADS responses.
