# Frontend implementation prompt: Document Library

Implement a production-quality **Documents** view in the existing frontend for the Forest Department Pilot RAG application.

## Purpose

The page gives authenticated users a quick, scalable view of documents that have already been uploaded and successfully ingested. It must work well with a few documents and with thousands. This version does not include summaries, uploads, deletion, downloads, re-indexing, or document preview.

## API

Use the existing authenticated API client and bearer token handling. Fetch:

```http
GET /documents
```

Supported query parameters:

- `search`: searches filename and inferred title
- `kind`: `pdf`, `docx`, or `txt`
- `document_type`: for example `rules`, `act`, `guidelines`, `circular`, `notification`, `order`, `procedure`, `faq`, or `document`
- `year`: a four-digit year
- `sort_by`: `updated_at`, `created_at`, `title`, `source`, or `page_count`
- `sort_order`: `asc` or `desc`
- `offset`: zero-based result offset
- `limit`: page size, default 25 and maximum 100

Response shape:

```ts
type DocumentLibraryResponse = {
  items: Array<{
    id: string;
    filename: string;
    title: string;
    kind: string;
    page_count: number | null;
    document_type: string;
    authority: string | null;
    years: string[];
    chunk_count: number;
    status: "indexed";
    ingested_at: string | null;
    created_at: string | null;
    updated_at: string | null;
  }>;
  pagination: {
    offset: number;
    limit: number;
    total: number;
    has_more: boolean;
  };
};
```

API errors use:

```ts
type ApiError = {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};
```

## Page design

Create a document-library page, not a generic analytics dashboard.

- Header: “Documents” with the total indexed-document count beneath or beside it.
- Primary control: a prominent search field with the placeholder “Search by title or filename”.
- Filters: file type, document type, and year. Include a clear-all action when any filter is active.
- Sorting: newest indexed, oldest indexed, title A–Z, and title Z–A.
- Main content: a compact, readable table on desktop and stacked rows/cards on narrow screens.

Desktop columns:

1. **Document** — inferred title as the primary text and filename as muted secondary text.
2. **Type** — file extension badge plus human-readable document category.
3. **Authority / year** — show the authority when available and compact year values.
4. **Size** — page count when available and indexed chunk count. Label chunks clearly; do not present them as pages.
5. **Indexed** — formatted `ingested_at` date.

Do not add a summary column. Do not show a status column because every returned item is already indexed.

## Data behavior

- Fetch only the current page from the server; never load the full corpus into browser memory.
- Default to `limit=25`, `offset=0`, `sort_by=updated_at`, and `sort_order=desc`.
- Debounce search by about 300 ms.
- Reset offset to zero whenever search, filters, or sorting changes.
- Keep search, filters, sorting, offset, and limit in URL query parameters so the view survives refresh and can be shared.
- Cancel or ignore stale requests when users change search or filters quickly.
- Use `pagination.total` to display the count and calculate page controls.
- Provide Previous and Next controls plus “Showing X–Y of Z documents”.
- Preserve the current rows while a subsequent page/filter request is loading, with a subtle updating indicator rather than flashing an empty table.

## States and accessibility

- Initial loading: table-shaped skeletons.
- Empty corpus: “No indexed documents yet.” Do not suggest uploading unless the existing user role and product already support it.
- No search results: “No documents match your search and filters” with a clear-filters action.
- API failure: show `error.message` and a Retry action.
- Missing title: fall back to `filename`.
- Missing authority, page count, years, or dates: use a restrained em dash; never invent values.
- Use semantic table markup on desktop, accessible labels for all controls, visible keyboard focus, and touch targets of at least 44px.
- Match the frontend’s existing design system, navigation, authentication flow, API utilities, and responsive conventions. Do not introduce a second component library.

## Acceptance criteria

- Only successfully indexed documents are shown.
- Search, filters, sorting, and pagination call the server with the documented parameters.
- The page remains usable with thousands of records.
- URL state, loading, empty, failure, and responsive states are implemented.
- No summary UI or unsupported document-management actions are introduced.
- Add focused tests for query-parameter construction, pagination boundaries, empty results, API failures, and missing optional metadata.
