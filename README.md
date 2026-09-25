# calibre-research

`calibre-research` is a command-line tool for improving a Calibre library through two related activities:

1. **Metadata repair**
2. **Significance research**

The goal is not to curate, filter, or delete books. Instead, the tool gathers better information
about each work and derives two deliberately separate results: a deterministic significance score
and, later, a personal read score based on the owner's reading history and preferences.

The project is intended to work well with large, messy Calibre libraries containing old ebooks, Humble Bundle acquisitions, comics, reprints, incomplete metadata, and books whose current ebook metadata does not accurately represent the original work.

## Current status

Early development.

Currently implemented:

* scanning a Calibre library with `calibredb`
* persistent SQLite storage
* separation of works and editions
* metadata completeness reporting
* configuration via `config.yaml`
* ordered Open Library and Google Books metadata providers
* persistent positive and negative provider-result caching
* non-destructive metadata repair proposals
* metadata issue tracking with provider-error semantics
* bounded retry and request-pacing controls
* command-based Google Books API-key resolution
* structured Wikidata award and nomination research
* idempotent, provenance-preserving Goodreads CSV imports
* local reading-status, rating, ownership, and shelf observations
* versioned scoring-rubric infrastructure
* initial CLI commands
* tests
* Ruff formatting/linting

Planned:

* LLM-assisted significance research
* confidence scoring and review queues
* StoryGraph imports and `personal_read_score`
* explicit synchronization of approved changes back to Calibre

## Requirements

