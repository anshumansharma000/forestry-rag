import os
import re

import tiktoken

from documents import normalize_text
from settings import env_int

TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
FAQ_PROFILE = "faq"
PROCEDURE_PROFILE = "procedure"
SECTION_PROFILE = "section"


def count_tokens(text: str) -> int:
    return len(TOKEN_ENCODING.encode(text))


def document_profile(doc: dict) -> str:
    sample = " ".join(page["text"][:1500] for page in doc["pages"][:2])
    source_title = f"{doc['source']} {doc['title']}".lower()
    sample_lower = sample.lower()
    haystack = f"{source_title} {sample_lower}"
    if "frequently asked" in haystack or re.search(r"\bfaqs?\b", haystack):
        return FAQ_PROFILE
    if re.search(r"\b(procedure|process|workflow|step-wise|stepwise)\b", source_title):
        return PROCEDURE_PROFILE
    if re.search(r"^\s*(process|procedure|workflow|steps)\b", sample_lower):
        return PROCEDURE_PROFILE
    return SECTION_PROFILE


def chunk_settings(profile: str, max_tokens: int | None, overlap_tokens: int | None) -> tuple[int, int]:
    if profile == FAQ_PROFILE:
        default_tokens = env_int("FAQ_CHUNK_TOKENS", 650)
        default_overlap = env_int("FAQ_CHUNK_OVERLAP_TOKENS", 80)
    elif profile == PROCEDURE_PROFILE:
        default_tokens = env_int("PROCEDURE_CHUNK_TOKENS", 720)
        default_overlap = env_int("PROCEDURE_CHUNK_OVERLAP_TOKENS", 160)
    else:
        default_tokens = env_int("CHUNK_TOKENS", env_int("CHUNK_SIZE", 600))
        default_overlap = env_int("CHUNK_OVERLAP_TOKENS", env_int("CHUNK_OVERLAP", 100))

    return (
        max_tokens if max_tokens is not None else default_tokens,
        overlap_tokens if overlap_tokens is not None else default_overlap,
    )


def is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 140:
        return False
    if is_boilerplate_line(line):
        return False
    if re.match(r"^(Chapter|Part|Section|Rule|Schedule|Annexure|Appendix|Process|Procedure|Workflow|Steps)\b", line):
        return True
    if re.match(r"^\d+(\.\d+)*[\). -]+[A-Z]", line):
        return True
    if re.match(r"^[A-Z][A-Z0-9 &,()/-]{8,}$", line):
        return True
    return False


def is_boilerplate_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", line.strip()).upper()
    if not normalized:
        return True
    if re.match(r"^\d+$", normalized):
        return True
    boilerplate_patterns = [
        "THE GAZETTE OF INDIA",
        "GAZETTE OF INDIA",
        "EXTRAORDINARY",
        "PUBLISHED BY AUTHORITY",
        "REGD. NO.",
        "REGISTERED NO.",
        "PART II",
        "SEC. 3",
    ]
    if any(pattern in normalized for pattern in boilerplate_patterns):
        return True
    if re.match(r"^CG-[A-Z]+-[A-Z]-\d{8}-\d+$", normalized):
        return True
    if re.match(r"^\d+\s+GI/\d{4}\s*\(\d+\)$", normalized):
        return True
    return False


def clean_heading(line: str) -> str:
    heading = re.sub(r"\s+", " ", line.strip())
    heading = re.split(r"\s*\.?-\s*\(?\d+\)?.*", heading, maxsplit=1)[0]
    heading = re.split(r"\s+[–-]\s+", heading, maxsplit=1)[0]
    return heading[:140].strip(" .-")


def is_clause_start(line: str) -> bool:
    return bool(
        re.match(r"^(\d+(\.\d+)*|[a-zA-Z]\)|\([a-zA-Z0-9ivx]+\)|[IVX]+\.)\s+", line.strip())
    )


def strip_faq_question_number(line: str) -> str:
    return re.sub(r"^\s*\d+\s*[\).]\s*", "", line.strip(), count=1)


def is_numbered_faq_line(line: str) -> bool:
    return bool(re.match(r"^\s*\d+\s*[\).]\s*\S+", line.strip()))


def is_faq_question(line: str) -> bool:
    line = line.strip()
    question_text = strip_faq_question_number(line)
    if not question_text or is_boilerplate_line(question_text):
        return False
    if question_text.endswith("?"):
        return True
    return bool(
        re.match(
            r"^(who|what|when|where|why|how|which|whether|is|are|can|could|should|does|do|did|will|would)\b",
            question_text,
            re.IGNORECASE,
        )
    )


