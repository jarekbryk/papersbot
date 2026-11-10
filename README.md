# PapersBot

Searches [Europe PMC](https://europepmc.org/) (which includes all of PubMed, PMC and preprint servers) for new papers on your chosen topics and publishes them as a static website on GitHub Pages, with one page and one **RSS feed per topic**. Every post shows the title, authors, journal, the abstract, and links to the DOI, PubMed and Europe PMC.

It runs daily on GitHub Actions. No server, no API keys, no third-party Python packages.

## Set up (once)

1. Create a new GitHub repository and push this folder to it (branch `main`).
2. In the repo go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
3. Go to **Actions → PapersBot → Run workflow** to do the first run (after that it runs every day at 06:17 UTC).

Your site appears at `https://<user>.github.io/<repo>/`. Each topic's feed is at `…/feeds/<slug>/feed.xml`, and `…/feeds.opml` lets you import all the feeds into a reader at once.

## Choose your topics

Edit [config.toml](config.toml). Each `[[feeds]]` block is one "bot":

```toml
[[feeds]]
slug = "tuberculosis"            # used in the URL
title = "Tuberculosis papers"
description = "Optional subtitle"
query = 'TITLE_ABS:"tuberculosis" AND SRC:MED'
```

Queries use [Europe PMC search syntax](https://europepmc.org/searchsyntax). Try a query on europepmc.org first to check you get what you expect. Some useful pieces:

| Want | Query fragment |
|---|---|
| Word in title or abstract | `TITLE_ABS:"gut microbiome"` |
| MeSH term | `MESH:"Neoplasms"` |
| PubMed records only | `SRC:MED` |
| Preprints only | `SRC:PPR` |
| Exclude preprints | `NOT SRC:PPR` |
| Open-access only | `OPEN_ACCESS:y` |
| Specific journal | `JOURNAL:"Nature"` |
| Specific author | `AUTH:"Smith J"` |

Don't add a date filter: the bot adds one itself (the last `lookback_days`) and skips papers it has already posted. Pushing a change to `config.toml` triggers a run automatically.

## Run locally

```bash
python3 papersbot.py run          # fetch new papers and build _site/
python3 -m http.server -d _site   # preview at http://localhost:8000
```

`fetch` and `build` can also be run separately.

## How it works

- `papersbot.py fetch` queries the Europe PMC REST API for each feed, drops papers already posted (matched by ID, DOI or title, so a paper indexed twice only appears once), and saves new ones to `data/<slug>.json`.
- The workflow commits `data/` back to the repo; that's the bot's memory.
- `papersbot.py build` renders `_site/` (HTML + RSS 2.0 + OPML), which the workflow deploys to GitHub Pages.

On the first run each feed gets at most `max_new_per_run` papers from the last week, so you don't get a flood.
