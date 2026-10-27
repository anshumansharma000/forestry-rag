# Document library preview

The Documents tab shows **View** beside **Delete** for admins, matching the
existing permission to retrieve original files. Desktop rows and mobile cards
open the same large modal. Non-admin roles retain their existing library access.

- PDF: authenticated original-file fetch, embedded browser PDF viewer, optional
  **Open in new tab** link. The blob is explicitly typed as PDF before embedding.
- TXT: original text rendered as escaped text, preserving line breaks.
- DOCX, PPT and PPTX: paginated text from published ingestion chunks, with section
  headings and page/slide numbers when available. This does not reproduce original
  formatting or images. No external Office viewer receives the file.

Original retrieval reuses `GET /documents/{id}/download`. An optional abort signal
allows closing the preview to cancel the request. Temporary blob URLs are revoked
when the modal closes; access tokens are sent in headers, never links.

`GET /documents/{id}/preview?offset=0&limit=40` provides the indexed-text view.
It requires an actual admin bearer token, including during local auth bypass,
and records `documents.preview` audit events. It returns `document_id`, `filename`,
`sections` and `next_offset` (null on the last page). Sections contain only
`chunk_index`, `section_heading`, `page_start`, `page_end` and `content`.
The limit must be 1–100. The query fetches one extra row to detect the next page;
embeddings and staged revisions are never returned.

Missing originals, deleted documents, and absent indexed text show an error with a
retry action. Initial loading and subsequent pages have their own loading states.
Previewing does not ingest, convert, edit or delete a document.

Deploy the backend and frontend changes together. No database migration is required.
Existing deletion migration requirements are independent of preview support.
