import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

from chunking import count_tokens
from documents import extract_legal_identifiers
from repositories import DocumentRepository
from services.gemini import gemini_client
from temporal import applicable_date, historical_question, temporal_metadata

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


@dataclass(frozen=True)
class RetrievalPlan:
    """Bound retrieval breadth separately from the final prompt size."""

    shape: str
    candidate_count: int
    anchor_count: int
    context_count: int
    context_token_budget: int


RETRIEVAL_DEFAULTS: dict[str, tuple[int, int, int, int]] = {
    "direct": (50, 4, 5, 3_000),
    "procedure": (80, 6, 8, 5_000),
    "comparison": (100, 8, 10, 7_000),
    "overview": (120, 8, 12, 8_000),
    "temporal": (120, 8, 12, 8_000),
}


def uses_embedding_2() -> bool:
    return os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2") == "gemini-embedding-2"


def embedding_query_text(text: str) -> str:
    if not uses_embedding_2():
        return text
    return f"task: question answering | query: {text.strip()}"


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
    return normalize_embedding(gemini_client.embed(embedding_query_text(text), "RETRIEVAL_QUERY"))


def embed_queries(texts: list[str]) -> list[list[float]]:
    prepared = [embedding_query_text(text) for text in texts]
    return [normalize_embedding(values) for values in gemini_client.embed_many(prepared, "RETRIEVAL_QUERY")]


def embedding_text(chunk: dict) -> str:
    metadata = chunk.get("metadata") or {}
    title = metadata.get("title") or chunk.get("source", "")
    lines = [f"Document type: {metadata.get('document_type', 'document')}"]
    if metadata.get("authority"):
        lines.append(f"Authority: {metadata['authority']}")
    if chunk.get("section_heading"):
        lines.append(f"Section: {chunk['section_heading']}")
    identifiers = extract_legal_identifiers(
        f"{chunk.get('section_heading') or ''}\n{chunk.get('content') or ''}"
    )
    if identifiers:
        lines.append(f"Legal identifiers: {', '.join(identifiers[:20])}")
    lines.append(chunk["content"])
    body = "\n".join(lines)
    if uses_embedding_2():
        return f"title: {title} | text: {body}"
    return f"Document: {title}\n{body}"


def retrieval_plan(question: str, options: dict | None = None, top_k: int | None = None) -> RetrievalPlan:
    options = options or {}
    shape = retrieval_shape(question)
    candidates, anchors, contexts, token_budget = RETRIEVAL_DEFAULTS[shape]
    prefix = shape.upper()
    candidates = int(os.getenv(f"RAG_{prefix}_CANDIDATES", str(candidates)))
    anchors = int(os.getenv(f"RAG_{prefix}_ANCHORS", str(anchors)))
    contexts = int(os.getenv(f"RAG_{prefix}_CONTEXTS", str(contexts)))
    token_budget = int(os.getenv(f"RAG_{prefix}_CONTEXT_TOKENS", str(token_budget)))

    explicit_k = top_k if top_k is not None else options.get("top_k")
    if explicit_k is not None:
        anchors = int(explicit_k)
        contexts = int(explicit_k)
    candidates = int(options.get("candidate_count", candidates))
    contexts = int(options.get("context_count", contexts))
    token_budget = int(options.get("context_token_budget", token_budget))
    return RetrievalPlan(
        shape=shape,
        candidate_count=max(anchors, candidates),
        anchor_count=max(1, anchors),
        context_count=max(anchors, contexts),
        context_token_budget=max(256, token_budget),
    )


def retrieval_shape(question: str) -> str:
    normalized = " ".join(question.lower().split())
    if historical_question(question) or re.search(
        r"\b(?:amend(?:ed|ment)?|supersed(?:e|ed|ing)|repeal(?:ed)?|latest|current(?:ly)?|as of|in force)\b",
        normalized,
    ):
        return "temporal"
    return classify_question_shape(question)


