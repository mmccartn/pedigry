# Pedigree dataset compiler

Builds a compact, root-agnostic site dataset from scraper output and DobeQuest data. It does not modify either input file.

```bash
python build_pedigree_dataset.py \
  --akc-data "../akc-scrape/dogs copy.json" \
  --dobequest-data ../akc-scrape/dobe-quest_2026.json \
  --output pedigree_dataset.json
```

The output contains a `nodes` map keyed by stable AKC or DobeQuest IDs. Each node contains only `name`, `titles`, `registration_number`, `birth_date`, `registration_type`, `sex`, `sire`, `dam`, and `primary_source`.

AKC records are used as seeds. The compiler follows AKC parent links when available and expands through DobeQuest sire/dam IDs when an AKC branch ends.
