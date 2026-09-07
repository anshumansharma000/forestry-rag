"""Conservative document dates; mentioned years are not publication dates."""

import re
from datetime import date, datetime


DATE_TOKEN = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+\d{4})"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value.strip(), flags=re.I).replace(",", "")
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    return None


def extract_temporal_metadata(text: str) -> dict:
    result = {"temporal_metadata_version": 1}
    labels = {
        "issued_date": r"(?:date of issue|issued on|dated|date)",
        "effective_date": r"(?:effective (?:from|on)|with effect from|comes? into force on|shall come into force on)",
    }
    # Only an unambiguous labelled date is accepted. References and multiple
    # competing dates stay unknown for the answer model to resolve from text.
    for field, label in labels.items():
        matches = list(re.finditer(rf"\b{label}\s*[:,-]?\s*({DATE_TOKEN})", text, re.I))
        if field == "issued_date":
            matches = [m for m in matches if not re.search(
                r"\b(amend|supersed|refer|previous|earlier|notification|order|circular)\w*\b",
                text[max(0, text.rfind('\n', 0, m.start()) + 1):m.start()], re.I
            )]
        dates = {parsed.isoformat() for m in matches if (parsed := parse_date(m.group(1)))}
        if len(dates) == 1:
            result[field] = dates.pop()
    result["amendment_references"] = [
        line.strip()[:1000] for line in text.splitlines()
        if re.search(r"\b(amend(?:s|ed|ment)?|supersed(?:e|es|ed|ing)|repeal(?:s|ed)?)\b", line, re.I)
    ][:10]
    return result


def temporal_metadata(context: dict) -> dict:
    metadata = context.get("metadata") or {}
    if metadata.get("temporal_metadata_version"):
        return metadata
    # Compatibility for existing chunks: use explicit dates in their own text,
    # never upload timestamps, filenames, or the largest mentioned year.
    return {**extract_temporal_metadata(context.get("text") or ""), **metadata}


def historical_question(question: str) -> bool:
    if re.search(r"\b(historic(?:al)?|previously|formerly|at that time|as of|as at|in|during|before|after)\b.*\b(?:19|20)\d{2}\b", question, re.I):
        return True
    without_titles = re.sub(r"\b(?:rules?|act|regulations?|code)[,\s]+(?:19|20)\d{2}\b", "", question, flags=re.I)
    return bool(re.search(r"\b(?:19|20)\d{2}\b|\b(historic(?:al)?|previously|formerly|at that time)\b", without_titles, re.I))


def applicable_date(metadata: dict, today: date) -> date | None:
    effective = parse_date(metadata.get("effective_date"))
    issued = parse_date(metadata.get("issued_date"))
    if (effective and effective > today) or (issued and issued > today):
        return None
    return effective or issued
