"""Build reviewable annotations from a read-only corpus snapshot; apply inserts only.

No SQL execution or shared-corpus updates. All generated profiles are unreviewed.
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from documents import extract_legal_identifiers
from legal_registry import LegalProfile, LegalRegistryRepository, LegalRelationship
from temporal import extract_temporal_metadata, parse_date

# Exact instrument names, not semantic similarity. Duplicate editions stay unresolved.
INSTRUMENT_NAME = re.compile(
    r'(?:the\s+)?(?:forest\s*\(?conserva\s*tion\)?|van\s*\(?sanrakshan\s+evam\s+samvar\s*dhan\)?|'
    r'indian\s+forest|compensatory\s+afforestation\s+fund|wild\s*life\s*\(?protection\)?|'
    r'biological\s+diversity|environment(?:al)?\s*\(?protection\)?|'
    r'mines\s+and\s+minerals\s*\(?development\s+and\s+regulation\)?)'
    r'\s*(?:\(?amendment\)?\s*)?(?:act|rules|adhiniyam)[,\s]+(?:19|20)\d{2}', re.I,
)


def normalized(value):
    return re.sub(r'[^a-z0-9]', '', value.lower()).removeprefix('the')


def instrument_names(chunks):
    """Self-identification clauses establish identity; citations do not."""
    names = []
    for chunk in chunks:
        compact = re.sub(r'\s+', ' ', chunk.get('content') or '')
        for match in re.finditer(
            r'\b(?:this\s+act|these\s+rules)\s+(?:may|shall)\s+be\s+called\s+(.{5,200}?(?:19|20)\d{2})',
            compact, re.I,
        ):
            name = re.split(r'\b(?:may|shall)\s+be\s+called\s+', match.group(1), flags=re.I)[-1].strip(' .,')
            if name not in names:
                names.append(name)
    return names


def classify(document, chunks):
    title = re.sub(r'[_-]+', ' ', document.get('source', '') + ' ' + document.get('title', ''))
    opening = ' '.join((chunk.get('content') or '') for chunk in chunks[:2])
    compact = re.sub(r'\s+', ' ', opening)
    if re.search(r'\b(?:manual|FAQ|simplified version|note|status of|scenario)\b', title + ' ' + compact[:140], re.I):
        return 'unknown', 'Secondary explanatory material/manual; do not treat as a primary legal instrument.'
    letter = bool(re.search(r'\bsub\s*:|\bsir\s*[,\.]|\bmadam\s*/?sir|\bi am directed\b', compact[:6000], re.I))
    if letter:
        if re.search(r'appointed date|commencement|come into force', title + ' ' + compact[:1000], re.I):
            return 'notification', 'Commencement notification/covering correspondence; inspect attached operative notification.'
        if re.search(r'clarif', title + ' ' + compact[:1000], re.I):
            return 'clarification', 'Ministry correspondence and clarification subject.'
        if re.search(r'guideline', title + ' ' + compact[:1000], re.I):
            return 'guideline', 'Ministry correspondence and guideline subject.'
        return 'instruction', 'Administrative correspondence; statutory authority requires review.'
    if re.search(r'S\s*U\s*P\s*R\s*E\s*M\s*E\s+C\s*O\s*U\s*R\s*T\s+O\s*F\s+I\s*N\s*D\s*I\s*A', compact[:700], re.I):
        return 'judicial', 'Court heading in opening excerpt; operative status still requires review.'
    if re.search(r'^(?:docs\s+)?(?:consolidated guidelines|handbook)', title, re.I):
        return 'handbook', 'Handbook cover/title.'
    if re.search(r'^Constitution of India', title, re.I):
        return 'unknown', 'Constitution compilation; not a standalone amendment. Outside the current instrument taxonomy.'
    names = instrument_names(chunks)
    if len({normalized(name) for name in names}) > 1:
        return 'handbook', 'Compilation containing multiple instruments; document label is not the authority of each excerpt.'
    if names:
        if re.search(r'\bamendment\b', names[0], re.I):
            return 'amendment', 'Instrument self-identification clause: ' + names[0]
        return ('rules' if re.search(r'\brules\b', names[0], re.I) else 'act'), 'Instrument self-identification clause: ' + names[0]
    for heading in [re.sub(r'[_]+', ' ', document.get('source', '')), document.get('title', '')]:
        match = INSTRUMENT_NAME.match(heading)
        if match and not re.search(r' - |submission|guideline|clarification|appointed', heading, re.I):
            return ('rules' if re.search(r'\brules\b', match.group(), re.I) else 'act'), 'Primary instrument title; inspect edition.'
    if re.search(r'\bnotification\b', compact[:300], re.I):
        return 'notification', 'Notification heading; citations do not establish that this document is an Act.'
    for kind, pattern in [('clarification', 'clarification'), ('guideline', 'guideline'), ('notification', 'notification')]:
        if re.search(pattern, title, re.I):
            return kind, 'Title-based candidate; inspect original document.'
    return 'unknown', 'Insufficient evidence for a reliable primary-instrument classification.'


def prepare(documents, chunks):
    grouped = defaultdict(list)
    for chunk in chunks:
        grouped[chunk['document_id']].append(chunk)
    for values in grouped.values():
        values.sort(key=lambda chunk: chunk['chunk_index'])
    entries = []
    targets = defaultdict(set)
    for document in documents:
        excerpts = grouped[document['id']]
        kind, reason = classify(document, excerpts)
        opening = '\n'.join(chunk['content'] for chunk in excerpts[:2])
        # Restrict issue dates to the header, before cited correspondence in the body.
        header = re.split(r'\b(?:sub\s*:|sir\s*,|madam\s*/?sir|i am directed)', opening[:2000], maxsplit=1, flags=re.I)[0]
        dates = extract_temporal_metadata(header)
        profile = LegalProfile(
            instrument_type=kind, issued_date=parse_date(dates.get('issued_date')),
            effective_date=parse_date(dates.get('effective_date')),
            identifiers=extract_legal_identifiers(opening)[:50],
            notes=reason + ' Automated draft, not SME reviewed. No currentness or supersession conclusion.',
        )
        if kind in {'act', 'rules', 'amendment'}:
            # Only register the instrument named by the opening heading, not every law it cites.
            names = instrument_names(excerpts)
            if not names:
                for heading in [re.sub(r'[_]+', ' ', document.get('source', '')), document.get('title', '')]:
                    match = INSTRUMENT_NAME.match(heading)
                    if match:
                        names = [match.group()]
                        break
            for name in names:
                match = INSTRUMENT_NAME.fullmatch(name)
                if match:
                    targets[normalized(match.group())].add(document['id'])
        entries.append({'document_id': document['id'], 'source': document['source'],
                        'profile': profile.model_dump(mode='json'), 'chunks_examined': len(excerpts)})
    ambiguous = []
    for entry in entries:
        links = {}
        for chunk in grouped[entry['document_id']]:
            for match in INSTRUMENT_NAME.finditer(chunk['content']):
                target_ids = targets.get(normalized(match.group()), set())
                if entry['document_id'] in target_ids:
                    continue  # A document's own title does not establish a link to another edition.
                if len(target_ids) > 1:
                    ambiguous.append({'source_document_id': entry['document_id'], 'reference': match.group(),
                                      'target_candidates': sorted(target_ids), 'chunk_index': chunk['chunk_index']})
                    continue
                if not target_ids:
                    continue
                target = next(iter(target_ids))
                evidence = chunk['content'][max(0, match.start()-160):match.end()+260]
                relation = 'refers_to'
                prefix = chunk['content'][max(0, match.start()-50):match.start()]
                if re.search(r'\b(?:further to amend|in supersession of)\s*$', prefix, re.I):
                    relation = 'amends' if 'amend' in prefix.lower() else 'supersedes'
                link = LegalRelationship(
                    target_document_id=target, relation=relation,
                    source_provision=f"Chunk {chunk['chunk_index']}, pages {chunk.get('page_start')}-{chunk.get('page_end')}; "
                                     f"{chunk.get('section_heading') or 'heading unspecified'}"[:500],
                    target_provision=match.group()[:500], evidence=evidence[:2000],
                )
                links.setdefault((target, relation), link.model_dump(mode='json'))
        entry['profile']['relationships'] = list(links.values())[:50]
        LegalProfile.model_validate(entry['profile'])
    unresolved = Counter(item['source_document_id'] for item in ambiguous)
    for entry in entries:
        if unresolved[entry['document_id']]:
            entry['profile']['notes'] += (
                f" {unresolved[entry['document_id']]} references have multiple target editions and remain unresolved; "
                "see the backfill review queue."
            )
    return {'namespace': 'dev', 'status': 'prepared_not_applied', 'reviewed': False,
            'documents': entries, 'ambiguous_references': ambiguous,
            'summary': {'documents': len(entries), 'chunks_examined': len(chunks),
                        'labels': dict(Counter(e['profile']['instrument_type'] for e in entries)),
                        'candidate_relationships': sum(len(e['profile']['relationships']) for e in entries),
                        'ambiguous_references': len(ambiguous)}}


def apply(manifest, repository=None):
    if manifest.get('namespace') != 'dev':
        raise ValueError('This backfill only supports the dev namespace.')
    repository = repository or LegalRegistryRepository()
    if repository.namespace != 'dev':
        raise ValueError('Set LEGAL_REGISTRY_NAMESPACE=dev; production backfill is not supported.')
    profiles = repository.profiles()  # Preflight before writes: fails if migration is absent.
    rows = []
    for entry in manifest['documents']:
        profile = LegalProfile.model_validate(entry['profile'])
        if profile.reviewed:
            raise ValueError('Automated backfill cannot mark profiles reviewed.')
        if entry['document_id'] not in profiles:
            rows.append({'namespace': 'dev', 'document_id': entry['document_id'],
                         'profile': profile.model_dump(mode='json'), 'reviewer_id': None})
    for offset in range(0, len(rows), 50):
        # Race-safe insert-only: never overwrite a profile reviewed/created since the preflight.
        repository.client.table('legal_document_profiles').upsert(
            rows[offset:offset+50], on_conflict='namespace,document_id', ignore_duplicates=True,
        ).execute()
    actual = repository.profiles()
    missing = [entry['document_id'] for entry in manifest['documents'] if entry['document_id'] not in actual]
    if missing:
        raise RuntimeError(f'Backfill verification failed: {len(missing)} profiles missing.')
    return {'verified_profiles': len(manifest['documents']), 'existing_preserved': len(profiles)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'apply'])
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        if args.snapshot is None:
            parser.error('--snapshot is required for prepare')
        result = prepare(json.loads((args.snapshot/'documents.json').read_text()),
                         json.loads((args.snapshot/'chunks.json').read_text()))
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps(result['summary'], indent=2))
    else:
        print(json.dumps(apply(json.loads(args.manifest.read_text()))))


if __name__ == '__main__':
    main()
