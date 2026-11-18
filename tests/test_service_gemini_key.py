from services import ai_client


def test_service_key_never_reads_user_credentials(monkeypatch):
    monkeypatch.setattr(ai_client, 'LOCAL_TEST', False)
    monkeypatch.setenv('SAKI_SERVICE_GEMINI_API_KEY', 'synthetic-service-key')
    def forbidden(*args, **kwargs):
        raise AssertionError('Service must not read any user credential')
    monkeypatch.setattr(ai_client, 'get_effective_gemini_key', forbidden)
    monkeypatch.setattr(ai_client, 'get_current_user_id', lambda: None)
    monkeypatch.setattr(ai_client, 'log_full_prompt', lambda *a, **k: None)
    monkeypatch.setattr(ai_client, 'record_token_usage', lambda *a, **k: None)
    class Response:
        status_code = 200
        text = ''
        def json(self):
            return {'candidates':[{'content':{'parts':[{'text':'synthetic reply'}]},'finishReason':'STOP'}]}
    def post(url, **kwargs):
        assert kwargs['headers']['x-goog-api-key'] == 'synthetic-service-key'
        return Response()
    monkeypatch.setattr(ai_client.requests, 'post', post)
    assert str(ai_client.call_gemini([], service_key=True)) == 'synthetic reply'


def test_missing_service_key_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(ai_client, 'LOCAL_TEST', False)
    monkeypatch.delenv('SAKI_SERVICE_GEMINI_API_KEY', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('No user-key fallback or external request allowed')
    monkeypatch.setattr(ai_client, 'get_effective_gemini_key', forbidden)
    monkeypatch.setattr(ai_client.requests, 'post', forbidden)
    reply = ai_client.call_gemini([], service_key=True)
    assert reply.status_code == 503
    assert reply.error_code == 'service_not_configured'
