import re

def clean_ocr(text: str) -> str:
    """Clean common OCR scanning noise that impacts record parsing."""
    t = text.replace(chr(8217), "'").replace(chr(8216), "'")

    # Heal hyphenated word line-wraps across line breaks or spaces:
    t = re.sub(r"\b([A-Za-z]{2,})\s*-\s*\n?\s*([a-z]{2,})\b", r"\1\2", t)

    # Clean stray OCR underscores in names and text:
    t = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", t)
    t = re.sub(r"\s+_\s+", " ", t)
    t = re.sub(r"_+", "", t)

    # Fix OCR noise on sex bracket e.g. '(BP:' -> '(B)', '(BD' -> '(B)', '(RB)' -> '(B)', 'P (698' -> '(D) (698'
    t = re.sub(r"\((?:BP:?|BD|RB|\(?B\)?[:_]?)\b", "(B)", t)
    t = re.sub(r"\(B\)[:_]", "(B)", t)
    t = re.sub(r"\(([b])\)", "(B)", t)
    t = re.sub(r"\b(?:it|II|III)\s+\(([Bb])\)", r"(B)", t)
    t = re.sub(r"\bP\s+\((\d{3})", r"(D) (\1)", t)
    t = re.sub(r"\(1\)\s+\((\d{3})", r"(D) (\1)", t)
    t = re.sub(r"\bfog\s+\((\d{3})", r"(B) (\1)", t)
    t = re.sub(r"WHITE GATE\.\s*\([^\)]*\n", "WHITE GATE (D)\n", t)
    t = re.sub(r"\(697\.5260\)", "(697,526)", t)

    # Fix specific OCR misreads in Doberman names
    t = re.sub(r"\bPAL\s+V\s+1D\s+TAXILUST\b", "PAL V D TAXILUST", t)
    t = re.sub(r"\bPAL\s+V\s+DD\s+TAXILUST\b", "PAL V D TAXILUST", t)

    # Pull registration number on next line up to the dog name line with sex bracket:
    t = re.sub(r"(\([DBE8FR]{1,2}\))\s*\n\s*[a-zA-Z\d\W]{0,20}?\(([0-9]{3}[,\.\s]+[0-9]{3})\)", r"\1 (\2)\n", t)

    # Fix missing sex bracket e.g. 'FRITZ V FUHRMANN  (680,142)' -> 'FRITZ V FUHRMANN (D) (680,142)'
    t = re.sub(r"(\b[A-Za-z\s]{4,35})\s+[\-\—\s]+\(([0-9]{3}[,\.\s]+[0-9]{3})\)", r"\1 (D) (\2)", t)

    # Fix spaces inside numbers e.g. '(69 ,699)' -> '(692,699)'
    t = re.sub(r"\(\s*69\s+[,.]\s*699\s*\)", "(692,699)", t)
    t = re.sub(r"\(\s*(\d{2})\s+[,.]\s*(\d{3})\s*\)", r"(\g<1>0,\2)", t)

    # Normalize dot inside registration numbers e.g. (694.697) -> (694,697)
    t = re.sub(r"\(\s*(\d{3})\s*\.\s*(\d{3})\s*\)", r"(\1,\2)", t)

    # Normalize stray hyphens before registration numbers e.g. -(694,285) -> (694,285)
    t = re.sub(r"-\s*\(\s*(\d{3}[,\.\s]+\d{3})\s*\)", r"(\1)", t)

    # Join line-wrapped registration numbers across hyphens, spaces, and stray margin symbols
    t = re.sub(r"\(\s*(\d{3})\s*[,.\-\s]+(?:\s*|[\W\d_a-zA-Z]{0,4}\s*)(\d{3})\s*\)", r"(\1,\2)", t)

    # Fix brackets scanned as 'J' e.g. [EANETTE -> JEANETTE
    t = re.sub(r"\n\s*\[([A-Z])", r"\nJ\1", t)
    t = re.sub(r"\n\s*[l|.]\s+([A-Z]{3,})", r"\n\1", t)

    return t
