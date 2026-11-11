"""Breed vocabulary used to (a) skip single-breed specialty clubs for other
breeds and (b) recognise breed section headers inside catalogs.

The list is deliberately broad (AKC breeds across the 1900s plus common
historical spellings). Matching is case-insensitive on word boundaries.
"""

from __future__ import annotations

import re

TARGET_BREED_PATTERN = re.compile(r"\bdober\s*man+\b|\bdobe\b", re.I)

# Words that identify another breed in a club name or section header.
OTHER_BREEDS = [
    "affenpinscher", "afghan", "airedale", "akita", "alaskan malamute", "malamute",
    "american foxhound", "foxhound", "basenji", "basset", "beagle", "bearded collie",
    "bedlington", "belgian", "bernese", "bichon", "black and tan coonhound", "coonhound",
    "bloodhound", "border terrier", "borzoi", "russian wolfhound", "boston terrier",
    "bouvier", "boxer", "briard", "brittany", "brussels griffon", "griffon",
    "bull terrier", "bulldog", "bullmastiff", "cairn", "cardigan", "corgi",
    "cavalier", "chesapeake", "chihuahua", "chow", "clumber", "cocker", "collie",
    "curly-coated", "dachshund", "dalmatian", "dandie dinmont", "english setter",
    "english springer", "springer", "english toy spaniel", "field spaniel",
    "flat-coated", "fox terrier", "french bulldog", "german shepherd", "shepherd",
    "german shorthaired", "pointer", "golden retriever", "retriever", "gordon setter",
    "great dane", "great pyrenees", "greyhound", "harrier", "irish setter", "setter",
    "irish terrier", "irish water spaniel", "irish wolfhound", "wolfhound",
    "italian greyhound", "japanese spaniel", "japanese chin", "keeshond",
    "kerry blue", "komondor", "kuvasz", "labrador", "lakeland", "lhasa",
    "maltese", "manchester", "mastiff", "miniature pinscher", "miniature schnauzer",
    "newfoundland", "norwegian elkhound", "elkhound", "norwich", "old english sheepdog",
    "sheepdog", "otterhound", "papillon", "pekingese", "pekinese", "pomeranian",
    "poodle", "pug", "puli", "rottweiler", "saint bernard", "st. bernard",
    "st bernard", "saluki", "samoyed", "schipperke", "schnauzer", "scottish deerhound",
    "deerhound", "scottish terrier", "scottie", "sealyham", "shetland", "shih tzu",
    "siberian husky", "husky", "silky", "skye", "spaniel", "staffordshire",
    "sussex", "weimaraner", "welsh terrier", "west highland", "whippet",
    "wirehaired", "wire-haired", "yorkshire", "terrier", "hound", "toy dog",
    "toy breed", "gun dog", "sporting dog", "field trial", "obedience", "beagle",
    "coursing", "sled dog", "retriever", "setter", "pointer",
]

# Words that indicate a club holds all-breed (or multi-breed) shows.
ALL_BREED_HINTS = [
    "kennel club", "kennel association", "dog club", "dog show", "fanciers",
    "kennel council", "exposition", "fair", "agricultural", "society",
    "dog breeders", "dog fanciers", "show", "association", "club of",
]

_OTHER_RE = re.compile(r"\b(" + "|".join(sorted({re.escape(b) for b in OTHER_BREEDS}, key=len, reverse=True)) + r")s?\b", re.I)


def club_relevance(club: str | None) -> str:
    """Classify a club as ``target`` (Doberman specialty), ``skip`` (specialty
    for another breed) or ``all_breed`` (worth checking)."""
    name = club or ""
    if TARGET_BREED_PATTERN.search(name):
        return "target"
    if _OTHER_RE.search(name):
        # "Kennel Club" names occasionally include a breed word in a place
        # name; those are rare enough that we keep the specialty rule simple.
        return "skip"
    return "all_breed"


# Section headers in catalogs are breed names in caps, often plural, e.g.
# "GREAT DANES", "DOBERMAN PINSCHERS", "BOXERS". Used by the parser to stop a
# Doberman section at the next breed.
SECTION_BREEDS = sorted({b for b in OTHER_BREEDS if b not in {
    "terrier", "hound", "spaniel", "setter", "pointer", "retriever", "shepherd",
    "sheepdog", "griffon", "show", "toy dog", "toy breed", "gun dog", "sporting dog",
    "field trial", "obedience", "coursing", "sled dog", "husky", "scottie",
}} | {"german shepherd dogs", "great danes", "boxers", "rottweilers", "giant schnauzers",
      "standard schnauzers", "collies", "old english sheepdogs", "shetland sheepdogs"}, key=len, reverse=True)

SECTION_HEADER_RE = re.compile(
    r"^\W*(" + "|".join(re.escape(b) for b in SECTION_BREEDS) + r")s?\b",
    re.I,
)

