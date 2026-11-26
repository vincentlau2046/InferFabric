"""Issue #64 — 引擎端口 loopback 发布（安全）。

网关持有全部鉴权与计量（API Key、限流、用量统计），引擎进程无
`--api-key`；引擎端口一旦发布到所有网卡（`-p {port}:{port}` 或
`--host 0.0.0.0`），同网段任何机器可绕过网关直连引擎。

契约：
- Docker 路径：发布一律 `-p 127.0.0.1:{port}:{port}`；容器内
  `--host 0.0.0.0` 保留（容器 netns 内必须绑 0.0.0.0，docker 端口
  转发才可达 —— 安全性由发布前缀保证）；
- 非 Docker（conda）路径：`--host 127.0.0.1`；
- NInfer managed / legacy 均发布到 127.0.0.1（test_pm_ninfer.py）。
"""

import pytest

from inferfabric.config import SGLangConfig, VLLMConfig


def _publish_args(cmd: list[str]) -> list[str]:
    """docker run 里每个 -p 后面的发布参数。"""
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]


def _wildcard_binds(cmd: list[str]) -> list[str]:
    """0.0.0.0 中不在容器内 --host 位置的出现（宿主侧绑定 = 违规）。"""
    return [a for i, a in enumerate(cmd)
            if a == "0.0.0.0" and (i == 0 or cmd[i - 1] != "--host")]


def _vllm(port: int = 8101) -> VLLMConfig:
    return VLLMConfig(model_dir="models/Qwen3-8B", served_name="q-test",
                      port=port, conda_env="iff", max_model_len=4096,
                      gpu_memory_utilization=0.8)


def _sglang(port: int = 8102) -> SGLangConfig:
    return SGLangConfig(model_dir="models/Qwen3-8B", served_name="q-test",
                        port=port)


class TestLoopbackPublish:
    """issue #64：引擎端口只对宿主 loopback 发布。"""

    @pytest.mark.parametrize("cfg", [_vllm(), _sglang()], ids=["vllm", "sglang"])
    def test_conda_binds_loopback_only(self, cfg):
        """非 Docker（conda）路径 --host 127.0.0.1，无 0.0.0.0。"""
        cmd = cfg.build_cmd()
        assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
        assert "0.0.0.0" not in cmd, cmd

    @pytest.mark.parametrize("cfg", [_vllm(), _sglang()], ids=["vllm", "sglang"])
    def test_docker_publishes_loopback_only(self, cfg):
        """Docker 发布一律 127.0.0.1 前缀；0.0.0.0 只允许出现在容器内
        --host 位置（容器 netns 绑定，安全性由发布前缀保证）。"""
        cmd = cfg.build_docker_cmd("iff-test")
        pubs = _publish_args(cmd)
        assert pubs == [f"127.0.0.1:{cfg.port}:{cfg.port}"], (pubs, cmd)
        assert _wildcard_binds(cmd) == [], cmd
