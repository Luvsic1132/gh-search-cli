# ghs — GitHub search that tells you if a repo is actually usable

Finding a repo on GitHub is easy. Finding out whether you can **actually use it** is the hard part: it has no license, or it was last pushed three years ago, or the README never mentions Windows.

`ghs` is a single-file, stdlib-only CLI that searches GitHub and then scores every hit on three things that matter before you `git clone`:

- **Is it alive?** — a 0-100 health score from push recency, archive status, license, fork status, issue backlog
- **Does it run here?** — platform hints scraped from the README (winget/scoop/.exe present, or "macOS only")
- **Does the author still stand behind it?** — an explicit "no longer maintained" in the README beats a recent commit, because authors do push docs fixes

No dependencies. No API key required. `pip` not needed.

## Install

```bash
curl -O https://raw.githubusercontent.com/<you>/gh-search-cli/main/ghs.py
python ghs.py search "json diff" --limit 5
```

Or clone and put it on your PATH:

```bash
git clone https://github.com/<you>/gh-search-cli.git
cd gh-search-cli
python ghs.py --help
```

Requires Python 3.8+. Works on Windows, macOS and Linux.

## Usage

### Search

```bash
ghs search "pdf table extract"
ghs search "epub to markdown" --lang python --min-stars 500 --pushed-after 2025-01-01
ghs search "screen annotation" --limit 20 --json
```

```
query: pdf table extract archived:false fork:false
total: 2104   showing: 3   auth: anonymous(10 req/min)

[1] camelot-dev/camelot  8.2k  Python  MIT  pushed:12d  score:84
    A Python library to extract tabular data from PDFs
    topics: csv, extraction, pdf, python, table-extraction
    active / cross-platform language / README mentions Windows
    https://github.com/camelot-dev/camelot
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--limit N` | how many results (default 8) |
| `--lang`, `--topic` | narrow by language / topic |
| `--min-stars`, `--pushed-after YYYY-MM-DD` | filter out dead or tiny projects |
| `--sort stars\|forks\|updated\|best-match` | ordering |
| `--allow-archived`, `--allow-fork` | include them (excluded by default) |
| `--no-relax` | disable automatic query relaxation when zero results come back |
| `--json` | machine-readable output |
| `--token` | override the stored token for one call |

When a query returns nothing, the tool retries with progressively looser variants (drops qualifiers, then stopwords) instead of just printing zero.

### Inspect one repo

```bash
ghs repo owner/name
ghs repo https://github.com/owner/name
```

Prints stats, license, last push, a README digest, and **install hints** pulled out of the README (npm/pip/cargo/brew/winget lines), so you can judge in five seconds whether it is worth a clone.

### Token

Anonymous search is limited to ~10 requests/min. With a token you get 30/min and 5000 core requests/hour.

```bash
ghs token set          # paste, or: ghs token set ghp_xxx
ghs token show
ghs token clear
```

Resolution order, first hit wins:

```
--token  >  $GITHUB_TOKEN  >  $GH_TOKEN  >  stored file
```

The stored file is `%APPDATA%\gh-search\token` on Windows, `$XDG_CONFIG_HOME/gh-search/token` (or `~/.config/gh-search/token`) elsewhere, with mode `0600` where the OS supports it. The token is only sent to `api.github.com`. It never leaves your machine otherwise.

## How the score works

Deliberately simple and auditable — it is a heuristic, not a verdict.

| Component | Points |
|---|---|
| Popularity | `log10(stars) * 15`, capped at 45 |
| Pushed within 90 days | +30 |
| within 365 days | +18 |
| within 730 days | +6 |
| Archived | -25 |
| Has a recognized license | +12 |
| Is a fork | -15 |

A README that says "no longer maintained" / "deprecated" / "不再维护" caps the score at 45 regardless of commit recency.

## Environment variables

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` / `GH_TOKEN` | token, if you prefer env over the stored file |
| `GHS_DENY_TOPICS` | optional comma-separated words; hits whose topics or description match are muted. Empty by default — this tool applies no editorial policy of its own |

## Known limitations

- **Keyword search only.** GitHub's code search is not exposed through this tool; it uses the repository search API.
- **The score is a heuristic.** A tidy-looking repo with a `no-license` tag may still be fine for personal use; check the license section in `ghs repo`.
- **Platform hints are regex guesses over the README.** They produce false negatives when a project supports Windows but never says so.
- **`--no-relax` matters for narrow queries.** The automatic relaxation can surface loosely-related repos if your exact phrasing does not exist.
- **Rate limits are real.** Anonymous mode is throttled to ~10 search requests/min; batch use needs a token.
- **No caching.** Every call hits the API.

## License

MIT. See [LICENSE](LICENSE).
