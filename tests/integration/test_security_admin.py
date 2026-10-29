"""Isolated real HTTP on ephemeral ports; never targets production or a GPU."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import threading
from unittest.mock import MagicMock

import pytest

from inferfabric.proxy import async_server, handler as handler_module
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from inferfabric.proxy.auth import AuthManager
from inferfabric.security.admin import SecurityAdmin
from inferfabric.security.auth_facade import SecurityAuth
from inferfabric.security.console_auth import set_password
from inferfabric.security.key_store import KeyStore


@contextmanager
def threaded_server(pm):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_module.ProxyHandler)
    server.proxy_mgr = pm
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('mode', ['threaded', 'async'])
def test_management_roundtrip_both_servers(tmp_path, monkeypatch, mode, caplog):
    store = KeyStore(tmp_path / 'security.db', provision=True)
    set_password(store, 'isolated-test-password')
    pm = MagicMock()
    pm.auth = SecurityAuth(AuthManager(), store)
    pm.pool_registry.snapshot.return_value.pools = {}
    pm.agent_registry = None
    pm.response_cache = None
    pm.mgr.gpu_mode = 'idle'
    pm.new_request_id.return_value = 'security-test'
    monkeypatch.setattr(handler_module, '_ADMIN_TOKEN', 'test-admin-token')

    async def roundtrip(base):
        pm.security_admin = SecurityAdmin(store, origins=[base])
        headers = {'Origin': base, 'X-Admin-Token': 'test-admin-token'}
        async with ClientSession() as client:
            async def request(method, path, body=None, extra=None):
                async with client.request(method, base + path, json=body,
                        headers={**headers, **(extra or {})}) as r:
                    return r.status, dict(r.headers), await r.json()
            status, _, _ = await request('GET', '/health')
            assert status == 200
            status, _, _ = await request('POST', '/admin/keys/login', {'password': 'isolated-test-password'},
                                          {'X-Admin-Token': 'wrong'})
            assert status == 401
            status, _, _ = await request('POST', '/admin/keys/login', {'password': 'isolated-test-password'},
                                          {'Origin': 'http://evil.test'})
            assert status == 403
            status, response_headers, result = await request('POST', '/admin/keys/login',
                                                             {'password': 'isolated-test-password'})
            assert status == 200
            assert 'Access-Control-Allow-Origin' not in response_headers
            headers['Cookie'] = response_headers['Set-Cookie'].split(';')[0]
            headers['X-CSRF-Token'] = result['csrf']
            status, _, _ = await request('POST', '/admin/keys', {'name': 'denied'}, {'X-CSRF-Token': ''})
            assert status == 401
            status, response_headers, result = await request('POST', '/admin/keys',
                                                              {'name': 'tool', 'models': ['iff/work']})
            assert status == 201 and response_headers['Cache-Control'] == 'no-store'
            token, entry = result['key'], result['entry']
            status, _, result = await request('GET', '/admin/keys')
            assert status == 200 and token not in json.dumps(result)
            assert result['policy'] == 'off'
            assert (await request('GET', '/admin/keys/reveal'))[0] == 404
            for policy in ('off', 'pools', 'all'):
                status, _, _ = await request('POST', '/admin/keys/policy',
                        {'policy': policy, 'confirm': policy, 'confirm_all': True})
                assert status == 200
                for path in ('/v1/chat/completions', '/v1/messages'):
                    body = {'model': 'iff/work', 'messages': [{'role': 'user', 'content': 'test'}], 'max_tokens': 1}
                    status, _, _ = await request('POST', path, body)
                    assert status == (404 if policy == 'off' else 401)
                    status, _, _ = await request('POST', path, body, {'x-api-key': token})
                    assert status == 404  # Real auth passed; no actual pool/backend configured.
            assert (await request('POST', '/admin/keys/revoke', {'id': entry['id']}))[0] == 200
            status, _, _ = await request('POST', '/v1/messages', {'model': 'iff/work'}, {'x-api-key': token})
            assert status == 401
            assert (await request('POST', '/admin/keys/logout', {}))[0] == 200
            assert (await request('GET', '/admin/keys'))[0] == 401
            assert token not in caplog.text and headers['Cookie'] not in caplog.text
            assert 'isolated-test-password' not in caplog.text

    if mode == 'threaded':
        with threaded_server(pm) as base:
            asyncio.run(roundtrip(base))
    else:
        async def scenario(ex):
            async with TestClient(TestServer(async_server.build_app(pm, ex, MagicMock()))) as client:
                await roundtrip(str(client.make_url('')).rstrip('/'))
        with ThreadPoolExecutor(max_workers=4) as ex:
            asyncio.run(scenario(ex))
