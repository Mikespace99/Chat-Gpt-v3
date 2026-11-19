from app.messaging_parser import extract_messages


def test_gateway_reads_all_entries_and_dedups_inside_payload():
    msg = {'type': 'text', 'text': {'body': 'ciao'}, 'from': '39333123',
           'id': 'wamid.1', 'timestamp': '1794300000'}
    entry = {'changes': [{'value': {'metadata': {'display_phone_number': '39081123'},
                                    'messages': [msg, msg]}}]}
    second = {'changes': [{'value': {'metadata': {'display_phone_number': '3908999'},
                                     'messages': [{**msg, 'id': 'wamid.2'}]}}]}
    result = extract_messages({'entry': [entry, second]})
    assert len(result) == 2
    assert {r['to'] for r in result} == {'39081123', '3908999'}


def test_gateway_skips_statuses_unsupported_missing_metadata():
    assert extract_messages({'entry': [{'changes': [
        {'value': {'statuses': [{'id': 's'}]}},
        {'value': {'messages': [{'id': 'a', 'type': 'text', 'text': {'body': 'hi'}}]}},
    ]}]}) == []


def test_gateway_preserves_interactive_reply_id():
    payload = {'entry': [{'changes': [{'value': {
        'metadata': {'display_phone_number': '39081'},
        'messages': [{'id': 'wamid.x', 'from': '39333', 'type': 'interactive',
                      'interactive': {'button_reply': {'id': 'confirm:example-uuid',
                                                        'title': 'Sì'}}}]
    }}]}]}
    result = extract_messages(payload)
    assert result[0]['button_reply_id'] == 'confirm:example-uuid'
    assert result[0]['message'] == 'Sì'
