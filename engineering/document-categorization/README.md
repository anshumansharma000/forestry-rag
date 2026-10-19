# Upload categorization

The backend accepts optional structured categorization on uploads. Existing clients
can keep sending the same requests. Ingestion adds source-backed metadata suggestions;
explicit corrections take precedence. No extra model call is made for this extraction.

## Deployment

Apply `migrations/016_document_categorization.sql` after the existing migration 013
(and migration 010 if using the legal registry). Applying 014 and 015 first is the
normal upgrade order. The migration is safe to reapply. It adds RPCs and one trigger;
it does not delete documents, regenerate embeddings, or change search functions.
Apply it before clients start sending categorization fields. Categorized uploads
check support before saving files. Older deployments can continue ordinary uploads
and ingestion without the new RPCs.

This repository contains the API, not the upload frontend. The interface should
fetch the catalog, add an optional document-type selector and metadata fields,
then use the payloads below. After ingestion, show suggestions and supporting
excerpts in an editable review panel.

## Document types and fields

`GET /documents/categories` returns stable `value`/`label` pairs for:

| Value | Label |
| --- | --- |
| `policy` | Policy |
| `constitution` | Constitution |
| `act` | Act |
| `amendment` | Amendment |
| `rules` | Rules |
| `consolidated_guidelines` | Consolidated Guidelines |
| `guidelines` | Guidelines |
| `circular` | Circular |
| `clarification` | Clarification |
| `notification` | Notification |
| `judgment_order` | Judgment / Court Order |
| `handbook` | Handbook / Compilation |
| `other` | Other / Unclassified |

An input object may contain `document_type`, `subjects` (list of strings),
`issuing_authority`, `jurisdiction`, `state`, `issued_date`, `effective_date`,
`consolidation_as_of`, and `identifiers` (list of strings). Dates use `YYYY-MM-DD`.
All fields are optional; omitted fields are retained and explicit `null` clears a
suggestion. Empty lists explicitly clear list fields. Unknown fields and invalid
category values/dates return 422 before multipart files are saved.

Subject examples are `Forest conservation / FCA`, `Forest conservancy`, and
`National Forest Policy`. Issuer and jurisdiction are independent of document type;
MoEFCC circulars and state guidelines can be displayed as groups using these fields.
Definitions remain passage content, not a primary document type.

## Multipart uploads

For `POST /documents/upload`, continue sending the `file` form field and optionally
add a text form field named `categorization`, containing JSON:

```json
{
  "document_type": "rules",
  "subjects": ["Forest conservation / FCA"],
  "issuing_authority": "Ministry of Environment, Forest and Climate Change",
  "jurisdiction": "National",
  "issued_date": "2023-01-01"
}
```

For `POST /documents/uploads`, continue sending repeated `files` fields and optionally
add `categorizations`: a JSON object keyed by the original uploaded filenames:

```json
{
  "rules.pdf": {"document_type": "rules"},
  "guidelines.pdf": {"document_type": "consolidated_guidelines"}
}
```

Every supplied key must match a file in the request. Files may be omitted from the
mapping. Each categorized result includes `categorization`; uncategorized uploads
keep their previous response shape. Filename normalization and upload limits remain
in force.

## Direct R2 uploads

Presign and PUT requests stay unchanged. Include optional categorization per file
when finalizing with `POST /documents/uploads/complete`:

```json
{
  "files": [{
    "upload_id": "3792898e-4aef-4985-a786-2980c069098f",
    "filename": "rules.pdf",
    "categorization": {"document_type": "rules"}
  }]
}
```

Queue ingestion as before; uploading metadata does not automatically enqueue a job.

## Suggestions and corrections

During ingestion the backend suggests a document type, supported subjects, issuer,
explicit dates, identifiers, and explicitly labeled jurisdiction/state where found.
Date and identifier extraction inspects all extracted pages, including OCR output.
Type suggestions prioritize the filename/title and cover. Issuer extraction examines
the first two pages. This is conservative pattern-based extraction, not guaranteed
classification: unsupported values remain absent. Referenced years are not issue
dates. Conflicting labeled dates remain unknown. Consolidation cutoffs are separate
from issue/effective dates. Relationships and supersession are not inferred from
mere references; use the existing admin legal-review endpoints for verified links.

`GET /documents/{document_id}/categorization` returns the envelope after ingestion:

```json
{
  "document_id": "00000000-0000-0000-0000-000000000001",
  "categorization": {
    "version": 1,
    "values": {"document_type": "rules", "issued_date": "2023-01-01"},
    "overrides": {"document_type": "rules"},
    "field_sources": {"document_type": "manual", "issued_date": "inferred"},
    "evidence": {"issued_date": [{"page": 1, "text": "Dated: 1 January 2023"}]},
    "review_required": true,
    "missing_fields": ["subjects", "issuing_authority", "jurisdiction", "state", "effective_date", "consolidation_as_of", "identifiers"]
  }
}
```

Title/filename evidence has `page: null`. Missing optional fields are diagnostic,
not ingestion failures. `review_required` identifies inferred fields or a missing
document type; it is not legal approval. Review by submitting accepted/corrected
fields through `PATCH /documents/{document_id}/categorization`. Submit the inferred
values unchanged to explicitly accept them. Existing uncategorized documents return
`categorization: null` and can be classified through PATCH without re-uploading or
embedding them again. New library items include the envelope, and
`GET /documents?category=rules` filters by the new type. The old `document_type`
field/filter retains its previous meaning.

All these routes require knowledge-manager or admin access. The API records an
audit event for corrections. An edit made during ingestion invalidates a stale
revision rather than overwriting the correction; retry that ingestion job.
Explicit date/issuer corrections update published chunk metadata without changing
vectors. Immutable historical revision snapshots are preserved.

## Legal registry and compatibility

When `LEGAL_REGISTRY_WRITES_ENABLED=true` and `LEGAL_REGISTRY_NAMESPACE` is configured,
upload/ingestion projects the metadata into an **unreviewed** legal-profile draft
in that namespace. The document row, categorization, and draft projection are committed
in the same database transaction. Reviewed legal profiles are never overwritten.
With the write gate disabled, metadata still saves and no registry writes occur.

The legacy instrument vocabulary is preserved: consolidated guidelines map to
`handbook`, circulars to `instruction`, and judgments/orders to `judicial`. The exact
new type remains in `document_category`. Policy, constitution, and other have the
legacy value `unknown`; they remain discoverable through collection-wide retrieval.
Legal review still uses `/admin/legal/profiles/{document_id}` and its independent
admin permission/write gate. Automatic suggestions never set `reviewed=true`.

This change does not enable `RAG_LEGAL_HIERARCHY` or alter ordinary retrieval. Existing
documents, chunk IDs, citations, and request fields remain valid. The new fields are
additive; clients that do not send categorization do not need changes. New inference
metadata is stored on subsequent ingestion; existing documents are not bulk-modified.

## Validation

Run `python -m pytest -q tests/test_document_categorization.py` for extraction and API
contracts. With Docker available, run
`RAG_POSTGRES_TESTS=1 python -m pytest -q tests/test_document_categorization_postgres.py`.
Database checks use an isolated, network-disabled disposable pgvector container and
never connect to the application's Supabase instance.
