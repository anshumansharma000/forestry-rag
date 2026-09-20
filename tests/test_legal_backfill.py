import pytest

from scripts.legal_backfill import apply, classify, instrument_names, prepare


def chunk(document_id, text, index=0):
    return {'document_id': document_id, 'content': text, 'chunk_index': index,
            'page_start': index + 1, 'page_end': index + 1, 'section_heading': None}


def test_letter_citing_act_is_not_act_or_judgment():
    text = 'Government of India\nSub: Clarification under the Forest (Conservation) Act, 1980.\nSir,\nSupreme Court of India'
    assert classify({'source': 'Clarification Act 1980.pdf'}, [chunk('a', text)])[0] == 'clarification'


def test_bilingual_rules_identified_from_english_section_deep_in_file():
    text = 'These rules may be called the Van (Sanrakshan Evam Samvardhan) Rules, 2023.'
    assert classify({'source': 'rules.pdf'}, [chunk('a', 'हिन्दी पाठ'), chunk('a', text, 187)])[0] == 'rules'


def test_overlap_headings_do_not_create_multiple_instruments():
    text = ('These rules may be called the Van (Sanrakshan Evam Samvardhan) '
            'These rules may be called the Van (Sanrakshan Evam Samvardhan) Rules, 2023.')
    assert instrument_names([chunk('a', text)]) == ['the Van (Sanrakshan Evam Samvardhan) Rules, 2023']


def test_ambiguous_editions_are_queued_not_arbitrarily_linked():
    a, b, c = [f'00000000-0000-0000-0000-{i:012d}' for i in range(1, 4)]
    documents = [{'id': name, 'source': f'{name}.pdf'} for name in [a, b, c]]
    text = 'This Act may be called the Forest (Conservation) Act, 1980.'
    chunks = [chunk(a, text), chunk(b, text), chunk(c, 'Reference: Forest (Conservation) Act, 1980.')]
    manifest = prepare(documents, chunks)
    assert manifest['documents'][2]['profile']['relationships'] == []
    assert manifest['ambiguous_references'][0]['target_candidates'] == [a, b]
    assert not any(entry['profile']['reviewed'] for entry in manifest['documents'])


def test_commencement_notification_not_classified_as_amendment():
    text = 'Sub: Fixing the appointed date under the Forest (Conservation) Amendment Act, 2023. Madam/Sir,'
    assert classify({'source': 'FCAmendment2023.pdf'}, [chunk('a', text)])[0] == 'notification'


def test_apply_rejects_prod_before_any_reads():
    with pytest.raises(ValueError, match='dev namespace'):
        apply({'namespace': 'prod'}, object())


def test_reviewer_profiles_are_not_overwritten_and_rerun_is_noop():
    class Client:
        namespace = 'dev'
        client = None
        saved = {'a': {'reviewed': True}}
        calls = []

        def profiles(self):
            return self.saved.copy()

        def table(self, name):
            assert name == 'legal_document_profiles'
            return self

        def upsert(self, rows, **kwargs):
            assert kwargs == {'on_conflict': 'namespace,document_id', 'ignore_duplicates': True}
            self.calls.append(rows)
            self.saved.update({r['document_id']: r['profile'] for r in rows if r['document_id'] not in self.saved})
            return self

        def execute(self):
            return self

    repo = Client()
    repo.client = repo
    manifest = {'namespace': 'dev', 'documents': [{'document_id': name, 'profile': {}} for name in ['a', 'b']]}
    result = apply(manifest, repo)
    assert result['existing_preserved'] == 1
    assert repo.saved['a']['reviewed']
    assert len(repo.calls) == 1
    apply(manifest, repo)
    assert len(repo.calls) == 1
