import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from rag_errors import RagError
from services.document_storage import SUPPORTED_STORAGE_EXTENSIONS, document_storage
from settings import DOCS_DIR as _DOCS_DIR

SUPPORTED_EXTENSIONS = SUPPORTED_STORAGE_EXTENSIONS
DOCS_DIR = _DOCS_DIR


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_txt(path: Path) -> list[dict]:
    text = normalize_text(path.read_text(encoding="utf-8"))
    return [{"page": None, "text": text}] if text else []


def read_pdf(path: Path) -> list[dict]:
    try:
        pages = read_pdf_with_pdfplumber(path)
        if pages:
            return pages
    except Exception:
        pass

    return read_pdf_with_pypdf(path)


def read_pdf_with_pypdf(path: Path) -> list[dict]:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise RagError(f"Could not read PDF {path.name}: {exc}") from exc

    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = normalize_text(page.extract_text() or "")
        if text:
            pages.append({"page": i, "text": text})
    return pages


def read_pdf_with_pdfplumber(path: Path) -> list[dict]:
    import pdfplumber

    pages = []
    table_index = 0
    with pdfplumber.open(str(path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            blocks = []
            text = normalize_text(page.extract_text() or "")
            if text:
                blocks.append({"type": "text", "text": text})

            for table in page.extract_tables():
                table_block = table_block_from_rows(table, table_index)
                table_index += 1
                if table_block:
                    blocks.append(table_block)

            page_text = normalize_text("\n\n".join(block["text"] for block in blocks))
            if page_text:
                pages.append({"page": page_number, "text": page_text, "blocks": blocks})
    return pages


def iter_docx_blocks(document: Document):
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def clean_cell_text(text: str) -> str:
    return normalize_text(text).replace("\n", " ")


def table_block_from_rows(raw_rows: list[list[str | None]], table_index: int) -> dict | None:
    rows = []
    for raw_row in raw_rows:
        cells = [clean_cell_text(cell or "") for cell in raw_row]
        if any(cells):
            rows.append(cells)

    if not rows:
        return None

    headers = rows[0] if len(rows) > 1 else []
    data_rows = rows[1:] if headers else rows
    rendered_rows = [" | ".join(cell for cell in row) for row in rows]
    return {
        "type": "table",
        "table_index": table_index,
        "headers": headers,
        "rows": data_rows,
        "text": normalize_text("\n".join(rendered_rows)),
    }


def docx_table_block(table: Table, table_index: int) -> dict | None:
    return table_block_from_rows([[cell.text for cell in row.cells] for row in table.rows], table_index)


def read_docx(path: Path) -> list[dict]:
    try:
        document = Document(str(path))
    except Exception as exc:
        raise RagError(f"Could not read DOCX {path.name}: {exc}") from exc

    blocks = []
    table_index = 0
    for block in iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            text = normalize_text(block.text)
            if text:
                blocks.append({"type": "text", "text": text})
        elif isinstance(block, Table):
            table_block = docx_table_block(block, table_index)
            table_index += 1
            if table_block:
                blocks.append(table_block)

    text = normalize_text("\n\n".join(block["text"] for block in blocks))
    return [{"page": None, "text": text, "blocks": blocks}] if text else []


def load_documents() -> list[dict]:
    docs = []
    with document_storage().document_files() as files:
        for document_file in files:
            path = document_file.path
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            if path.suffix.lower() == ".pdf":
                pages = read_pdf(path)
            elif path.suffix.lower() == ".docx":
                pages = read_docx(path)
            else:
                pages = read_txt(path)

            if pages:
                docs.append(
                    {
                        "source": document_file.name,
                        "kind": path.suffix.lower().lstrip("."),
                        "title": infer_title(document_file.name, pages),
                        "page_count": len([p for p in pages if p["page"] is not None]) or None,
                        "pages": pages,
                    }
                )
    return docs


def infer_title(source: str, pages: list[dict]) -> str:
    for page in pages:
        for line in page["text"].splitlines():
            line = line.strip()
            if 8 <= len(line) <= 180:
                return line
    return Path(source).stem.replace("_", " ").replace("-", " ").title()