def is_answer_start(line: str) -> bool:
    return bool(re.match(r"^(ans|answer)\s*[:.：-]", line.strip(), re.IGNORECASE))


def token_chunks(text: str, max_tokens: int) -> list[str]:
    tokens = TOKEN_ENCODING.encode(text)
    if len(tokens) <= max_tokens:
        return [text]
    return [
        TOKEN_ENCODING.decode(tokens[i : i + max_tokens]).strip()
        for i in range(0, len(tokens), max_tokens)
        if TOKEN_ENCODING.decode(tokens[i : i + max_tokens]).strip()
    ]


def split_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?;:])\s+", text.strip())
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def split_subclauses(text: str) -> list[str]:
    parts = [
        part.strip()
        for part in re.split(r"\s+(?=(?:\([a-zA-Z0-9ivx]+\)|[a-zA-Z]\)|\d+\.)\s+)", text.strip())
        if part.strip()
    ]
    return parts or ([text.strip()] if text.strip() else [])


def pack_text_parts(parts: list[str], max_tokens: int) -> list[str]:
    packed = []
    current = ""
    for part in parts:
        if count_tokens(part) > max_tokens:
            if current:
                packed.append(current)
                current = ""
            packed.extend(token_chunks(part, max_tokens))
            continue
        if not current:
            current = part
        elif count_tokens(f"{current} {part}") <= max_tokens:
            current = f"{current} {part}"
        else:
            packed.append(current)
            current = part
    if current:
        packed.append(current)
    return packed


def split_long_text(text: str, max_tokens: int | None = None) -> list[str]:
    max_tokens = max_tokens or int(os.getenv("MAX_UNIT_TOKENS", "220"))
    if count_tokens(text) <= max_tokens:
        return [text]

    units = []
    current = ""
    for sentence in split_sentences(text):
        sentence_tokens = count_tokens(sentence)
        if sentence_tokens > max_tokens:
            if current:
                units.append(current)
                current = ""
            units.extend(pack_text_parts(split_subclauses(sentence), max_tokens))
            continue

        if not current:
            current = sentence
        elif count_tokens(f"{current} {sentence}") <= max_tokens:
            current = f"{current} {sentence}"
        else:
            units.append(current)
            current = sentence
    if current:
        units.append(current)
    return units


def split_with_context(context_lines: list[str], body: str, max_tokens: int) -> list[str]:
    context = normalize_text("\n".join(line for line in context_lines if line.strip()))
    body = normalize_text(body)
    if not context:
        return split_long_text(body, max_tokens)
    if not body:
        return [context]

    context_tokens = count_tokens(context)
    if context_tokens >= max_tokens * 0.75:
        return token_chunks(f"{context}\n\n{body}", max_tokens)

    body_budget = max(80, max_tokens - context_tokens - 8)
    return [normalize_text(f"{context}\n\n{piece}") for piece in split_long_text(body, body_budget)]


def table_context_lines(table: dict, active_heading: str | None) -> list[str]:
    table_number = int(table.get("table_index", 0)) + 1
    context = []
    if active_heading:
        context.append(f"Section: {active_heading}")
    context.append(f"Table {table_number}")

    headers = [header.strip() for header in table.get("headers", []) if header.strip()]
    if headers:
        context.append(f"Columns: {', '.join(headers)}")
    return context


def format_table_row(table: dict, row: list[str], row_index: int) -> str:
    headers = table.get("headers", [])
    if headers:
        values = []
        for cell_index, cell in enumerate(row):
            if not cell:
                continue
            header = (
                headers[cell_index].strip()
                if cell_index < len(headers) and headers[cell_index].strip()
                else f"Column {cell_index + 1}"
            )
            values.append(f"{header}: {cell}")
        row_text = "; ".join(values) if values else "empty row"
    else:
        cells = [cell for cell in row if cell]
        row_text = " | ".join(cells) if cells else "empty row"
    return f"Row {row_index + 1}: {row_text}"


def table_units(table: dict, page_number: int | None, active_heading: str | None, max_unit_tokens: int) -> list[dict]:
    units = []
    context = table_context_lines(table, active_heading)
    table_index = table.get("table_index", 0)
    for row_index, row in enumerate(table.get("rows", [])):
        row_text = format_table_row(table, row, row_index)
        for piece in split_with_context(context, row_text, max_unit_tokens):
            units.append(
                {
                    "type": "table",
                    "heading": active_heading,
                    "page": page_number,
                    "text": piece,
                    "table_index": table_index,
                    "row_index": row_index,
                }
            )
    return units


