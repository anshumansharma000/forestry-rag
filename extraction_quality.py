"""Non-authoritative text corruption diagnostics, including valid non-Latin text."""

import re

VERSION = "extraction-diagnostics-v1"


def page_diagnostics(page):
    text = page.get("text") or ""
    visible = [c for c in text if not c.isspace()]
    replacement = text.count("\ufffd")
    control = sum(ord(c) < 32 and c not in "\n\r\t" for c in text)
    flags = []
    if replacement >= 3 and replacement / max(1, len(visible)) > 0.01:
        flags.append("replacement_characters")
    if control >= 3:
        flags.append("control_characters")
    if re.search(r"(.)\1{39,}", text) and not re.search(r"[.…·_\-=]{40,}", text):
        flags.append("repeated_glyphs")
    return {"page": page.get("page"), "flags": flags, "review_required": bool(flags), "method": page.get("extraction_method", "unknown")}


def document_diagnostics(pages):
    return {
        "version": VERSION,
        "pages": [page_diagnostics(p) for p in pages],
        "scope": "Heuristic text diagnostics only; inspect original pages and table reading order.",
    }
