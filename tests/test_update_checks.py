from unittest.mock import patch

import pytest

from trafficgen import maintenance


@pytest.mark.parametrize('action', ['check_update', 'target_check'])
def test_inline_release_check_returns_job_identity_without_redirect(simulator, action):
    client = simulator.test_client()
    headers = {'Accept': 'application/json'}
    with client.session_transaction() as session:
        session['trafficgen_admin'] = True
        session['csrf_token'] = 'token'
    with patch.object(maintenance, 'status', return_value={'busy': False}), \
         patch.object(maintenance, 'enqueue', return_value={'id': 'accepted-check'}) as enqueue:
        response = client.post('/settings/system/action', headers=headers,
                               data={'csrf_token': 'token', 'action': action})
        assert response.status_code == 202
        assert response.json['job_id'] == 'accepted-check'
        assert 'Location' not in response.headers
        enqueue.assert_called_once()
        assert enqueue.call_args.args[1] == action
        # CSRF protection is unchanged for JSON responses.
        response = client.post('/settings/system/action', headers=headers, data={'action': action})
        assert response.status_code == 400
        assert enqueue.call_count == 1
    with patch.object(maintenance, 'status', return_value={'busy': True}):
        response = client.post('/settings/system/action', headers=headers,
                               data={'csrf_token': 'token', 'action': action})
        assert response.status_code == 409
        assert response.json['error']
        assert 'Location' not in response.headers
    with patch.object(maintenance, 'status', return_value={'busy': False}), \
         patch.object(maintenance, 'enqueue', return_value={'id': 'native-check'}):
        response = client.post('/settings/system/action', data={'csrf_token': 'token', 'action': action})
        assert response.status_code == 302  # Native/non-JavaScript forms keep their fallback.
