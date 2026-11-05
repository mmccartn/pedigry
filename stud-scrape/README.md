# AKC Stud Book Archive Downloader

Inventories and downloads public OCR and provenance files for *The American Kennel Club Stud Book Register* from the Internet Archive collection `pub_american-kennel-club-stud-book-register`.

No Internet Archive account or credentials are needed. The tool uses the public Advanced Search API to discover collection items and the Metadata API to obtain each item's available files.

## Files retained

Each volume is stored under `data/volumes/<Internet Archive identifier>/`.

- `*_djvu.xml` is the primary source for future record parsing. It preserves page and word-level OCR structure.
- `*_djvu.txt` is a convenient flattened OCR text copy for search and exploratory processing. It is downloaded by default, but can be excluded.
- `*_meta.xml`, `*_scandata.xml`, `*_page_numbers.json`, and `*_files.xml` retain item, page, and file-inventory provenance.

`data/manifest.json` is the resumable plain-JSON inventory. It records the Archive identifier and metadata, remote file properties, local paths, download status, and local SHA-256 digests. A transient metadata error is retained as `metadata_request_failed` for that item, so a later inventory run can retry it without losing the rest of the manifest.

## Usage

Run commands from this directory.

```bash
python stud_book_archive.py inventory --follow-next-items
python stud_book_archive.py download
```

The public collection search omits some `printdisabled` issues. `--follow-next-items` follows each item's `next_item` metadata link to inventory those otherwise hidden issues. To extend an already inventoried public collection beyond December 1961 without re-inventorying every visible issue, start from the final public identifier:

```bash
python stud_book_archive.py inventory \
  --identifier sim_american-kennel-club-stud-book-register_1961-12_78_12 \
  --follow-next-items
```

To refresh metadata for every existing manifest record and follow every available `next_item` branch, including links absent from the initial public collection search, run:

```bash
python stud_book_archive.py follow-links
```

If a traversal is interrupted, resume after its last safely saved identifier:

```bash
python stud_book_archive.py follow-links --start-after <identifier>
```

Run only one command that writes `data/manifest.json` at a time; do not run `download`, `inventory`, `discover`, or `follow-links` concurrently.

The link chain ends in December 1968. The `discover` command probes the observed monthly identifier pattern through the Metadata API, accepts an item only when its identifier, `sim_pubid`, date, volume, issue, and DjVu XML match, and marks accepted records as `identifier_pattern` in the manifest:

```bash
python stud_book_archive.py discover --year-from 1969 --year-to 2004
```

Build and download a small test subset first:

```bash
python stud_book_archive.py inventory --year-from 1930 --year-to 1930
python stud_book_archive.py download --year-from 1930 --year-to 1930
```

Restrict an inventory or download to one known Internet Archive item:

```bash
python stud_book_archive.py inventory --identifier sim_american-kennel-club-stud-book-register_1930-01-01_47_1
python stud_book_archive.py download --identifier sim_american-kennel-club-stud-book-register_1930-01-01_47_1
```

To refresh Archive metadata immediately before downloading:

```bash
python stud_book_archive.py download --refresh-inventory
```

To keep structured XML and provenance files without flattened OCR text:

```bash
python stud_book_archive.py download --without-text
```

Downloads are validated against the Archive-reported file size, written atomically, and recorded after each file. Re-running `download` skips files whose local SHA-256 and size match the manifest. Failed files are retried on a later run. Files marked private by Internet Archive, or denied with HTTP 401/403, are recorded as `unavailable` and skipped without aborting other downloads.

Use `--delay` to increase the pause between Archive requests and `--retries` to adjust retry attempts. The defaults are a 0.5-second pause and three retries.

## Autonomous Doberman Extraction Pipeline

The extraction pipeline (`autonomous_extractor.py`) extracts official AKC Doberman Pinscher registrations across Stud Book volumes using on-demand remote scan fetches, dynamic vertical gutter segmentation, and high-resolution Tesseract OCR.

### Architecture

1. **On-Demand Single-Page Fetching:**
   Uses HTTP Range requests to inspect the remote Internet Archive `_jp2.zip` index and download *only* the specific 1–4 Doberman scan pages for an issue (~5 MB per volume) into local cache (`data/jp2/`), skipping the rest of the 150 MB volume zip.
2. **Dynamic Column Projection Profiling:**
   Instead of brittle hardcoded pixel coordinates, sums vertical pixel densities down the scan image to automatically identify the column margins and vertical white gutters between printed columns.
3. **Era Format Transitions:**
   The column slicer routes layouts based on historical AKC format transition dates:
   - `1908 – 1925`: Annual volumes (1- or 2-column wide layout under `DOGS.` and `BITCHES.` headings).
   - `1926-01 – 1933-06`: Classic dense 3-column monthlies (2 white vertical gutters).
   - `1933-07 – 1949-04`: 2-column monthlies (single center gutter).
   - `1949-05 – 1952-11`: 1-column full-page monthlies.
   - `1952-12 onwards`: Modern tabular format.
4. **OCR Text Normalization (`clean_helpers.py`):**
   - Heals words broken across line breaks with hyphens (e.g. `Keller Ken- nels` → `Keller Kennels`).
   - Rejoins line-wrapped registration numbers (e.g. `(555,-\n 710)` → `(555,710)`).
   - Strips letterpress baseline noise and stray OCR underscores (e.g. `BERTHA V_STROSSEN` → `BERTHA V STROSSEN`).
   - Cleans leading em-dashes (`\u2014`) and trailing punctuation noise from owner, breeder, and parent strings.