def retrieve(
    question: str,
    top_k: int | None = None,
    repository: DocumentRepository | None = None,
    options: dict | None = None,
) -> list[dict]:
    if env_bool("RAG_LEGAL_HIERARCHY", False):
        from legal_retrieval import retrieve_legal

        return retrieve_legal(question, top_k, repository, options)
    repository = repository or DocumentRepository()
    options = options or {}
    plan = retrieval_plan(question, options, top_k)
    queries = retrieval_queries(
        question,
        enabled=bool(options.get("multi_query", env_bool("RAG_MULTI_QUERY", True))),
    )
    rows_by_id: dict[str, dict] = {}
    fusion_by_id: dict[str, dict] = {}
    query_embeddings = embed_queries(queries) if len(queries) > 1 else [embed_query(queries[0])]
    for query, query_embedding in zip(queries, query_embeddings, strict=True):
        query_rows = repository.match_chunks(query_embedding, query, plan.candidate_count)
        for query_rank, row in enumerate(query_rows):
            merge_retrieval_row(rows_by_id, fusion_by_id, row, query_rank)
    # An amendment may use the rule identifier rather than the user's wording.
    # Search for updates as well before truncating to the final context window.
    identifiers = extract_legal_identifiers(question)
    if identifiers and not historical_question(question):
        update_query = f"{question} {' '.join(identifiers[:5])} amendment supersession latest update"
        update_rows = repository.match_chunks(embed_query(update_query), update_query, plan.candidate_count)
        for update_rank, row in enumerate(update_rows):
            merge_retrieval_row(rows_by_id, fusion_by_id, row, update_rank)
    candidates = rerank_candidates(
        question,
        [
            context_from_row(row, fusion_by_id[row_id]["best_rank"], fusion_by_id[row_id])
            for row_id, row in rows_by_id.items()
        ],
    )
    minimum_score = float(options.get("min_context_score", min_context_score()))
    candidates = [candidate for candidate in candidates if candidate_strength(candidate) >= minimum_score]
    from jev_policy import rerank

    candidates = rerank(question, candidates)
    anchors = diversify_contexts(
        candidates,
        plan.anchor_count,
        max_per_source=int(options.get("max_per_source", os.getenv("RETRIEVAL_MAX_PER_SOURCE", "0"))),
        duplicate_threshold=float(options.get("duplicate_threshold", os.getenv("RETRIEVAL_DUPLICATE_THRESHOLD", "0.82"))),
    )
    expanded = expand_neighbors(
        anchors,
        candidates,
        repository,
        plan.context_count,
        enabled=bool(options.get("expand_neighbors", env_bool("RETRIEVAL_EXPAND_NEIGHBORS", True))),
    )
    return pack_contexts(expanded, plan.context_count, plan.context_token_budget)


def merge_retrieval_row(
    rows_by_id: dict[str, dict],
    fusion_by_id: dict[str, dict],
    row: dict,
    rank: int,
) -> None:
    row_id = row["id"]
    state = fusion_by_id.setdefault(row_id, {"best_rank": rank, "query_hits": 0, "rrf_score": 0.0})
    state["best_rank"] = min(state["best_rank"], rank)
    state["query_hits"] += 1
    state["rrf_score"] += 1.0 / (60 + rank + 1)
    current = rows_by_id.get(row_id)
    if current is None or float(row.get("similarity") or 0) > float(current.get("similarity") or 0):
        rows_by_id[row_id] = row


def classify_question_shape(question: str) -> str:
    """Classify only the answer depth needed; legal/temporal intent is handled elsewhere."""
    normalized = " ".join(question.lower().split())
    # Comparison takes precedence even when the wording also contains "how" or "procedure".
    if re.search(r"\b(?:compare|difference|versus|vs\.?|distinguish)\b", normalized):
        return "comparison"
    overview_patterns = (
        r"\bwhat are (?:all )?(?:the )?(?:application )?(?:provisions|rules|requirements|guidelines|conditions)\b",
        r"\b(?:give|provide|explain|summari[sz]e) (?:me )?(?:an? )?(?:overview|complete overview)\b",
        r"\b(?:overview|framework) (?:of|for|on)\b",
        r"\bhow does .+ work\b",
    )
    if any(re.search(pattern, normalized) for pattern in overview_patterns):
        return "overview"
    if re.search(r"\b(?:procedure|process|steps?|checklist)\b", normalized):
        return "procedure"
    if re.search(r"^(?:please )?(?:help me |i (?:want|need) to )?apply\b|\b(?:walk me through|guide me|applying for)\b", normalized):
        return "procedure"
    # "How much is the application fee?" is a lookup, not a workflow.
    # Preserve workflow classification for "How do I apply?" and mixed questions.
    if re.search(r"\bhow\b(?!\s+(?:much|many|long|often|old|far|soon)\b)", normalized):
        return "procedure"
    return "direct"


