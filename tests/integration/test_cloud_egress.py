"""Configured upstreams remain reachable with a stale default urllib proxy."""

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from inferfabric import forwarder
from inferfabric.cloud_discovery import CloudDiscovery, CloudModel, ProviderConfig
from inferfabric.proxy import responses_bridge


@pytest.fixture
def direct_upstream(monkeypatch):
    class Upstream(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/models":
                self.reply(200, {"data": [{"id": "fixture-model"}]})
            else:
                self.reply(404, {})

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/chat/completions":
                self.reply(200, {"id": "direct-chat"})
            elif self.path == "/v1/responses":
                self.reply(200, {"id": "direct-response"})
            else:
                self.reply(404, {})

        def log_message(self, *args):
            pass

    # Reserve a loopback port without listening, so the stale proxy cannot
    # accidentally be reached or send requests outside this test.
    with socket.socket() as dead_proxy, HTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        dead_proxy.bind(("127.0.0.1", 0))
        host, port = dead_proxy.getsockname()
        proxy = f"http://{host}:{port}"
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # Model a long-lived urllib opener that was built before the proxy
        # stopped. The monkeypatch also restores any previous opener.
        monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener())
        thread = threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield ProviderConfig(name="fixture-provider",
                                 openai_base=f"http://127.0.0.1:{upstream.server_port}/v1",
                                 timeout=2)
        finally:
            upstream.shutdown()
            thread.join(timeout=2)


class CaptureResponse:
    def __init__(self):
        self.body = bytearray()
        self.wfile = SimpleNamespace(write=self.body.extend)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def _safe_write(self, data):
        self.body.extend(data)


def test_cloud_forwarding_ignores_a_stale_default_proxy(direct_upstream):
    client = CaptureResponse()
    model = CloudModel("fixture-model", direct_upstream.name)
    result = forwarder.forward_to_cloud(client, {"model": model.model_id}, direct_upstream,
                                        model, suppress_client_errors=True)
    assert result.status == 200, result.error
    assert json.loads(client.body)["id"] == "direct-chat"


def test_provider_discovery_ignores_a_stale_default_proxy(direct_upstream, tmp_path):
    discovery = CloudDiscovery(tmp_path / "cloud_provider.yaml")
    models = discovery._discover_provider(direct_upstream)
    assert models is not None
    assert [model.model_id for model in models] == ["fixture-model"]


def test_responses_native_ignores_a_stale_default_proxy(direct_upstream, tmp_path):
    cloud = CloudDiscovery(tmp_path / "cloud_provider.yaml")
    cloud._providers[direct_upstream.name] = direct_upstream
    model = CloudModel("fixture-model", direct_upstream.name)
    cloud._cloud_models[f"{model.provider}/{model.model_id}"] = model
    gate = SimpleNamespace(ok=True, release=lambda: None)
    pm = SimpleNamespace(cloud=cloud, dual_gate=SimpleNamespace(acquire_target=lambda *args, **kw: gate))
    candidate = responses_bridge._DirectCandidate(model.provider, model.model_id, "cloud")
    client = CaptureResponse()
    result = responses_bridge._probe_native(client, pm, {"model": model.model_id}, candidate,
                                              pool=None, remaining=2, client_stream=False)
    assert result.ok, result.error
    assert json.loads(client.body)["id"] == "direct-response"


def test_responses_chat_fallback_ignores_a_stale_default_proxy(direct_upstream):
    status, body = responses_bridge._probe_cloud_chat(direct_upstream, {"model": "fixture-model"}, 2)
    assert status == 200, body
    assert json.loads(body)["id"] == "direct-chat"