* Python 3.11 or later
* [uv](https://docs.astral.sh/uv/)
* Calibre
* access to the `calibredb` executable

## Setup

Clone the repository:

```bash
git clone https://github.com/scole66/calibre-research.git
cd calibre-research
```

Install the project and development dependencies:

```bash
uv sync --extra dev
```

You generally do not need to manually create or activate a virtual environment. `uv` manages the project environment.

Run the CLI with:

```bash
uv run calibre-research --help
```

## Configuration

By default, `calibre-research` looks for:

```text
./config.yaml
```

in the current directory.

If no `config.yaml` exists, built-in defaults are used where possible.

An explicit config file can be supplied with:

```bash
uv run calibre-research --config /path/to/config.yaml ...
```

If an explicitly specified config file does not exist, that is an error.

Start from:

```text
config/config.example.yaml
```

For example:

```yaml
database: "~/.local/share/calibre-research/research.sqlite"

research:
  max_cost_per_run_usd: 5.00
  max_cost_per_book_usd: 0.10
  reuse_cached_evidence: true

calibre:
  library: "/Users/somebody/Calibre Library"
  executable: "/Applications/calibre.app/Contents/MacOS/calibredb"
  auto_apply_confidence: 0.98
  review_confidence: 0.80
```

On Windows, the Calibre section might instead look like:

```yaml
calibre:
  library: "C:\\Users\\somebody\\Documents\\Calibre Library"
  executable: "/mnt/c/Program Files/Calibre2/calibredb.exe"
```

These values are intentionally opaque strings:

* `calibre.executable` is the executable string passed to `subprocess`
* `calibre.library` is the library string passed directly to `calibredb --with-library`

The program does not attempt OS-specific path translation.

Command-line values override config values:

```bash
uv run calibre-research scan \
    --library "/other/library" \
    --executable "/other/path/calibredb"
```

## Initialize the database

```bash
uv run calibre-research init-db
```

The SQLite database is local runtime state and should not be committed to Git.

## Scan a Calibre library

With the library and executable configured:

```bash
uv run calibre-research scan
```

Or specify them explicitly:

```bash
uv run calibre-research scan \
    --library "/Users/somebody/Calibre Library" \
    --executable "/Applications/calibre.app/Contents/MacOS/calibredb"
```

The scanner uses Calibre's machine-readable JSON output.

### Metadata report

To see a basic metadata-completeness report:

```bash
uv run calibre-research scan --report
```

Example:

```text
Scanned 1329 Calibre records

Metadata report
  Total records:       1329
  Missing title:       0
  Missing author:      0
  Missing ISBN:        879
  Missing publisher:   192
  Missing pubdate:     346
  Missing language:    136
  With series:         282
```

This report deliberately measures **structural completeness**, not semantic correctness.

For example:

```text
author = "Unknown"
```

is currently considered present.

Detection of suspicious-but-present values belongs to the later metadata audit/research phase.

Calibre's sentinel publication date:

```text
0101-01-01T00:00:00+00:00
```

is treated as missing.

## Import Goodreads history

The local SQLite database is authoritative; Goodreads is an observation source rather than a
synchronization target. Import a standard Goodreads library export with:

```bash
uv run calibre-research import-goodreads goodreads_library_export.csv
```

The importer matches ISBN first, then normalized title and author. As a conservative fallback it
ignores only trailing numbered-series decorations such as `(The Stormlight Archive, #4)` when the
author also matches. Unmatched rows create local works, and positive `Owned Copies` values create
owned-edition records, including physical formats absent from Calibre. Ambiguous matches enter the
review queue instead of being guessed into submission.

Each distinct export is retained as an immutable import batch with its original rows. Re-importing
the same file is a no-op; importing a later export adds new source-specific observations without
erasing earlier reading statuses, ratings, ownership, or shelves. `explain` displays the accumulated
personal history, but `personal_read_score` remains unavailable until deterministic personal scoring
and series-continuity rules are implemented.

Exports imported before the numbered-series matching rule may have created duplicate works. Preview
a conservative repair plan with:

```bash
uv run calibre-research reconcile-goodreads
```

The command only proposes merging a Goodreads-created work with no edition into a uniquely matching
Calibre-backed work. It refuses to merge works with research data or ambiguous matches. After
reviewing the plan, apply the safe merges with `reconcile-goodreads --apply`. Source rows and personal
observations are reassigned to the Calibre-backed work; the redundant work is then removed.

## Other CLI commands

Show database statistics:

```bash
uv run calibre-research stats
```

Show items requiring review:

```bash
uv run calibre-research review
```

Explain currently stored information about a work:

```bash
uv run calibre-research explain "Dreamsnake"
```

The research commands are still under development.

## Development

### Run tests

Run the complete test suite:

```bash
uv run pytest
```

Verbose output:

```bash
uv run pytest -v
```

Run one test file:

```bash
uv run pytest tests/test_scoring.py
```

Run one test:

```bash
uv run pytest tests/test_scoring.py::test_name
```

### Formatting

The project uses Ruff.

Format the source tree:

```bash
uv run ruff format .
```

Check formatting without modifying files:

```bash
uv run ruff format --check .
```

### Linting

Run Ruff's linter:

```bash
uv run ruff check .
```

Automatically apply safe fixes:

```bash
uv run ruff check --fix .
```

A useful pre-commit sanity check is:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Or, while developing:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pytest
```

## Data model

The persistent research database deliberately distinguishes a **work** from an **edition**.

For example:

```text
Work:
    Dreamsnake
    Vonda N. McIntyre
    original publication: 1978

Edition:
    later ebook edition
    publication: 2019
    ISBN: ...
```

This matters because Calibre metadata frequently describes the particular ebook edition, while the information useful for library organization may concern the original work.

The intended pipeline is:

```text
Calibre record
    |
    v
work / edition identification
    |
    +--> bibliographic research
    |
    +--> significance research
             |
             v
       persistent evidence
             |
             v
       versioned rubric
             |
             v
       derived scores
```

A central design principle is:

```text
research facts -> rubric -> score
```

Research evidence should survive changes to the scoring rubric.

Changing rubric version 1.0 to 1.1 should require rescoring, not researching the entire library again.

## Metadata research

The metadata command performs a non-destructive metadata research pass:

```bash
uv run calibre-research metadata --limit 10
```

Ordinary runs select only pending editions. An edition is complete for the configured provider
chain after a provider matches it or every provider records a confident miss. Provider failures
remain separate from completed misses and can be retried directly:

```bash
uv run calibre-research metadata --retry-errors
```

The retry pass reuses cached matches and misses, calls only providers that previously failed, and
reconciles the current issue after the provider responds. It cannot be combined with `--refresh`,
which intentionally includes completed editions and bypasses cached provider evidence.

The initial target workflow is:

```text
Calibre record
    |
    v
identify work / edition
    |
    v
query structured metadata
    |
    v
compare with current Calibre metadata
    |
    v
store evidence and proposed repairs
    |
    v
display proposal
```

Initial metadata fields of interest include:

* title
* authors
* ISBN and other identifiers
* publisher
* original publication date
* edition publication date
* language
* series
* series index

Research should initially produce **proposals only**.

Writing changes back to Calibre will be a separate, explicit operation.

Metadata providers are configured and tried in order. Google Books requires an API key when it
is enabled. Prefer resolving it through an external secret manager instead of storing it in YAML:

```yaml
metadata:
  # Listing a provider enables it. Providers are tried in this order.
  providers:
    - openlibrary
    - googlebooks

  googlebooks:
    base_url: https://www.googleapis.com/books/v1
    api_key_command:
      - op
      - read
      - "op://your-vault/calibre-research/google-books-api-key"
    # Wait indefinitely for interactive authentication by default.
    api_key_command_timeout_seconds: null
    timeout_seconds: 15.0
    max_retries: 2
    retry_wait_multiplier_seconds: 1.0
    retry_wait_max_seconds: 30.0
    requests_per_second: 2.0
```

`api_key_command` is executed directly without a shell. Its trimmed standard output is used as
the key and takes precedence over the literal `api_key` fallback. Resolved keys are excluded from
stored source URLs and provider error messages. A null `api_key_command_timeout_seconds` waits
until the command finishes or the user interrupts it; set a finite number of seconds for
unattended runs if desired. If the command fails, its standard error is included in the CLI
diagnostic while standard output is discarded.

## Significance research

The significance pipeline first preserves useful context from cached metadata without making
network requests:

```bash
uv run calibre-research research --depth significance --limit 25
uv run calibre-research next --limit 10
```

Current structured facts include Google Books descriptions, categories, average ratings and rating
counts, plus original-publication years already identified through metadata research. Descriptions
are displayed as `about` context; they do not contribute to significance.

Significance explanations are generated from structured observations that contribute to the
rubric. If none have been found, the tool says so. A low-coverage score is provisional; absent data
is not interpreted as negative evidence.

When enabled, Wikidata supplies free, credential-free award and nomination data from structured
`award received` and `nominated for` statements. Work identity must match both title and author
before an observation is accepted. Award name, year, result, source identifier, URL, and confidence
are stored independently of the rubric. Positive records contribute points only when the versioned
scorer interprets them; a missing record does not prove that no award exists.

Wikidata requests are serialized through one provider instance per run, conservatively paced, and
cached in memory to avoid refetching repeated authors or awards. The client identifies itself with
a contactable bot User-Agent, sends `maxlag=5`, honors `Retry-After` on HTTP 429/503 responses, and
uses bounded exponential backoff when the server supplies no delay. These controls are configurable
under `research.wikidata`; do not raise the request rate merely to make a bulk run finish sooner.

Provider attempts are tracked per work. A throttling response or other provider error does not
overwrite existing evidence or produce a completed score; that work is automatically returned to
the next ordinary significance run. A successful lookup or confirmed miss clears the retry state.
`--refresh` remains available when all works should be researched again.

Scores expose the state of every component. An unresearched component is `unknown`, not zero. Until
at least one component is assessed, output says `significance: not yet rated`; researched results are
shown against the assessed portion of the rubric, for example `8/25 assessed points`.

Later research will also collect information such as:

* major awards and nominations
* critical reputation
* historical significance
* relevant author significance
* reader reception
* unusual or noteworthy context

Fuzzy significance research will eventually use an LLM with web-search capabilities, but research results will be stored as structured evidence rather than treated as ephemeral model output.

Each claim should retain:

* the claim/value
* source evidence
* confidence
* research timestamp

## Separate derived scores

`significance_score` asks how acclaimed, important, or historically notable a work is.
`personal_read_score` asks how likely the owner is to value reading it now. The latter is explicitly
unavailable until personal reading-history evidence is imported; significance is not presented as
a personalized recommendation.

The significance rubric is versioned and human-editable.

Version 2.0 currently defines these dimensions:

| Component           | Maximum |
| ------------------- | ------: |
| Major awards        |      25 |
| Critical reputation |      20 |
| Author significance |      15 |
| Reader reception    |      10 |
| Historical interest |      10 |

Confidence is separate from significance.

For example:

```text
significance_score: 62
confidence: 0.96
```

and:

```text
significance_score: 62
confidence: 0.42
```

mean different things.

Sparse evidence should not automatically make an obscure book less significant. Scores are shown
against assessed dimensions rather than treating every unknown component as zero.

## Local data

Do not commit runtime databases, secrets, or machine-specific configuration.

Typical ignored files include:

```text
*.sqlite
*.sqlite3
*.db
.env
.env.*
config.yaml
```

The repository should contain code, tests, schema/migrations, example configuration, and scoring rubrics.

## Project philosophy

`calibre-research` should be conservative about changing data and liberal about preserving evidence.

It should never conclude:

* This book has a low score, therefore you should delete it.

Instead, it should answer questions such as:

* What is this book?
* Is my metadata correct?
* When was it originally published?
* Is it part of a series?
* Has it won significant awards?
* Why might this obscure book be historically or critically interesting?
* Why might I want to read it?

The intended long-term behavior is:

**research once, preserve the evidence, and reinterpret it cheaply.**
