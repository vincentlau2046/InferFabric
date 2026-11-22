"""inferfabric/jobs.py — 控制面长任务注册表（批 4 TOP3b · spec 2026-11-17-batch4-jobs-infra.md）。

复用 packs 安装的 202+Thread+轮询模式（pack_handlers.start_install 先例）：
  POST 创建 job → 202 {job_id, status}；线程跑任务；GET /admin/jobs/{id} 轮询。

设计约束：
- 协作式取消：cancel() 只置位 cancelled 标志，任务体在检查点自查收尾
  （模型加载无法安全中断 — switch 不设检查点，取消=放弃等待，结果仍如实落账）；
- 线程安全：_lock 保护全部读写；job 状态机 queued→running→(done|failed|cancelled)；
- 上限保留 50 条已完结记录，防长驻进程内存泄漏；
- discover_all 的取消检查点在 provider 循环间（cloud_discovery），网络抓取
  单次超时由 cfg.timeout 界定，检查点粒度 = 每 provider。
"""

import threading
import time
import uuid


class JobRegistry:
    """控制面长任务状态机。线程安全；单测覆盖状态迁移与取消语义。"""

    TERMINAL = {"done", "failed", "cancelled"}
    MAX_FINISHED = 50

    def __init__(self, max_finished: int = MAX_FINISHED):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._max_finished = max_finished

    def create(self, kind: str, label: str = "") -> dict:
        """登记新任务。返回 {job_id, status:"queued"}。"""
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "kind": kind,
                "label": label,
                "status": "queued",
                "created_at": time.time(),
                "finished_at": None,
                "result": None,
                "cancel_requested": False,
            }
        return {"job_id": job_id, "status": "queued"}

    def start(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job["status"] == "queued":
                job["status"] = "running"

    def cancel_requested(self, job_id: str) -> bool:
        """任务体检查点调用：True = 用户已请求取消。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job["cancel_requested"])

    def request_cancel(self, job_id: str) -> dict:
        """用户请求取消。已完结 → 409；活跃 → 置位并返回确认。
        任务体负责在检查点收尾为 cancelled（未到检查点的任务保持 running 直到自查）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"error": "unknown job"}, 404
            if job["status"] in self.TERMINAL:
                return {"error": "job already finished", "status": job["status"]}, 409
            job["cancel_requested"] = True
        return {"job_id": job_id, "status": "cancelling"}, 200

    def finish(self, job_id: str, status: str, result=None) -> None:
        """任务体收尾。status ∈ done|failed|cancelled；非活跃 job 忽略（幂等）。
        超限裁剪最旧已完结记录。"""
        if status not in self.TERMINAL:
            raise ValueError(f"invalid terminal status: {status}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in self.TERMINAL:
                return
            # 竞态保护：用户取消与任务自然完成同时到达 — 完成路径如实落账，
            # 不覆盖为 cancelled（结果已产出，假装取消是撒谎）。
            if status != "cancelled" and job["cancel_requested"]:
                pass  # 完成态优先，cancel_requested 留痕
            job["status"] = status
            job["finished_at"] = time.time()
            job["result"] = result
            self._prune_locked()

    def get(self, job_id: str) -> "dict | None":
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            out = dict(job)
            out.pop("cancel_requested", None)  # 内部协控位不外露
            return out

    def _prune_locked(self) -> None:
        finished = [j for j in self._jobs.values() if j["status"] in self.TERMINAL]
        if len(finished) <= self._max_finished:
            return
        finished.sort(key=lambda j: j["finished_at"] or 0)
        for j in finished[: len(finished) - self._max_finished]:
            self._jobs.pop(j["job_id"], None)

    def run_async(self, registry_job: dict, target_fn, on_error_result) -> None:
        """起线程跑 target_fn(should_cancel: callable[[], bool])。
        target_fn 返回 (terminal_status, result_payload)；异常归一为 failed。
        on_error_result(exc) → result payload（调用方注入友好错误结构）。
        """
        job_id = registry_job["job_id"]

        def _run():
            self.start(job_id)
            try:
                status, payload = target_fn(lambda: self.cancel_requested(job_id))
                self.finish(job_id, status, payload)
            except Exception as exc:  # 任务体异常绝不炸线程 — 如实落 failed
                self.finish(job_id, "failed", on_error_result(exc))

        t = threading.Thread(target=_run, name=f"job-{job_id}", daemon=True)
        t.start()
        return t
