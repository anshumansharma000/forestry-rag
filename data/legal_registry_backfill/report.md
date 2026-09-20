# Dev legal registry backfill

Applied and verified on 2026-09-19T10:57:46.138565+00:00.

- 358 profiles saved: 296 proposed instrument labels and 62 unknown/secondary documents.
- 110 candidate relationships: 102 references, 2 amendments, 6 supersessions.
- 173 ambiguous source/target groups remain unresolved (706 excerpt occurrences).
- Every profile remains unreviewed; no SME approval or legal currentness is implied.
- Shared documents and chunk text/metadata unchanged; other namespaces unchanged.
- Re-running the backfill changed no rows. Old and new search RPCs both passed live read checks.
- 241 automated tests passed.

## Proposed classifications

| Classification | Count |
|---|---:|
| act | 8 |
| amendment | 10 |
| clarification | 73 |
| guideline | 109 |
| handbook | 5 |
| instruction | 69 |
| judicial | 8 |
| notification | 8 |
| rules | 6 |
| unknown | 62 |

## Review files

- [Document labels](document_labels.csv)
- [Ambiguous relationships](relationship_review_queue.csv)
- [Full draft annotations and evidence](dev_annotations.json)
- [Verification details](verification.json)

The new hierarchy is not enabled in production. Draft annotations do not participate as reviewed category matches or relationship links until reviewed. Manual, FAQ, constitutional, secondary and insufficiently identifiable documents remain available through the legacy retrieval fallback.
