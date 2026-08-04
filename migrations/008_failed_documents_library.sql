create index if not exists documents_failed_updated_at_idx
on documents (updated_at desc, id)
where metadata->>'ingest_status' = 'failed';
