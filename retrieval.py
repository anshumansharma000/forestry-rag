import math
import os
import re
from collections import Counter

from documents import extract_legal_identifiers
from repositories import DocumentRepository
from services.gemini import gemini_client

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}


def normalize_embedding(values: list[float]) -> list[float]:
    magnitude = sum(value * value for value in values) ** 0.5
    if magnitude == 0:
        return values
    return [value / magnitude for value in values]


def embed_text(text: str) -> list[float]:
    return normalize_embedding(gemini_client.embed(text, "RETRIEVAL_DOCUMENT"))


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [normalize_embedding(values) for values in gemini_client.embed_many(texts, "RETRIEVAL_DOCUMENT")]


def embed_query(text: str) -> list[float]:
    return normalize_embedding(gemini_client.embed(text, "RETRIEVAL_QUERY"))


def embedding_text(chunk: dict) -> str:
    metadata = chunk.get("metadata") or {}
    lines = [
        f"Document: {metadata.get('title') or chunk.get('source', '')}",
        f"Document type: {metadata.get('document_type', 'document')}",
    ]
    if metadata.get("authority"):
        lines.append(f"Authority: {metadata['authority']}")
    if chunk.get("section_heading"):
        lines.append(f"Section: {chunk['section_heading']}")
    identifiers = extract_legal_identifiers(
        f"{chunk.get('section_heading') or ''}\n{chunk.get('content') or ''}"
    )
    if identifiers:
        lines.append(f"Legal identifiers: {', '.join(identifiers[:20])}")
    lines.append("")
    lines.append(chunk["content"])
    return "\n".join(lines)


def retrieve(
    question: str,
    top_k: int | None = None,
    repository: DocumentRepository | None = None,
    options: dict | None = None,
) -> list[dict]:
    repository = repository or DocumentRepository()
    options = options or {}
    k = top_k or int(options.get("top_k") or os.getenv("TOP_K", "3"))
    candidate_count = max(k, int(options.get("candidate_count", os.getenv("RETRIEVAL_CANDIDATES", "40"))))
    query_embedding = embed_query(question)
    rows = repository.match_chunks(query_embedding, question, candidate_count)
    candidates = rerank_candidates(question, [context_from_row(row, rank) for rank, row in enumerate(rows)])
    minimum_score = float(options.get("min_context_score", min_context_score()))
    candidates = [candidate for candidate in candidates if candidate_strength(candidate) >= minimum_score]
    anchors = diversify_contexts(
        candidates,
        k,
        max_per_source=int(options.get("max_per_source", os.getenv("RETRIEVAL_MAX_PER_SOURCE", "0"))),
        duplicate_threshold=float(options.get("duplicate_threshold", os.getenv("RETRIEVAL_DUPLICATE_THRESHOLD", "0.82"))),
    )
    return expand_neighbors(
        anchors,
        candidates,
        repository,
        k,
        enabled=bool(options.get("expand_neighbors", env_bool("RETRIEVAL_EXPAND_NEIGHBORS", True))),
    )


def context_from_row(row: dict, rank: int | None = None) -> dict:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "source": row["source"],
        "chunk_index": row["chunk_index"],
        "chunk_type": row["chunk_type"],
        "section_heading": row["section_heading"],
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "text": row["content"],
        "metadata": row.get("metadata") or {},
        "base_score": float(row.get("similarity") or 0),
        "score": float(row.get("similarity") or 0),
        "hybrid_rank": rank,
        "evidence_role": "matched",
    }


