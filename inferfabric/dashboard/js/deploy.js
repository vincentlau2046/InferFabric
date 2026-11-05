/* InferFabric Console — Deploy tab (v2, Task 7)
 * 部署表单（name + engine 类型）+ 拉取表单（name + framework）+ 长任务进度态。
 *
 * 数据源：
 *   - POST /deploy {name, type}     — 生成 YAML 并切换（admin header）
 *   - POST /pull  {name, framework} — 下载权重（admin header）
 *
 * 设计纪律（R12 + R14）：
 *   - 部署表单仅含 name + engine 类型选择（vllm / llama_cpp——auto_deploy 仅支持
 *     此两者，model_discovery.py:141；sglang/ninfer/ollama 服务需 models.d/ 手动 YAML）。
 *     旧 model_dir / port / gpu_mem slider 字段已删除——后端 auto_deploy
 *     自动生成 YAML，不读这些字段（handler.py:1055 _handle_deploy 仅取 name+type）。
 *   - 拉取表单仅含 name + framework。
 *   - 事件委托（无 inline onclick）；数字等宽 tabular-nums；零 emoji（SVG 图标）。
 *   - 破坏性/重型操作 UI.confirm：deploy 启动模型=GPU 资源承诺；
 *     pull 网络磁盘密集。
 *   - /pull 同步无流式：in-flight 期间显示不定进度指示器，响应后替换为结果文本。
 *
 * 暴露：
 *   - window.tabRenderers['tab-deploy'] = renderDeploy
 *   - window.doDeploy() / window.doPull()
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[deploy] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  var ENGINE_LABEL = {
    vllm: 'vLLM', llama_cpp: 'llama.cpp',
  };

  function engineLabel(type) {
    return ENGINE_LABEL[type] || (type ? type.charAt(0).toUpperCase() + type.slice(1) : '—');
  }

  /* ── 进度态 helpers ──
   *   showProgress(msg)   — in-flight：不定 spinner + 消息
   *   showResult(title, detail) — 完成/失败：替换 spinner 为结果文本（保留可见） */
  function showProgress(msg) {
    var card = $('depProgressCard');
    var box = $('deployProgress');
    if (card) card.style.display = '';
    if (!box) return;
    box.innerHTML =
      '<div class="dep-progress-row">' +
        '<span class="dep-spinner"></span>' +
        '<span class="dep-progress-msg"></span>' +
      '</div>';
    var msgEl = box.querySelector('.dep-progress-msg');
    if (msgEl) msgEl.textContent = msg;
  }

  function showResult(title, detail) {
    var card = $('depProgressCard');
    var box = $('deployProgress');
    if (card) card.style.display = '';
    if (!box) return;
    box.innerHTML = '<div class="dep-progress-result"></div>';
    var el = box.querySelector('.dep-progress-result');
    if (el) el.textContent = title + (detail ? ' · ' + detail : '');
  }

  function setBtnBusy(action, busy) {
    var btn = document.querySelector('[data-action="' + action + '"]');
    if (btn) btn.disabled = busy;
  }

  /* ── 部署：POST /deploy {name, type} ──
   *   后端 auto_deploy 生成 YAML → switch。already_configured 时后端亦自动 switch
   *   （handler.py:1063），故统一视为成功路径。成功后跳转推理 TAB 查看新模型。 */
  window.doDeploy = async function () {
    var nameEl = $('depName');
    var typeEl = $('depType');
    var name = nameEl ? nameEl.value.trim() : '';
    var type = typeEl ? typeEl.value : 'vllm';
    if (!name) { UI.toast('请输入模型名称', 'error'); return; }

    var run = async function () {
      setBtnBusy('deploy', true);
      showProgress('正在部署 ' + name + '…');
      try {
        var res = await fetch('/deploy', {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({ name: name, type: type }),
        });
        var j = null;
        try { j = await res.json(); } catch (_) {}
        if (!res.ok) {
          var msg = (j && (j.error || j.message)) || ('HTTP ' + res.status);
          throw new Error(msg);
        }
        // 后端对逻辑失败也返回 HTTP 200（_handle_deploy 用 _send_json 默认 200），
        // 必须查 j.status：成功仅 {switched, already_active}（model_lifecycle.py:333）；
        // 其余（"Unsupported type for auto-deploy" / "Unknown model" / "Invalid transition"）为失败。
        var st = j && j.status ? j.status : '';
        if (st !== 'switched' && st !== 'already_active') {
          throw new Error((j && j.message) || ('未知状态: ' + (st || '(空)')));
        }
        showResult(name + ' 部署完成', engineLabel(type));
        UI.toast(name + ' 部署完成', 'ok');
        store.forceRefresh();
        store.switchTab('tab-inference');
      } catch (e) {
        showResult(name + ' 部署失败', (e && e.message ? e.message : String(e)));
        UI.toast('部署失败: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        setBtnBusy('deploy', false);
      }
    };

    UI.confirm({
      title: '部署模型',
      body: '将部署 ' + name + '（' + engineLabel(type) + '）。后端自动生成 YAML 并切换，'
            + '将占用 GPU 资源，可能耗时数十秒加载权重。确定？',
      onOk: run,
    });
  };

  /* ── 拉取：POST /pull {name, framework} ──
   *   /pull 同步无流式（ollama pull 子进程最长 1800s）。in-flight 期间显示不定
   *   spinner，响应后替换为结果文本。仅 ollama 框架实际支持拉取。 */
  window.doPull = async function () {
    var nameEl = $('pullName');
    var fwEl = $('pullFw');
    var name = nameEl ? nameEl.value.trim() : '';
    var fw = fwEl ? fwEl.value : '';
    if (!name) { UI.toast('请输入模型名称', 'error'); return; }

    var run = async function () {
      setBtnBusy('pull', true);
      showProgress('正在拉取 ' + name + '…');
      try {
        var res = await fetch('/pull', {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({ name: name, framework: fw }),
        });
        var j = null;
        try { j = await res.json(); } catch (_) {}
        if (!res.ok) {
          var msg = (j && (j.error || j.message)) || ('HTTP ' + res.status);
          throw new Error(msg);
        }
        var st = j && j.status ? j.status : '';
        if (st === 'pulled') {
          showResult(name + ' 拉取完成', (j && j.message) || '');
          UI.toast(name + ' 拉取完成', 'ok');
          store.forceRefresh();
        } else {
          // 后端返回 error 状态（如 vllm/llama_cpp 不支持拉取）
          var emsg = (j && j.message) || '未知结果';
          showResult(name + ' 拉取未完成', emsg);
          UI.toast('拉取未完成: ' + emsg, 'error');
        }
      } catch (e) {
        showResult(name + ' 拉取失败', (e && e.message ? e.message : String(e)));
        UI.toast('拉取失败: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        setBtnBusy('pull', false);
      }
    };

    UI.confirm({
      title: '拉取模型',
      body: '将拉取 ' + name + '（' + fw + '）。网络与磁盘密集，可能耗时数分钟。确定？',
      onOk: run,
    });
  };

  /* ── 渲染入口 ──
   *   部署/拉取表单为静态（无 snapshot 数据依赖）；进度卡由 doDeploy/doPull 显隐。
   *   渲染器仅作注册占位，保持与其他 TAB 一致的 tabRenderers 契约。 */
  function renderDeploy() {
    // 静态表单无需重绘；保留供 store.tab_active 订阅与未来扩展。
  }

  window.renderDeploy = renderDeploy;
  window.tabRenderers['tab-deploy'] = renderDeploy;

  store.on('tab_active', function (tab) {
    if (tab === 'tab-deploy') renderDeploy();
  });

  /* ── 事件委托（无 inline onclick） ── */
  var root = $('tab-deploy');
  if (root) {
    root.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-action]');
      if (!btn || !root.contains(btn) || btn.disabled) return;
      var act = btn.getAttribute('data-action');
      if (act === 'deploy') { window.doDeploy(); return; }
      if (act === 'pull') { window.doPull(); return; }
    });
  }
})();
