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

Check a bibliography:

```sh
./check_ads_bib.sh path/to/ref.bib
```

You can also call the Python script directly:

```sh
python3 check_ads_bib.py path/to/ref.bib
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