def rerank_candidates(question: str, candidates: list[dict]) -> list[dict]:
    query_terms = meaningful_terms(question)
    query_identifiers = identifier_keys(extract_legal_identifiers(question))
    query_years = set(re.findall(r"\b(?:19|20)\d{2}\b", question))
    intent = query_intent(question)

    for candidate in candidates:
        metadata = candidate.get("metadata") or {}
        searchable = " ".join(
            [
                candidate.get("source") or "",
                metadata.get("title") or "",
                candidate.get("section_heading") or "",
                candidate.get("text") or "",
            ]
        )
        candidate_terms = meaningful_terms(searchable)
        lexical = len(query_terms & candidate_terms) / max(1, len(query_terms))
        field_terms = meaningful_terms(
            f"{candidate.get('source') or ''} {metadata.get('title') or ''} {candidate.get('section_heading') or ''}"
        )
        field_overlap = len(query_terms & field_terms) / max(1, len(query_terms))
        candidate_identifiers = identifier_keys(
            [
                *metadata.get("identifiers", []),
                *extract_legal_identifiers(f"{candidate.get('section_heading') or ''}\n{candidate.get('text') or ''}"),
            ]
        )
        identifier_match = (
            len(query_identifiers & candidate_identifiers) / len(query_identifiers) if query_identifiers else 0.0
        )
        candidate_years = set(metadata.get("years") or re.findall(r"\b(?:19|20)\d{2}\b", searchable))
        year_match = len(query_years & candidate_years) / len(query_years) if query_years else 0.0
        intent_match = 1.0 if intent and candidate.get("chunk_type") == intent else 0.0
        base_score = max(0.0, min(1.0, candidate["base_score"]))
        noise_penalty = 0.12 if looks_like_noise(candidate.get("text") or "") else 0.0
        boost = (
            (0.04 * lexical)
            + (0.08 * identifier_match)
            + (0.03 * field_overlap)
            + (0.02 * year_match)
            + (0.01 * intent_match)
        )
        rerank_score = base_score + boost - min(noise_penalty, 0.08)
        candidate["score"] = max(0.0, min(1.0, rerank_score))
        candidate["metadata"] = {
            **metadata,
            "retrieval": {
                "hybrid_score": round(candidate["base_score"], 6),
                "lexical_overlap": round(lexical, 6),
                "identifier_match": round(identifier_match, 6),
                "field_overlap": round(field_overlap, 6),
                "year_match": round(year_match, 6),
                "boost": round(boost, 6),
                "noise_penalty": noise_penalty,
            },
        }

    return sorted(
        candidates,
        key=lambda item: (
            item["score"],
            item["base_score"],
            -(item["hybrid_rank"] if item["hybrid_rank"] is not None else 10_000),
        ),
        reverse=True,
    )


def diversify_contexts(
    candidates: list[dict],
    limit: int,
    max_per_source: int | None = None,
    duplicate_threshold: float | None = None,
) -> list[dict]:
    selected = []
    source_counts: Counter[str] = Counter()
    max_per_source = int(os.getenv("RETRIEVAL_MAX_PER_SOURCE", "0")) if max_per_source is None else max_per_source
    duplicate_threshold = (
        float(os.getenv("RETRIEVAL_DUPLICATE_THRESHOLD", "0.82"))
        if duplicate_threshold is None
        else duplicate_threshold
    )
    deferred = []

    for candidate in candidates:
        if max_per_source > 0 and source_counts[candidate["source"]] >= max_per_source:
            deferred.append(candidate)
            continue
        if any(text_similarity(candidate["text"], item["text"]) >= duplicate_threshold for item in selected):
            deferred.append(candidate)
            continue
        selected.append(candidate)
        source_counts[candidate["source"]] += 1
        if len(selected) >= limit:
            return selected

    for candidate in deferred:
        if candidate["id"] not in {item["id"] for item in selected}:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def expand_neighbors(
    anchors: list[dict],
    candidates: list[dict],
    repository: DocumentRepository,
    limit: int,
    enabled: bool | None = None,
) -> list[dict]:
    enabled = env_bool("RETRIEVAL_EXPAND_NEIGHBORS", True) if enabled is None else enabled
    if not anchors or not enabled or not hasattr(repository, "neighbor_chunks"):
        return anchors[:limit]
    if len(anchors) >= limit:
        return anchors[:limit]

    anchor_limit = max(1, min(len(anchors), math.ceil(limit * 0.6)))
    pool = list(anchors)
    seen_ids = {context["id"] for context in pool}
    for anchor in anchors[:anchor_limit]:
        for row in repository.neighbor_chunks(anchor["document_id"], anchor["chunk_index"], radius=1):
            if row["id"] in seen_ids or row["chunk_index"] == anchor["chunk_index"]:
                continue
            neighbor = context_from_row({**row, "similarity": anchor["base_score"] * 0.82}, rank=None)
            neighbor["score"] = anchor["score"] * 0.82
            neighbor["evidence_role"] = "neighbor"
            neighbor["metadata"] = {
                **neighbor["metadata"],
                "retrieval": {"expanded_from_chunk": anchor["chunk_index"]},
            }
            pool.append(neighbor)
            seen_ids.add(neighbor["id"])
            if len(pool) >= limit:
                break
        if len(pool) >= limit:
            break

    return pool[:limit]


