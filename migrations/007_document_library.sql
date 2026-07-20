create index if not exists documents_indexed_updated_at_idx
on documents (updated_at desc, id)
where metadata->>'ingest_status' = 'indexed';

create index if not exists documents_indexed_kind_idx
on documents (kind)
where metadata->>'ingest_status' = 'indexed';

create index if not exists documents_indexed_type_idx
on documents ((metadata->>'document_type'))
where metadata->>'ingest_status' = 'indexed';
