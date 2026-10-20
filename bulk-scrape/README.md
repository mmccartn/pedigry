# Bulk AKC registration collector

Collects Doberman records from known AKC registration-number candidates in safe, resumable batches. It is separate from the recursive name-based pedigree scraper, but writes accepted records into the same version-two `dogs.json` schema.

## What it accepts

```text
AKC registration number: authoritative identity key
AKC returned breed:      required validation
DobeQuest name:          provenance only; not a rejection criterion
```

A DobeQuest candidate number is accepted when MyAKC returns the same registration number and a Doberman Pinscher. The returned AKC name becomes the saved canonical AKC identity even if the DobeQuest name differs.

## Setup

The tool reuses the dependencies and MyAKC authentication cache from `../akc-scrape/`.

```bash
cd ../akc-scrape
python -m pip install -r requirements.txt
python -m playwright install chromium
export AKC_USERNAME="your-MyAKC-email"
export AKC_PASSWORD="your-MyAKC-password"
```

## Examples

Use DobeQuest as a source of candidate registration numbers and save into a shared corpus dataset:

```bash
python akc_bulk_registration_scraper.py \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --output ../akc-scrape/dogs.json \
  --downloads-dir ../akc-scrape/downloads \
  --batch-size 10 \
  --headed
```

Use a newline-delimited list of registration numbers:

```bash
python akc_bulk_registration_scraper.py \
  --numbers registration_numbers.txt \
  --output ../akc-scrape/dogs.json \
  --downloads-dir ../akc-scrape/downloads \
  --batch-size 10
```

Use the coherent experimental HTTP optimization path with automatic browser fallback:

```bash
python akc_bulk_registration_scraper.py \
  --numbers registration_numbers.txt \
  --output ../akc-scrape/dogs.json \
  --downloads-dir ../akc-scrape/downloads \
  --http-optimization
```

`--http-optimization` keeps browser login and MFA intact while using the authenticated request context for registration lookup/Add Dog, one batch table-key discovery request, Dog Information metadata, batch form creation, PDF report generation, and batch cleanup. Every direct step validates its response and falls back to the established browser workflow if MyAKC returns unexpected state or HTML.

Record browser traffic across the Manage Dogs page and all popup windows:

```bash
python akc_bulk_registration_scraper.py \
  --numbers registration_numbers.txt \
  --record-har ../akc-scrape/step-artifacts/net-recordings/bulk-test.har \
  --headed
```

`--record-har` records the entire Playwright browser context, including popup pages such as Add Dog and Create Entry Forms. HAR files may contain authenticated session material; keep them local and do not commit them.

Retry prior transient failures such as form timeouts:

```bash
python akc_bulk_registration_scraper.py \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --output ../akc-scrape/dogs.json \
  --downloads-dir ../akc-scrape/downloads \
  --retry-failed
```

## Batch safety

The tool is intended for a dedicated MyAKC bulk-collection account. After each batch finishes, it uses MyAKC's `All My Dogs` selection plus `Remove Dog of Interest from List` action to clear the table in one operation. Do not use this tool against an account whose Dogs of Interest list you need to preserve.

For each batch, it validates all added dog records first, then opens one shared Create Entry Forms chooser containing the validated batch. It selects each dog in that same chooser and generates one PDF report per dog before clearing the table.

Candidates are processed in reverse lexical registration-number order by default. This prioritizes newer/higher `W*` series before older prefixes such as `A*`, which is more useful when early historical numbers are absent from the online MyAKC database.

Each candidate is checkpointed in `registration_manifest.json` with statuses such as:

```text
pending
added_to_batch
already_collected
saved
not_found
number_mismatch
breed_mismatch
form_timeout
```

Existing records and saved PDF registration numbers are skipped in memory without being inserted into the manifest. The manifest is lazy: it receives an entry only when a candidate actually begins processing or reaches a result status. When `--manifest` is omitted, its default is derived from `--output` using a `.bulk-manifest.json` suffix, so independent test datasets do not share prior statuses. Each accepted dog still generates one official form PDF; batching reduces Add Dog and Dog of Interest list churn, not the unavoidable one-PDF-per-dog form submissions.

## Journal checkpoints and clean stop

Each saved dog is appended immediately to a JSONL journal beside the output dataset. The default journal path is the output path with a `.jsonl` suffix. The full `dogs.json` snapshot is compacted every 100 saved dogs by default and at normal shutdown.

```bash
python akc_bulk_registration_scraper.py \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --output ../akc-scrape/dogs.json \
  --checkpoint-every 250
```

Press `Ctrl+C` once for a graceful bulk stop. The collector preserves the already durable journal, attempts the dedicated-account table cleanup, closes MyAKC resources, and exits with status 130. The journal replays on the next run; a second interrupt may terminate immediately.

## PDF index cache

The shared download directory stores an `.akc-pdf-index.json` metadata cache. On the first run it reads each PDF once to record its name and registration number. Later runs only reopen PDFs whose size or modification time changed, so large existing corpora do not need full `pypdf` extraction on every startup.