def retrieval_confidence(contexts: list[dict]) -> float:
    if not contexts:
        return 0.0
    matched = [candidate_strength(context) for context in contexts if context.get("evidence_role") == "matched"] or [
        candidate_strength(contexts[0])
    ]
    top = matched[0]
    supporting = sum(1 for score in matched[1:3] if score >= top * 0.75)
    return round(min(1.0, top + min(0.12, supporting * 0.06)), 4)


def retrieval_is_confident(contexts: list[dict]) -> bool:
    return bool(contexts) and retrieval_confidence(contexts) >= float(os.getenv("RETRIEVAL_CONFIDENCE_THRESHOLD", "0.01"))


def min_context_score() -> float:
    return float(os.getenv("RETRIEVAL_MIN_CONTEXT_SCORE", "0.0"))


def candidate_strength(context: dict) -> float:
    return max(float(context.get("score") or 0), float(context.get("base_score") or 0) * 0.5)


def meaningful_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", text.lower())
        if len(token) > 1 and token not in STOP_WORDS
    }


def identifier_keys(identifiers: list[str]) -> set[str]:
    return {re.sub(r"[^a-z0-9]+", "", identifier.lower()) for identifier in identifiers if identifier}


def query_intent(question: str) -> str | None:
    lowered = question.lower()
    if re.search(r"\b(how|process|procedure|workflow|steps?)\b", lowered):
        return "procedure"
    if re.search(r"\b(faq|question|answer)\b", lowered):
        return "faq"
    if re.search(r"\b(table|schedule|fee|rate|amount)\b", lowered):
        return "table"
    return None


def looks_like_noise(text: str) -> bool:
    dotted_lines = len(re.findall(r"[.…·]{4,}\s*\d+\s*$", text, re.MULTILINE))
    short_lines = [line for line in text.splitlines() if line.strip()]
    return dotted_lines >= 2 or (len(short_lines) >= 8 and dotted_lines >= len(short_lines) * 0.25)


def text_similarity(left: str, right: str) -> float:
    left_terms = meaningful_terms(left)
    right_terms = meaningful_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def format_source(record: dict) -> str:
    source = (record.get("metadata") or {}).get("display_source") or record["source"]
    page_start = record.get("page_start")
    page_end = record.get("page_end")
    if page_start is None:
        return source
    if page_start == page_end:
        return f"{source}, page {page_start}"
    return f"{source}, pages {page_start}-{page_end}"


def source_payload(contexts: list[dict]) -> list[dict]:
    return [
        {
            "source": ctx["source"],
            "display_source": format_source(ctx),
            "page_start": ctx.get("page_start"),
            "page_end": ctx.get("page_end"),
            "chunk_index": ctx["chunk_index"],
            "section_heading": ctx.get("section_heading"),
            "score": round(ctx["score"], 4),
            "evidence_role": ctx.get("evidence_role", "matched"),
            "text": ctx["text"],
        }
        for ctx in contexts
    ]


def chunk_row(document_id: str, chunk: dict) -> dict:
    return {
        "document_id": document_id,
        "source": chunk["source"],
        "chunk_index": chunk["chunk_index"],
        "chunk_type": chunk["chunk_type"],
        "section_heading": chunk["section_heading"],
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
        "content": chunk["content"],
        "token_estimate": chunk["token_estimate"],
        "metadata": chunk["metadata"],
        "embedding": embed_text(embedding_text(chunk)),
    }


def generate_with_gemini(prompt: str) -> str:
    return gemini_client.generate(prompt)
