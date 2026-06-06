import re

from retrieval import format_source, generate_with_gemini, retrieval_is_confident

INSUFFICIENT_EVIDENCE_ANSWER = (
    "The provided documents do not contain enough reliable information to answer this question."
)
UNSUPPORTED_ANSWER = (
    "The retrieved documents may contain relevant information, but I could not produce a sufficiently supported answer."
)


def format_history(messages: list[dict], max_messages: int | None = None) -> str:
    selected = messages[-max_messages:] if max_messages else messages
    lines = []
    for message in selected:
        role = message["role"].title()
        lines.append(f"{role}: {message['content']}")
    return "\n".join(lines).strip() or "No prior conversation."


def answer_with_gemini(question: str, contexts: list[dict], chat_history: list[dict] | None = None) -> str:
    if not retrieval_is_confident(contexts):
        return INSUFFICIENT_EVIDENCE_ANSWER

    source_block = "\n\n".join(
        (
            f"[{i + 1}] Source: {format_source(ctx)}"
            f"\nSection: {ctx.get('section_heading') or 'Not specified'}"
            f"\nEvidence role: {ctx.get('evidence_role', 'matched')}"
            f"\n{ctx['text']}"
        )
        for i, ctx in enumerate(contexts)
    )
    history_block = format_history(chat_history or [], max_messages=10)
    prompt = f"""You are a careful assistant for a forest department RAG app.

Use the conversation history only to understand references in the latest question, such as "that", "it", or "the same rule".
Use only the source excerpts for factual claims.
If the excerpts do not contain the answer, say that the provided documents do not contain enough information.
For rules, circulars, and amendments, mention newer or amending material when it appears in the excerpts.
Keep the answer concise.
Cite sources inline like [1] or [2].
Do not cite a source unless its excerpt directly supports the claim.

Conversation history:
{history_block}

Source excerpts:
{source_block}

Latest question: {question}
Answer:"""
    return validate_answer_citations(generate_with_gemini(prompt), len(contexts))


def validate_answer_citations(answer: str, source_count: int) -> str:
    if source_count <= 0:
        return UNSUPPORTED_ANSWER

    return re.sub(
        r"\[(\d+)\]",
        lambda match: match.group(0) if 1 <= int(match.group(1)) <= source_count else "",
        answer,
    ).strip()


def answer_is_abstention(answer: str) -> bool:
    return answer == INSUFFICIENT_EVIDENCE_ANSWER


def rewrite_question_for_retrieval(messages: list[dict], latest_message: str) -> str:
    history_block = format_history(messages, max_messages=10)
    prompt = f"""Rewrite the latest user message as a standalone search query for retrieving forest department rules,
circulars, amendments, notifications, or orders.

Do not answer the question.
Preserve document names, rule numbers, section numbers, dates, authorities, species, locations, and legal terms.
Resolve references like "that", "it", "same", "above", or "this rule" from the conversation history.
If the latest message is already standalone, return it unchanged.
Return only the rewritten search query.

Conversation history:
{history_block}

Latest user message: {latest_message}

Standalone search query:"""
    return generate_with_gemini(prompt).strip().strip('"')