def faq_document_units(doc: dict, chunk_token_limit: int | None = None) -> list[dict]:
    configured_unit_tokens = env_int("FAQ_UNIT_TOKENS", env_int("MAX_UNIT_TOKENS", 260))
    max_unit_tokens = min(configured_unit_tokens, chunk_token_limit or configured_unit_tokens)
    units = []
    active_heading = None
    active_question = None
    question_page = None
    answer_lines: list[str] = []

    def flush_question() -> None:
        nonlocal active_question, question_page, answer_lines
        if not active_question:
            return

        context = []
        if active_heading:
            context.append(f"FAQ section: {active_heading}")
        context.append(f"Question: {active_question}")
        answer = "\n".join(answer_lines).strip()
        if answer and not is_answer_start(answer):
            answer = f"Answer: {answer}"

        for piece in split_with_context(context, answer, max_unit_tokens):
            units.append(
                {
                    "type": "faq",
                    "heading": active_heading or active_question,
                    "page": question_page,
                    "text": piece,
                }
            )

        active_question = None
        question_page = None
        answer_lines = []

    for page in doc["pages"]:
        raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", page["text"]) if b.strip()]
        for block in raw_blocks:
            lines = [
                line.strip()
                for line in block.splitlines()
                if line.strip() and not is_boilerplate_line(line)
            ]
            for i, line in enumerate(lines):
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                numbered_question = is_numbered_faq_line(line) and is_answer_start(next_line)
                if is_faq_question(line) or numbered_question:
                    flush_question()
                    active_question = line
                    question_page = page["page"]
                elif active_question and (not is_heading(line) or is_numbered_faq_line(line)):
                    answer_lines.append(line)
                elif is_heading(line):
                    flush_question()
                    active_heading = clean_heading(line)
                else:
                    active_heading = clean_heading(line) if is_heading(line) else active_heading

    flush_question()
    return units


def page_units(
    page_text: str,
    page_number: int | None,
    page_blocks: list[dict] | None = None,
    active_heading: str | None = None,
    max_unit_tokens: int | None = None,
) -> tuple[list[dict], str | None]:
    max_unit_tokens = max_unit_tokens or env_int("MAX_UNIT_TOKENS", 220)
    raw_blocks = page_blocks or [{"type": "text", "text": b.strip()} for b in re.split(r"\n\s*\n", page_text) if b.strip()]
    units = []

    for block in raw_blocks:
        if block.get("type") == "table":
            units.extend(table_units(block, page_number, active_heading, max_unit_tokens))
            continue

        lines = [
            line.strip()
            for line in block.get("text", "").splitlines()
            if line.strip() and not is_boilerplate_line(line)
        ]
        if not lines:
            continue
        if len(lines) == 1 and is_heading(lines[0]):
            active_heading = clean_heading(lines[0])
            units.append({"type": "heading", "heading": active_heading, "page": page_number, "text": active_heading})
            continue

        clause_buffer = []
        for line in lines:
            if is_heading(line):
                if clause_buffer:
                    text = " ".join(clause_buffer)
                    for piece in split_long_text(text, max_unit_tokens):
                        units.append({"type": "text", "heading": active_heading, "page": page_number, "text": piece})
                    clause_buffer = []
                active_heading = clean_heading(line)
                units.append({"type": "heading", "heading": active_heading, "page": page_number, "text": active_heading})
            elif is_clause_start(line) and clause_buffer:
                text = " ".join(clause_buffer)
                for piece in split_long_text(text, max_unit_tokens):
                    units.append({"type": "clause", "heading": active_heading, "page": page_number, "text": piece})
                clause_buffer = [line]
            else:
                clause_buffer.append(line)

        if clause_buffer:
            text = " ".join(clause_buffer)
            unit_type = "clause" if is_clause_start(text) else "text"
            for piece in split_long_text(text, max_unit_tokens):
                units.append({"type": unit_type, "heading": active_heading, "page": page_number, "text": piece})

    return units, active_heading


def document_units(doc: dict, profile: str, chunk_token_limit: int | None = None) -> list[dict]:
    if profile == FAQ_PROFILE:
        return faq_document_units(doc, chunk_token_limit)

    units = []
    active_heading = doc["title"] if profile == PROCEDURE_PROFILE else None
    configured_unit_tokens = (
        env_int("PROCEDURE_UNIT_TOKENS", env_int("MAX_UNIT_TOKENS", 260))
        if profile == PROCEDURE_PROFILE
        else env_int("MAX_UNIT_TOKENS", 220)
    )
    max_unit_tokens = min(configured_unit_tokens, chunk_token_limit or configured_unit_tokens)
    for page in doc["pages"]:
        page_result, active_heading = page_units(
            page["text"],
            page["page"],
            page.get("blocks"),
            active_heading=active_heading,
            max_unit_tokens=max_unit_tokens,
        )
        units.extend(page_result)
    return units