def is_narrow_lookup(question: str) -> bool:
    """Only clear factual lookups can avoid count-based model escalation."""
    normalized = " ".join(question.lower().split())
    if classify_question_shape(question) != "direct":
        return False
    if re.search(r"\b(?:and|why|explain|analy[sz]e|relationship|implications?|detail(?:ed)?|all)\b", normalized):
        return False
    return bool(re.match(
        r"(?:how (?:much|many|long|often|old|far|soon)\b|who\b|when\b|where\b|"
        r"what (?:is|are) (?:the )?(?:application )?(?:fee|fees|rate|amount|deadline|duration|"
        r"validity|date|authority|form|definition|meaning)\b|define\b)",
        normalized,
    ))


def retrieval_queries(question: str, *, enabled: bool = True) -> list[str]:
    """Expand broad questions into bounded evidence facets without introducing answer facts."""
    if not enabled or classify_question_shape(question) != "overview":
        return [question]
    facets = (
        "governing rule scope definitions prior approval",
        "requirements procedure application authority consent",
        "conditions consequences levies fees exceptions special cases limitations violations penalties",
    )
    max_queries = max(1, int(os.getenv("RAG_MULTI_QUERY_MAX", "4")))
    return [question, *(f"{question} {facet}" for facet in facets[: max_queries - 1])]