5. **Dual-Pass Reconciliation & Quality Flagging:**
   Runs a secondary cross-check pass against the original Internet Archive DjVu XML within section boundaries. Any valid registration dropped in the primary image pass due to faded ink or microfilm fold lines is automatically recovered. Any entry with missing registrations or incomplete parentage is cataloged in `data/review_flags.json` for human inspection, while clean entries pass through with full confidence.
6. **Multi-Core Parallel Execution:**
   Utilizes `ProcessPoolExecutor` across multiple CPU cores to process entire volume years concurrently in seconds.

### Usage

Extract Doberman registrations for a specific year or range:

```bash
# Extract full 1926 dataset using default multiprocessing
python autonomous_extractor.py --year-from 1926 --year-to 1926

# Extract full 1929 dataset with 8 parallel worker processes
python autonomous_extractor.py \
  --year-from 1929 --year-to 1929 \
  --workers 8 \
  --output data/autonomous_dobermans_1929.json \
  --flags-output data/review_flags_1929.json

# Run using Surya OCR engine (when local GGUF models are present in bin/)
python autonomous_extractor.py --year-from 1929 --year-to 1929 --ocr-engine surya
```

Outputs formatted JSON containing parsed dogs with name, registration number, sex, whelped date, color, owner, breeder, sire, dam, and volume/page/column provenance.

### Volume Breakdown & Verification Report

To print the breakdown table comparing extracted dogs against manual expectations in `template.yml`:

```bash
# Print breakdown table for a specific year
python report_counts.py --year 1929

# Print breakdown tables for all extracted years (1926-1929)
python report_counts.py --all
```

## Gemini Vision Stud Book Extractor (Multi-Breed & All Fields)

The script `gemini_studbook_extractor.py` provides vision-based transcription directly from scan images:
- **All Dog Breeds:** Transcribes all breeds on the page into unified JSON.
- **Deep Extended Ancestry:** Parses multi-generational lineage trees into structured ancestor objects.
- **Handwritten Notes:** Transcribes librarian pencil/pen notes and marginalia into a separate `handwritten_annotations` list.
- **Supplementary Sections:** Extracts Changes of Ownership / Transfers, Corrections, and Cancellations.
- **Adaptive Image Compression:** Auto-downscales large page scans to max 2200px width with quality JPEG compression to eliminate network timeouts.

### Setup & Usage

1. Create a `.env` file in the project root:
   ```env
   GEMINI_API_KEY=your_key_here
   ```
2. Run extraction:
   ```bash
   # Process a range of pages in a volume with per-page caching
   python gemini_studbook_extractor.py --year 1929 --issue 1 --leaf-start 40 --leaf-end 41

   # Send complete full pages without column slicing
   python gemini_studbook_extractor.py --year 1929 --issue 1 --full-page --leaf-start 40 --leaf-end 41

   # Extract from an individual image file
   python gemini_studbook_extractor.py --image path/to/scan.png
   ```

## Standalone On-Demand JP2 Page Downloader

The tool `download_jp2_pages.py` downloads specific individual pages or full page ranges from Internet Archive `_jp2.zip` archives over HTTP Range requests without downloading entire volume zips:

```bash
# Download a single leaf
python download_jp2_pages.py --year 1929 --issue 1 --leaf 40

# Download a range of leaves
python download_jp2_pages.py --year 1929 --issue 1 --range 3 65

# Download all primary registration pages in the volume
python download_jp2_pages.py --year 1929 --issue 1 --all-registrations

# Download only leaves containing a specific breed
python download_jp2_pages.py --year 1929 --issue 1 --breed "Doberman"
```

## Keyboard-First Review GUI

The review tool (`review_gui.py`) is a lightweight local web application designed to review and correct flagged entries with high speed.

### Features

- **Side-by-Side Verification:** Places a high-resolution cropped physical snippet of the dog's entry from the printed column scan side-by-side with the editable fields.
- **Flag Diagnostics:** Displays the exact reasons why the record was flagged (e.g. `missing_birth_date`, `missing_dam`, `faded_registration`).
- **Keyboard-First Controls:**
  - `Tab` / `Shift+Tab`: Cycle through form fields.
  - `PageDown` / `Alt+Right`: Save changes and automatically advance to the next flagged dog.
  - `PageUp` / `Alt+Left`: Return to the previous dog.
  - `[+]` / `[-]` / `[R]`: Zoom scan snippet in, out, or reset.
- **Auto-Persistence:** Automatically updates `data/autonomous_dobermans_<year>.json` and marks the flag resolved in `data/review_flags_<year>.json` upon saving.

### Launching the Review GUI

```bash
# Launch review GUI for 1926 (defaults to Flagged Only mode)
python review_gui.py --year 1926

# Launch review GUI in "All Dogs" mode (to browse/verify all 644 dogs sequentially)
python review_gui.py --year 1926 --all

# Launch on a specific port
python review_gui.py --year 1928 --port 8088
```

You can toggle between **"Flagged Only"** and **"All Dogs (Full Registry)"** anytime via the dropdown in the header bar.