def chunk_token_count(units: list[dict]) -> int:
    return count_tokens("\n\n".join(unit["text"] for unit in units))


def overlap_units(units: list[dict], overlap_tokens: int) -> list[dict]:
    if overlap_tokens <= 0:
        return []

    selected = []
    total = 0
    for unit in reversed(units):
        unit_tokens = count_tokens(unit["text"])
        if selected and total + unit_tokens > overlap_tokens:
            break
        selected.insert(0, unit)
        total += unit_tokens
        if total >= overlap_tokens:
            break
    return selected


def fit_overlap(overlap: list[dict], next_unit_tokens: int, max_tokens: int) -> list[dict]:
    fitted = list(overlap)
    while fitted and chunk_token_count(fitted) + next_unit_tokens > max_tokens:
        fitted.pop(0)
    return fitted


def unit_pages(units: list[dict]) -> list[int]:
    return sorted({unit["page"] for unit in units if unit["page"] is not None})


def unit_heading(units: list[dict]) -> str | None:
    for unit in reversed(units):
        if unit["heading"]:
            return unit["heading"]
    return None


def unit_types(units: list[dict]) -> set[str]:
    return {unit["type"] for unit in units}


def unit_table_indexes(units: list[dict]) -> list[int]:
    return sorted({unit["table_index"] for unit in units if "table_index" in unit})


def context_unit(profile: str, heading: str | None) -> dict | None:
    if not heading or profile == FAQ_PROFILE:
        return None
    label = "Procedure" if profile == PROCEDURE_PROFILE else "Section"
    text = f"{label}: {heading}"
    return {"type": "context", "heading": heading, "page": None, "text": text}


def add_context_unit(units: list[dict], profile: str, heading: str | None, next_unit_tokens: int, max_tokens: int) -> list[dict]:
    context = context_unit(profile, heading)
    if not context:
        return units
    if any(unit["text"] == context["text"] for unit in units):
        return units

    with_context = [context, *units]
    while len(with_context) > 1 and chunk_token_count(with_context) + next_unit_tokens > max_tokens:
        with_context.pop(1)
    if chunk_token_count(with_context) + next_unit_tokens <= max_tokens:
        return with_context
    return units


def chunk_document(doc: dict, max_tokens: int | None = None, overlap_tokens: int | None = None) -> list[dict]:
    profile = document_profile(doc)
    max_tokens, overlap_tokens = chunk_settings(profile, max_tokens, overlap_tokens)

    chunks = []
    current_units: list[dict] = []

    def flush(units: list[dict]) -> None:
        if not units:
            return
        text = normalize_text("\n\n".join(unit["text"] for unit in units))
        pages = unit_pages(units)
        types = unit_types(units)
        if profile == FAQ_PROFILE:
            chunk_type = "faq"
        elif profile == PROCEDURE_PROFILE:
            chunk_type = "procedure"
        elif "table" in types and types <= {"context", "heading", "table"}:
            chunk_type = "table"
        else:
            chunk_type = "heading" if types == {"heading"} else "section"
        table_indexes = unit_table_indexes(units)
        chunks.append(
            {
                "source": doc["source"],
                "chunk_index": len(chunks),
                "chunk_type": chunk_type,
                "section_heading": unit_heading(units),
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "content": text,
                "token_estimate": count_tokens(text),
                "metadata": {
                    "kind": doc["kind"],
                    "title": doc["title"],
                    "profile": profile,
                    "unit_types": sorted(types),
                    "table_indexes": table_indexes,
                },
            }
        )

    for unit in document_units(doc, profile, max_tokens):
        current_tokens = chunk_token_count(current_units)
        unit_tokens = count_tokens(unit["text"])
        active_heading = unit_heading(current_units)
        heading_flush_ratio = 0.25 if profile == FAQ_PROFILE else 0.45
        heading_changed = (
            active_heading
            and unit["heading"]
            and unit["heading"] != active_heading
            and current_tokens >= max_tokens * heading_flush_ratio
        )

        if current_units and (current_tokens + unit_tokens > max_tokens or heading_changed):
            previous_units = current_units
            flush(previous_units)
            current_units = fit_overlap(overlap_units(previous_units, overlap_tokens), unit_tokens, max_tokens)
            current_units = add_context_unit(current_units, profile, unit_heading(previous_units), unit_tokens, max_tokens)

        current_units.append(unit)

    flush(current_units)
    return chunks
