# Pedigree dataset compiler

Builds a compact, root-agnostic site dataset from AKC scraper output, AKC Stud Book extraction records, and DobeQuest data. It does not modify any input source files.

```bash
python build_pedigree_dataset.py \
  --akc-data "../akc-scrape/dogs.json" \
  --studbook-data "../stud-scrape/data/autonomous_dobermans_*.json" \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --infer-sex \
  --output ../pedigry/public/data/pedigree_dataset.json
```

The output contains a `nodes` map keyed by stable AKC (`akc:<number>`) or DobeQuest (`dobequest:<id>`) IDs. Each node contains only `name`, `titles`, `registration_number`, `dobequest_id`, `birth_date`, `registration_type`, `sex`, `sire`, `dam`, and `primary_source`.

### Features & Multi-Source Precedence

- **Authoritative Provenance:**
  - Modern AKC Scraper records provide high-resolution form metadata.
  - Official AKC Stud Book records (`--studbook-data`) provide verified historical registrations, whelped dates, and verified sire/dam links for early generations.
  - DobeQuest provides foundation profile provenance when official AKC records are unavailable.
- **Fast Pre-Indexed Lookups:**
  - Pre-indexes DobeQuest by normalized AKC registration number, eliminating linear search bottlenecks.
- **Rich Birth Dates:**
  - Retains authentic whelped dates from both Stud Book records and DobeQuest profiles (`dateOfBirth` / `whelpDate`).
- **Flexible Root Selection (`--root-dog`):**
  - Compiles either the full root-agnostic graph or only the reachable ancestors of any named root dog across AKC, Stud Book, or DobeQuest records:

```bash
python build_pedigree_dataset.py \
  --akc-data "../akc-scrape/dogs.json" \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --root-dog "Riviera's Some Like It Hot" \
  --infer-sex \
  --output riviera_ancestors.json
```

AKC records are used as seeds. The compiler follows AKC parent links when available and expands through DobeQuest sire/dam IDs when an AKC branch ends. `--infer-sex` fills only missing sex values with an unambiguous inferred role: a dog used solely as a sire is marked Male, and a dog used solely as a dam is marked Female. Output JSON is compact/minified for static-site delivery.
