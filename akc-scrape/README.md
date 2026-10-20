# AKC pedigree scraper

Automates collection of AKC dog pedigree data through MyAKC. It saves entry-form PDFs and structured JSON records, then follows available sire and dam relationships to build a pedigree dataset.

## Setup

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
export AKC_USERNAME="your-MyAKC-email"
export AKC_PASSWORD="your-MyAKC-password"
```

## Run

```bash
python akc_pedigree_scraper.py "Riviera's Some Like It Hot" --breed "Doberman Pinschers" --headed
```

```bash
usage: akc_pedigree_scraper.py [-h] [--breed BREED] [--output OUTPUT]
                               [--downloads-dir DOWNLOADS_DIR]
                               [--levels LEVELS]
                               [--login-timeout LOGIN_TIMEOUT]
                               [--auth-state AUTH_STATE]
                               [--dobequest-data DOBEQUEST_DATA]
                               [--dobequest-overrides DOBEQUEST_OVERRIDES]
                               [--remove-after-processing] [--retry-not-found]
                               [--prefer-local-pdfs] [--headed]
                               name

Collect AKC dog details and parent relationships through MyAKC entry forms.

positional arguments:
  name                  Starting dog's registered name

options:
  -h, --help            show this help message and exit
  --breed BREED
  --output OUTPUT
  --downloads-dir DOWNLOADS_DIR
  --levels LEVELS       Number of pedigree levels to collect; 1 collects only
                        the named dog
  --login-timeout LOGIN_TIMEOUT
                        Seconds to allow for login and MFA completion
                        (default: 300)
  --auth-state AUTH_STATE
                        Local browser session cache (default: .akc-auth-
                        state.json)
  --dobequest-data DOBEQUEST_DATA
                        Optional DobeQuest JSON for exact-name AKC-number
                        fallback
  --dobequest-overrides DOBEQUEST_OVERRIDES
                        Verified DobeQuest name-to-profile override JSON
  --remove-after-processing
                        Remove each processed Dog of Interest after its record
                        is saved
  --retry-not-found     Retry records previously marked not_found
  --prefer-local-pdfs   Reuse uniquely name-matched local PDFs before MyAKC
                        lookup
  --headed              Show the browser while it runs
```

The scraper uses the PDF form fields for `akc_number`, `name`, `breed`, `sex`, `birth_date`, `place_of_birth`, `breeder`, `sire`, and `dam`, plus `breed_variety`, `color`, `markings`, `ofa_number`, and `medical_screens` from MyAKC's Dog Information panel. `medical_screens` is a mapping of every populated field in the Medical Screens section, such as `OFA Number` or `DNA Number`. Records also store `registration_type`, `aliases`, `sire_akc_number`, `dam_akc_number`, each parent's lookup status, `collected_at`, and the PDF filename stem as `pdf_id`. Before generating a new PDF, the scraper reuses an existing PDF with the selected AKC number when its `pdf_id` is not already assigned to another JSON record. New PDFs use the compact Herding Test/Trial form with contact fields excluded. When an AKC name lookup reports no results, the scraper retries title-stripped variants from both ends of the name, which handles titles such as `CH`, `GCH`, or `RA`. Credentials must be supplied through environment variables and are never read from or stored in project files. Fresh output uses a version-two dataset wrapper with source-aware `id`, `names`, `parents`, and `sources` fields. DobeQuest is used only for exact-name AKC-number fallback and profile provenance; AKC remains the source for birthplace and core form data.

## HTTP optimization and HAR capture

Use `--record-har` to capture the entire Playwright browser context, including Add Dog and entry-form popups:

```bash
python akc_pedigree_scraper.py "Example Dog" \
  --record-har step-artifacts/net-recordings/main-test.har \
  --headed
```

Use `--http-optimization` to try direct authenticated HTTP for simple single-result Doberman name lookups and Add Dog requests. Multi-result, title-normalized, Roman-suffix, and unexpected response shapes automatically use the established browser workflow.

## Journal checkpoints

Record updates append to a JSONL journal beside the output dataset and compact into the full `dogs.json` snapshot every 100 updates by default. Use `--checkpoint-every` to change that interval and `--journal` to choose a journal path. Press `Ctrl+C` once to request a graceful stop; Git Bash browser aborts are recognized as shutdown rather than printed as tracebacks, and durable journal entries replay automatically on the next run.