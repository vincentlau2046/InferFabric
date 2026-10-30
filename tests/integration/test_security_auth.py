"""Real HTTP headers through the existing async edge and pool dispatch."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from inferfabric.proxy import async_server
from aiohttp.test_utils import TestClient, TestServer
from inferfabric.proxy.auth import AuthManager
from inferfabric.security.auth_facade import SecurityAuth
from inferfabric.security.key_store import KeyStore


def test_off_policy_unregistered_key_passes_auth(tmp_path):
    """off 宽限：未注册 key 不拦（2026-10-30 事故回归：客户端带占位 key 被 401 打挂）。"""
    store = KeyStore(tmp_path / 'security.db', provision=True)
    assert store.get_policy() == 'off'
    pm = MagicMock()
    pm.auth = SecurityAuth(AuthManager(), store)
    pm.pool_registry.snapshot.return_value.pools = {}
    pm.agent_registry = None
    pm.response_cache = None
    pm.new_request_id.return_value = 'off-grace'

    async def scenario(ex):
        async with TestClient(TestServer(async_server.build_app(pm, ex, MagicMock()))) as client:
            body = {'model': 'iff/work', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1}
            # 匿名（off 允许）与带未注册 key 都通过鉴权 → 都落到 unknown-pool 404
            assert (await client.post('/v1/chat/completions', json=body)).status == 404
            res = await client.post('/v1/messages', json=body,
                                    headers={'x-api-key': 'sk-iff-placeholder'})
            assert res.status == 404
    with ThreadPoolExecutor(max_workers=2) as ex:
        asyncio.run(scenario(ex))


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/messages'])
@pytest.mark.parametrize('header', ['Authorization', 'x-api-key'])
def test_pool_auth_protocol_headers(tmp_path, path, header):
    store = KeyStore(tmp_path / 'security.db', provision=True)
    store.set_policy('pools')
    meta, token = store.create_key('client', ['iff/work'], None)
    pm = MagicMock()
    pm.auth = SecurityAuth(AuthManager(), store)
    pm.pool_registry.snapshot.return_value.pools = {}  # Authorized requests reach unknown-pool 404.
    pm.agent_registry = None
    pm.response_cache = None
    pm.new_request_id.return_value = 'security-test'

    async def scenario(ex):
        async with TestClient(TestServer(async_server.build_app(pm, ex, MagicMock()))) as client:
            body = {'model': 'iff/work', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1}
            assert (await client.post(path, json=body)).status == 401
            headers = {header: ('Bearer ' if header == 'Authorization' else '') + token}
            assert (await client.post(path, json=body, headers=headers)).status == 404
            body['model'] = 'iff/other'
            assert (await client.post(path, json=body, headers=headers)).status == 401
            store.revoke_key(meta['id'])
            body['model'] = 'iff/work'
            assert (await client.post(path, json=body, headers=headers)).status == 401
    with ThreadPoolExecutor(max_workers=2) as ex:
        asyncio.run(scenario(ex))