def context_from_row(row: dict, rank: int | None = None, fusion: dict | None = None) -> dict:
    metadata = row.get("metadata") or {}
    if fusion:
        metadata = {
            **metadata,
            "retrieval_fusion": {
                "query_hits": fusion["query_hits"],
                "best_rank": fusion["best_rank"],
                "rrf_score": round(fusion["rrf_score"], 6),
            },
        }
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
        "token_estimate": row.get("token_estimate"),
        "metadata": metadata,
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
    today = date.today()
    use_recency = not historical_question(question)

    for candidate in candidates:
        metadata = temporal_metadata(candidate)
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
        document_date = applicable_date(metadata, today)
        # A bounded preference among relevant evidence, not a replacement for
        # semantic relevance or proof that a provision has been superseded.
        recency_boost = 0.0
        if use_recency and document_date and (lexical > 0 or identifier_match > 0):
            age_years = (today - document_date).days / 365.25
            recency_boost = 0.03 / (1.0 + age_years / 5.0)
        base_score = max(0.0, min(1.0, candidate["base_score"]))
        fusion = metadata.get("retrieval_fusion") or {}
        fusion_boost = min(0.03, max(0, int(fusion.get("query_hits") or 1) - 1) * 0.01)
        noise_penalty = 0.12 if looks_like_noise(candidate.get("text") or "") else 0.0
        boost = (
            (0.04 * lexical)
            + (0.08 * identifier_match)
            + (0.03 * field_overlap)
            + (0.02 * year_match)
            + (0.01 * intent_match)
            + recency_boost
            + fusion_boost
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
                "recency_boost": round(recency_boost, 6),
                "fusion_boost": round(fusion_boost, 6),
                "applicable_date": document_date.isoformat() if document_date else None,
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
    selective = env_bool("RAG_COST_OPTIMIZATIONS", True) and env_bool("RAG_SELECTIVE_EVIDENCE", True)

    for candidate in candidates:
        if selective and any(evidence_contains(item, candidate) for item in selected):
            continue
        if max_per_source > 0 and source_counts[candidate["source"]] >= max_per_source:
            deferred.append(candidate)
            continue
        if not selective and any(text_similarity(candidate["text"], item["text"]) >= duplicate_threshold for item in selected):
            deferred.append(candidate)
            continue
        selected.append(candidate)
        source_counts[candidate["source"]] += 1
        if len(selected) >= limit:
            return selected

    for candidate in deferred:
        if selective and any(evidence_contains(item, candidate) for item in selected):
            continue
        if candidate["id"] not in {item["id"] for item in selected}:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def evidence_contains(existing: dict, candidate: dict) -> bool:
    """Only remove verbatim redundancy within a known document and section.

    Similar wording alone cannot establish equivalence of legal provisions.
    Keep provenance/date variants, including identical rules in different documents.
    """
    if not existing.get("document_id") or not existing.get("section_heading"):
        return False
    fields = ("document_id", "source", "section_heading")
    if any(existing.get(field) != candidate.get(field) for field in fields):
        return False
    left_meta, right_meta = temporal_metadata(existing), temporal_metadata(candidate)
    if any(left_meta.get(field) != right_meta.get(field) for field in ("issued_date", "effective_date")):
        return False
    left = " ".join((existing.get("text") or "").split())
    right = " ".join((candidate.get("text") or "").split())
    # Preserve case, punctuation and boundaries; 100 must not match 1000.
    return bool(right and re.search(r"(?<!\w)" + re.escape(right) + r"(?!\w)", left))


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

    anchor_limit = max(1, min(len(anchors), math.ceil(limit * 0.6)))
    pool = list(anchors)
    selective = env_bool("RAG_COST_OPTIMIZATIONS", True) and env_bool("RAG_SELECTIVE_EVIDENCE", True)
    if selective and len(pool) >= limit:
        return pool[:limit]
    seen_ids = {context["id"] for context in pool}
    for anchor in anchors[:anchor_limit]:
        revision_id = (anchor.get("metadata") or {}).get("index_revision_id")
        revision_options = {"revision_id": revision_id} if revision_id else {}
        for row in repository.neighbor_chunks(anchor["document_id"], anchor["chunk_index"], radius=1, **revision_options):
            if row["id"] in seen_ids or row["chunk_index"] == anchor["chunk_index"]:
                continue
            neighbor = context_from_row({**row, "similarity": anchor["base_score"] * 0.82}, rank=None)
            neighbor["score"] = anchor["score"] * 0.82
            neighbor["evidence_role"] = "neighbor"
            neighbor["metadata"] = {
                **neighbor["metadata"],
                "retrieval": {"expanded_from_chunk": anchor["chunk_index"]},
            }
            if selective and any(evidence_contains(item, neighbor) for item in pool):
                continue
            pool.append(neighbor)
            seen_ids.add(neighbor["id"])
            if not selective and len(pool) >= limit:
                break
        if not selective and len(pool) >= limit:
            break

    if selective:
        # All direct matches retain priority. Among adjacent passages, protect
        # qualifications before spending the remaining slots on generic context.
        neighbors = pool[len(anchors):]
        neighbors.sort(key=lambda item: (
            bool(re.search(r"\b(?:except|exception|unless|provided|notwithstanding|exempt|subject to|"
                           r"amend(?:ed|ment)?|supersed(?:ed|es)?|repeal(?:ed)?)\b", item["text"], re.I)),
            item["score"],
        ), reverse=True)
        pool = [*anchors, *neighbors]
    return pool[:limit]


def pack_contexts(contexts: list[dict], limit: int, token_budget: int) -> list[dict]:
    """Keep ranked evidence within a predictable prompt budget."""
    selected: list[dict] = []
    used_tokens = 0
    for context in contexts:
        token_estimate = int(context.get("token_estimate") or count_tokens(context.get("text") or ""))
        if selected and used_tokens + token_estimate > token_budget:
            continue
        selected.append(context)
        used_tokens += token_estimate
        if len(selected) >= limit or used_tokens >= token_budget:
            break
    return selected


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
            "citation_number": index,
            "document_id": str(ctx["document_id"]),
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
        for index, ctx in enumerate(contexts, start=1)
    ]


def cited_source_payload(answer: str, contexts: list[dict]) -> list[dict]:
    cited_numbers = {
        int(value)
        for citation in re.findall(r"\[((?:\d+\s*,\s*)*\d+)\]", answer)
        for value in re.findall(r"\d+", citation)
        if 1 <= int(value) <= len(contexts)
    }
    return [source for source in source_payload(contexts) if source["citation_number"] in cited_numbers]


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


def generate_with_gemini(
    prompt: str,
    *,
    operation: str = "answer_direct",
    system_instruction: str | None = None,
) -> str:
    return gemini_client.generate(
        prompt,
        operation=operation,
        system_instruction=system_instruction,
    )


def generate_structured_with_gemini(
    prompt: str,
    schema: dict,
    *,
    operation: str,
    system_instruction: str | None = None,
) -> dict:
    return gemini_client.generate_structured(
        prompt,
        schema,
        operation=operation,
        system_instruction=system_instruction,
    )
