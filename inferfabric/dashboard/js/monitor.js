/* InferFabric Console — Monitor tab (v2, Task 5)
 * 纯遥测、只读、零操作（spec §4.3）。
 *   - 5 ECharts: 功耗/电费单图双轴（v6.2 取代 GPU vram+util 时间曲线——实时值已在
 *     顶部 GPU KPI 卡；柱=平均功耗 W 左轴 + 折线=累计电量/电费 度=元 右轴：
 *     流速↔存量因果对，charts.js 的 dualAxis 显式放行，见 _applyRules 注释）/
 *     Token 左右双卡双轴（v6.4 取代上下堆叠 + 费用概览卡）：
 *     共享粒度控制条（分钟/小时/天/周）驱动 col-6×2 双卡，每卡
 *     柱=每桶 Prompt/Completion 堆叠（左轴 tokens）+ 折线=窗口起点累积（右轴）——
 *     本地=累计 token 量（自己的 GPU 免费跑，量有意义），
 *     云端=累计费用 ¥（按量付费，钱有意义；桶 cost 服务端按价格表算）/
 *     TTFT/TPOT 双卡趋势折线
 *   - 6 KPI（2 行 × 3 列）: KV Cache / Batch Size / Seq Length / TPOT(ms) / TTFT(s) / Throughput
 *     （GET /api/engine_metrics）
 *   - 2 表: 请求日志 + 切换历史（13px 紧凑）
 *
 * 数据源（全部 GET，只读）：
 *   - store /api/snapshot → metrics_24h request_log history gpu gpu_util active_services
 *     （token_stats 30d 属 overview.js 7 天趋势，Monitor Token 卡四档改走端点）
 *   - GET /api/token-curve?granularity=minute|hour|day|week → Token 用量四档
 *     （服务端分桶 + limit=100000；label=分桶单位：分钟=12×5min/小时=24×1h/
 *      天=30×1d/周=13×7d；local/cloud 双 scope；月整体弃用；
 *      每桶 cost 字段（¥，4dp）= 云端费用，未配价模型计 0）
 *   - GET /api/engine_metrics?model=<active> → 6 KPI 原始指标
 *   - GET /api/power?gran=hour|day|week → 功耗/电费分桶（5min TTL；服务端 60s
 *     采样落 SQLite，页面关着历史也连续；小时=近24h/天=近30天/周=近~90天）
 *
 * 窗口/粒度切换 = 客户端 display filter（不触达服务端状态变更）。
 *
 * 暴露：window.tabRenderers['tab-monitor'] = renderMonitor
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[monitor] UI/store not ready — deferring');
    return;
  }

  var IFCharts = window.IFCharts;
  var $ = function (id) { return document.getElementById(id); };

  /* ── 状态 ── */
  var _pgran = 'hour';           // 功耗/电费图表粒度 display filter（小时/天/周）
  var _tokenGran = 'hour';       // Token 图表粒度 display filter（分钟/小时/天/周）
  var _charts = { power: null, tokenLocal: null, tokenCloud: null, ttft: null, tpot: null, agent: null };
  var _chartsInit = false;

  /* ── 客户端 / Agent 用量卡（v6.5） ──
   * 数据源 GET /api/agent-stats；5min TTL + inflight guard；不随 3s snapshot。
   * 认领：POST /api/agents（adminHeaders）；DELETE /api/agents?id= 管理。 */
  var _AGENT_TTL = 300000;        // 5min——四卡统一口径（fetch + 重绘均 5min，图不闪）
  var _agentCache = null, _agentLast = 0, _agentInflight = false;
  var _agentCfg = { gran: 'hour', scope: 'all' };
  var _agentManage = false;
  var _claimSample = '';

  // 功耗/电费卡独立 TTL 缓存（5min）——历史功耗无需秒级刷新；不随 3s snapshot 重绘。
  // 数据源 /api/power（服务端 60s 采样 + v007 表 + 5min 后端 TTL），与延迟卡同构。
  var _pgranCache = {};
  var _pgranInflight = {};
  var _POWER_TTL = 300000;       // 5min——四卡统一口径
  var _pgranRendered = null;     // 已完整渲染（热缓存 + 同档 → 3s 轮询下跳过重绘）

  // 引擎指标节流（避免每 3s 轮询都打 /api/engine_metrics）
  var _engineCache = null;
  var _engineModel = null;
  var _lastEngineFetch = 0;
  var ENGINE_TTL = 15000;        // 15s

  // Token 卡四档统一节流 + 数据源（单位语义统一 v6.3）：
  // 四档都走 GET /api/token-curve?granularity=（服务端分桶 + limit=100000；
  // 不再用 token_stats 的 day/月 —— token_stats 30d 留存且随 snapshot 3s 轮询，
  // 周档只有端点有 90d 数据）。每档独立缓存：_tokenCurveCache[gran] = {data, at}，
  // data = 端点响应 {local, cloud}，local[i]/cloud[i] = idx i（i=0 最旧 → n-1 最新），
  // 含 prompt/completion/cached 拆分。
  var _tokenCurveCache = {};         // gran -> {data, at}
  var TOKEN_CURVE_TTL = 300000;      // 5min——四卡统一口径（fetch + 重绘均 5min，图不闪）
  var _tokenRenderedAt = {};         // gran -> 上次渲染用的 cache.at（同 at → 跳过重绘，防闪）

  /* ── 小工具 ── */
  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function shortName(m) {
    if (!m) return '—';
    return String(m).split('/').pop();
  }

  function isMonitorActive() {
    var el = $('tab-monitor');
    return !!(el && el.classList.contains('active'));
  }

  function fmtTok(n) {
    if (n == null || isNaN(n)) return '0';
    n = Number(n);
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(n);
  }

  function showEmpty(id, show) {
    var el = $(id);
    if (el) el.style.display = show ? '' : 'none';
  }

  /* ── 图表初始化（null-check：IFCharts 缺 echarts 时返回 null） ── */
  function ensureCharts() {
    if (_chartsInit) return;
    _chartsInit = true;
    if (IFCharts && typeof IFCharts.create === 'function') {
      _charts.power = IFCharts.create('monPowerChart');
      _charts.tokenLocal = IFCharts.create('monTokenLocalChart');
      _charts.tokenCloud = IFCharts.create('monTokenCloudChart');
      _charts.ttft = IFCharts.create('monTtftChart');
      _charts.tpot = IFCharts.create('monTpotChart');
      _charts.agent = IFCharts.create('monAgentChart');
    }
    // null → 容器显示 empty state（IFCharts 已 log warning）
    if (!_charts.power) {
      var p = $('monPowerChart');
      if (p) p.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tokenLocal) {
      var tl = $('monTokenLocalChart');
      if (tl) tl.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tokenCloud) {
      var tc = $('monTokenCloudChart');
      if (tc) tc.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.ttft) {
      var tt = $('monTtftChart');
      if (tt) tt.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tpot) {
      var tp = $('monTpotChart');
      if (tp) tp.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
  }

  /* ── 1. 功耗 / 电费（单图双轴，v6.2 取代 GPU 显存/利用率曲线）──
   * 口径：GPU 板卡功耗（nvidia-smi power.draw，含 idle），¥1/度。
   * 粒度 hour/day/week = display filter（服务端分桶，GET /api/power；月整体弃用）。
   *   - 柱（左轴 W）  = 每桶平均功耗——看"哪个时段烧得凶"
   *   - 折线（右轴 度=元） = 窗口起点累计电量/电费——看"一共烧了多少、花了多少"
   * 双轴合法性：功耗↔累计电量是 流速↔存量 因果对（累计=功率对时间积分），
   * 非 TTFT/TPOT 那种无关量纲对比；故显式 {dualAxis:true} 放行（charts.js 唯一受权例外）。
   * 刷新：5min TTL 独立拉取 + 完成回调重绘；不随 3s snapshot 重绘（热缓存+同档跳过）。 */
  function _pgranWinText(gran) {
    if (gran === 'day') return '近 30 天';
    if (gran === 'week') return '近 90 天';
    return '近 24h';
  }

  /* 右轴（度）刻度标签：总量小（<1 度）时 2 位小数不被 toFixed(1) 压成 0.0；
   * 步长恒为 {1,2,5}×10^k（见 _niceCeil），去掉尾零 —— 0.20 度 → 0.2 度。 */
  function _pgranKwhFmt(v) {
    var s = (Math.abs(v) >= 1 ? Number(v).toFixed(1) : Number(v).toFixed(2));
    return s.replace(/0+$/, '').replace(/\.$/, '') + ' 度';
  }

  /* nice 上取整：返回最小的 n×步长 ≥ v，步长 ∈ {1,2,5}×10^k。
   * 右轴（度）用它定 max —— max/n 是干净步长，n 等分下每格标签
   * 总量大时是整数（37 度 → max 60 → 0,10,…,60），
   * 总量 <1 度时是 0.1/0.2/0.5 档干净小数（0.64 度 → max 1.2 → 0,0.2,…,1.2）。 */
  function _niceCeil(v, n) {
    if (!(v > 0)) v = 1;
    var raw = v / n;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;           // [1, 10)
    var step;
    if (norm <= 1) step = 1;
    else if (norm <= 2) step = 2;
    else if (norm <= 5) step = 5;
    else step = 10;
    // 拍掉浮点噪声（0.6442/6→step 0.2 → n*step*mag=1.2000000000000002）：
    // 若带回 1.2000000000000002，echarts 刻度数 = ceil(max/interval)
    // = ceil(6.000000000000001)=7，与左轴 6 等分错位 → 网格对不齐。
    return +((n * step * mag).toFixed(12));
  }

  function _pgranLabels(buckets, gran) {
    return buckets.map(function (b) {
      var d = new Date(b.t * 1000);   // b.t = 桶起点 epoch 秒（服务端本地时区对齐）
      if (gran === 'day' || gran === 'week') return (d.getMonth() + 1) + '-' + d.getDate();
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    });
  }

  function getPgranSeries(gran) {
    var c = _pgranCache[gran];
    var now = Date.now();
    if (c && now - c.at < _POWER_TTL) return c.data;
    if (!_pgranInflight[gran]) {
      _pgranInflight[gran] = true;
      fetch('/api/power?gran=' + gran, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _pgranCache[gran] = { data: data || {}, at: Date.now() };
          if (isMonitorActive()) drawPower();
        })
        .catch(function (e) { console.warn('[monitor] /api/power fetch failed:', e); })
        .then(function () { _pgranInflight[gran] = false; });
    }
    return c ? c.data : {};
  }

  function _pgranTooltip(params) {
    if (!params || !params.length) return '';
    var label = params[0].axisValue;
    var rows = '';
    for (var i = 0; i < params.length; i++) {
      var p = params[i];
      if (p.seriesName === '平均功耗') {
        rows += '<div>' + p.seriesName + '：' +
          (p.value == null ? '—' : Math.round(p.value) + ' W') + '</div>';
      } else if (p.seriesName === '累计电量') {
        // 数据为对象式点位 {value, symbol:'circle'}——逐桶打点后须解包
        var vRaw = p.value;
        var v = (vRaw && typeof vRaw === 'object') ? vRaw.value : vRaw;
        rows += '<div>' + p.seriesName + '：' +
          (v == null ? '—' : Number(v).toFixed(2) + ' 度 · ¥' + Number(v).toFixed(2)) + '</div>';
      }
    }
    return '<div><b>' + label + '</b></div>' + rows;
  }

  function drawPower() {
    ensureCharts();
    var data = getPgranSeries(_pgran);
    var buckets = (data && data.buckets) || [];
    var totals = (data && data.totals) || {};
    var available = !!(data && data.available);

    // hero 行：窗口累计电费（大字）+ 累计度数 · 平均功率 · 窗口
    var heroCost = $('monPowerCost');
    var heroMeta = $('monPowerMeta');
    if (heroCost) {
      heroCost.textContent = '¥' + (totals.yuan || 0).toFixed(2);
    }
    if (heroMeta) {
      heroMeta.textContent = '累计 ' + (totals.kwh || 0).toFixed(1) + ' 度 · 平均 ' +
        (totals.avg_w != null ? Math.round(totals.avg_w) + ' W' : '—') +
        ' · ' + _pgranWinText(_pgran);
    }

    if (!_charts.power) return;
    showEmpty('monPowerEmpty', !available || !buckets.length);
    if (!available || !buckets.length) {
      // 空态也须给出完整双轴骨架（charts.js 双轴为显式受权例外，见 _applyRules
      // opts.dualAxis；缺 axis 数组 + dualAxis 时 yAxisIndex:1 引用不存在轴，
      // echarts 首渲染即抛 cartesian2d getInitialData 异常）
      IFCharts.update(_charts.power, {
        xAxis: { data: [], boundaryGap: true },
        // 双轴同构骨架：左 W 固定 0–600（卡 TDP 封顶，不会突破，无需动态取整）；
        // 右 度 空态 _niceCeil(0,6)=1.2 → 0,0.2,…,1.2。两轴同 splitNumber:6 + min:0
        // → 6 等分像素位置重合，右轴只印标签不画网格线，一套尺度线（左轴的）。
        yAxis: [
          { name: 'W', min: 0, max: 600, splitNumber: 6, position: 'left',
            axisLabel: { formatter: function (v) { return v + ' W'; } } },
          { name: '度', min: 0, max: _niceCeil(0, 6), splitNumber: 6,
            splitLine: { show: false }, position: 'right',
            axisLabel: { formatter: _pgranKwhFmt } },
        ],
        tooltip: { trigger: 'axis', formatter: _pgranTooltip },
        legend: { data: ['平均功耗', '累计电量'] },
        series: [
          { type: 'bar', name: '平均功耗', yAxisIndex: 0, data: [], z: 2 },
          { type: 'line', name: '累计电量', yAxisIndex: 1, data: [], z: 3 },
        ],
      }, { dualAxis: true });
      return;
    }

    var xs = _pgranLabels(buckets, _pgran);
    var avg = buckets.map(function (b) { return b.avg_w == null ? null : b.avg_w; });
    // 累计线：全部桶逐桶打点（方案 B）——前导空桶 cum=0 也带点、数据桶带真值，
    // 线从窗口起点 0 连续到末端总量；对象式点位在 house symbol:'none' 下才可见。
    var cum = buckets.map(function (b) {
      return { value: b.cum_kwh, symbol: 'circle', symbolSize: 5 };
    });
    // 左轴 W 固定 0–600（卡 TDP 封顶，不会突破）；右轴 度 max = _niceCeil(累计量, 6)
    // —— nice 上取整到 6 等分干净步长，max/6 恒为 {1,2,5}×10^k：
    //   总量大 → 整数刻度（37 度 → max 60 → 0,10,…,60），总量 <1 度 → 0.1/0.2/0.5 档。
    // 两轴同 splitNumber:6 + min:0 → 6 等分像素位置重合，右轴只印标签（splitLine 隐藏），
    // 只有左轴一套尺度线——「坐标整数」「max 随实际调节」「不能有两套尺度线」三点齐。
    var maxKwh = _niceCeil((totals.kwh || 0) || 1, 6);

    IFCharts.update(_charts.power, {
      xAxis: { data: xs, boundaryGap: true },
      yAxis: [
        { name: 'W', min: 0, max: 600, splitNumber: 6, position: 'left',
          axisLabel: { formatter: function (v) { return v + ' W'; } } },
        { name: '度', min: 0, max: maxKwh, splitNumber: 6,
          splitLine: { show: false }, position: 'right',
          axisLabel: { formatter: _pgranKwhFmt } },
      ],
      tooltip: { trigger: 'axis', formatter: _pgranTooltip },
      legend: { data: ['平均功耗', '累计电量'] },
      series: [
        { type: 'bar', name: '平均功耗', yAxisIndex: 0, data: avg, barWidth: '55%', z: 2 },
        { type: 'line', name: '累计电量', yAxisIndex: 1, data: cum, z: 3,
          areaStyle: { opacity: 0.08 } },
      ],
    }, { dualAxis: true });
  }

  function renderPowerCard() {
    // 热缓存 + 本档已渲染 → 数据没变，跳过重绘（3s 轮询不闪图）；
    // 只读路径（getPgranSeries 内部 TTL 过期 → fetch → 落地 drawPower()）。
    ensureCharts();
    var c = _pgranCache[_pgran];
    if (c && (Date.now() - c.at) < _POWER_TTL && _pgranRendered === _pgran) return;
    drawPower();
    _pgranRendered = _pgran;
  }

  /* ── 2. Token 用量：左右双卡双轴（v6.4 取代上下堆叠 + 费用概览卡）──
   * 共享粒度控制条（data-seg=gran，分钟/小时/天/周）驱动 col-6×2 双卡同粒度联动。
   * 每卡双轴（{dualAxis:true} 受权例外，与功耗卡同构——流速↔存量积分对）：
   *   - 柱（左轴 tokens）= 每桶 Prompt/Completion 堆叠——"哪个时段用了多少"
   *   - 折线（右轴）= 窗口起点累积（客户端前缀和，后端零改动）：
   *       本地 = 累计 token 量（自己的 GPU 只花电费，量 = 干了多少活）
   *       云端 = 累计费用 ¥（桶 cost 由 /api/token-curve 按价格表逐请求算；
   *               按量付费，钱才是要看的量；未配价模型计 0）
   * 四档统一走端点：GET /api/token-curve?granularity=minute|hour|day|week
   *（服务端分桶：minute=12×5min / hour=24×1h / day=30×1d / week=13×1周；
   *  token_stats 的 day/月 已弃用 —— 周档只有端点有 90d 数据）。
   * 响应 {local:[...], cloud:[...]} 双 scope，各桶含 prompt/completion/cached/cost 拆分。
   * 空态：同一 update 路径恒给双 yAxis 结构（yAxisIndex:1 不会引用不存在的轴，
   *  无需功耗卡式独立空态骨架分支——那里左轴需显式 0–600 封顶值）。 */
  // 每档桶数 n 与桶宽（ms），与服务端 spec 字典一一对应
  var _TOKEN_N = { minute: 12, hour: 24, day: 30, week: 13 };
  var _TOKEN_W = { minute: 5 * 60000, hour: 3600000, day: 86400000, week: 7 * 86400000 };
  var _tokenCurveInflight = {};   // gran -> true（防并发重复 fetch）

  function buildTokenData(gran, scope) {
    var now = Date.now();

    // 同步返回缓存（命中 TTL）；否则启动异步 fetch 并返回上次缓存或全零（不阻塞渲染）。
    var hit = _tokenCurveCache[gran];
    if (hit && (now - hit.at) < TOKEN_CURVE_TTL) {
      return _tokenFromBuckets(gran, hit.data[scope] || [], now);
    }
    if (!_tokenCurveInflight[gran]) {
      _tokenCurveInflight[gran] = true;
      fetch('/api/token-curve?granularity=' + gran, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          // 保留 local + cloud 两个 scope（供本地/云端两张图各自取用）
          _tokenCurveCache[gran] = { data: data || {}, at: now };
        })
        .catch(function (e) {
          console.warn('[monitor] token-curve(' + gran + ') fetch failed:', e);
          // 失败不清缓存（保留下次可用旧值）；无缓存则置空
        })
        .then(function () {
          _tokenCurveInflight[gran] = false;
          if (isMonitorActive()) renderTokenChart();
        });
    }
    return _tokenFromBuckets(gran, (hit && hit.data[scope]) || [], now);
  }

  // 把 token-curve 的 scope 桶数组（idx 0=最旧→n-1=最新）转成图表数据。
  // cumTok/cumCost = 窗口起点累积（前缀和）：本地卡用 cumTok（累计 token 量），
  // 云端卡用 cumCost（累计费用 ¥，桶 cost 字段；未配价模型 cost=0）。
  // 无缓存时返回 n 个零桶（空图 + empty state，累积线平 0）。
  // x 轴标签用桶起点 b.t（墙钟对齐，与功耗卡同口径）；b.t 缺失时回退旧推算。
  function _tokenFromBuckets(gran, buckets, now) {
    var n = _TOKEN_N[gran] || 24;
    var w = _TOKEN_W[gran] || 3600000;
    var xs = [], prompt = [], comp = [], cumTok = [], cumCost = [];
    var runTok = 0, runCost = 0;
    for (var k = 0; k < n; k++) {
      var b = (buckets && buckets[k]) || {};
      var tMs = b.t != null ? (b.t * 1000) : (now - (n - 1 - k) * w);
      xs.push(_tokenGranLabel(gran, tMs));
      var p = b.prompt || 0, c = b.completion || 0;
      prompt.push(p);
      comp.push(c);
      runTok += p + c;          cumTok.push(runTok);
      runCost += b.cost || 0;   cumCost.push(runCost);
    }
    return { xs: xs, prompt: prompt, completion: comp, cumTok: cumTok, cumCost: cumCost };
  }

  // 桶标签：minute → HH:mm（桶起点）；hour → HH:00；day/week → MM-DD
  function _tokenGranLabel(gran, dateMs) {
    var d = new Date(dateMs);
    if (gran === 'minute') return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    if (gran === 'hour') return String(d.getHours()).padStart(2, '0') + ':00';
    return (d.getMonth() + 1) + '-' + d.getDate();
  }

  /* 缓存命中率（Cache Hit Rate）= 缓存命中 prompt tokens / 总 prompt tokens。
   * 双协议统一口径：OpenAI 系取 prompt_tokens_details.cached_tokens（计入
   * prompt_tokens）；Anthropic 系取 cache_read_input_tokens（creation 计入
   * 总量、不计命中）。窗口 = 当前粒度的全部桶（端点响应 cached/prompt 汇总；
   * 不再用 token_stats 的 30d）。 */
  function _scopeCacheHitRate(scope) {
    var hit = _tokenCurveCache[_tokenGran];
    var buckets = (hit && hit.data[scope]) || [];
    var p = 0, c = 0;
    for (var k = 0; k < buckets.length; k++) {
      var b = buckets[k] || {};
      p += b.prompt || 0;
      c += b.cached || 0;
    }
    return p > 0 ? c / p : null;
  }

  function renderCacheHitBadges() {
    var pairs = [['monTokenLocalCacheHit', 'local'],
                 ['monTokenCloudCacheHit', 'cloud']];
    for (var i = 0; i < pairs.length; i++) {
      var el = $(pairs[i][0]);
      if (!el) continue;
      var rate = _scopeCacheHitRate(pairs[i][1]);
      el.textContent = rate == null
        ? 'Cache Hit Rate —'
        : 'Cache Hit Rate ' + (rate * 100).toFixed(1) + '%';
    }
  }

  /* 右轴（¥）刻度标签：与功耗卡「度」标签同手法——总量 <¥1 时 2 位小数
   * 不被 toFixed(1) 压成 ¥0，去尾零（¥0.20 → ¥0.2）。 */
  function _yuanFmt(v) {
    var s = (Math.abs(v) >= 1 ? Number(v).toFixed(1) : Number(v).toFixed(2));
    return '¥' + s.replace(/0+$/, '').replace(/\.$/, '');
  }

  /* 调色板取色（house 铁律：系列色固定为调色板顺序、不硬编码）。
   * v7.0 统一调色板 5 槽（蓝→琥珀→青→绿→粉），dark/light 同 hex；取第 i 色。
   *
   * 统一色系规则（v7.0，跨卡语义→色号映射，后续新图表按此标准取色）：
   *   「累积/存量」线（功耗卡「累计电量」+ Token 双卡「累计 Token/累计费用」）
   *     = palette[1] 琥珀，跨卡锚点；暖色与冷色柱天然分层（线 vs 柱不同 mark type
   *     + 不同轴 + 暖冷对立，CVD 全对验证无须次级编码即过）。
   *   「分量/流速」柱（Token 双卡 Prompt/Completion 堆叠柱）
   *     = palette[0] 蓝（Prompt）+ palette[2] 青（Completion）——跳过琥珀槽[1]
   *     避免柱与累计线同色；蓝↔青全对 ΔE 16.3 双主题过 15。
   *   功耗卡「平均功耗」柱 = 第 1 个 series，自然顺位 palette[0] 蓝，不显式钉。
   *   趋势卡逐模型折线 = palette[0..4] 按 rank 取（蓝/青/绿/粉 + 第5 走 Other 折叠），
   *     跳过琥珀[1]（累计线语义槽，不充当数据系列色）。 */
  function _paletteColor(i, fallback) {
    var pal = IFCharts && IFCharts.palettes;
    var th = (IFCharts && typeof IFCharts.currentTheme === 'function')
      ? IFCharts.currentTheme() : 'dark';
    var p = (pal && pal[th]) || (pal && pal.dark) || [];
    return p[i] || fallback;
  }

  /* 「累积/存量」线色 = palette[1] 琥珀：跨卡（功耗↔Token）对齐。
   * 钉色三处：lineStyle.color（线描边）+ itemStyle.color（符号点）；
   * areaStyle 仅给 opacity 不设 color → ECharts 自动用 lineStyle.color 填充面积
   * （实测：设 lineStyle.color 后面积区像素 cyan=0、全琥珀）。 */
  function _cumColor() {
    return _paletteColor(1, '#b45309');
  }

  /* 趋势卡逐模型 rank→色：v7.0 统一走 PALETTES，跳过琥珀[1]（累计线专用槽）。
   * 返回 rank 0..3 对应的数据色数组 [蓝, 青, 绿,粉]，长度 4 = 同屏模型上限。 */
  function _dataColors() {
    var pal = IFCharts && IFCharts.palettes;
    var th = (IFCharts && typeof IFCharts.currentTheme === 'function')
      ? IFCharts.currentTheme() : 'dark';
    var p = (pal && pal[th]) || (pal && pal.dark) || [];
    return [p[0], p[2], p[3], p[4]].filter(Boolean);
  }

  /* Token 双卡 tooltip：柱 = 每桶 Prompt/Completion（token 量），
   * 折线 = 累积（对象式点位需解包 value）；云端累积以 ¥ 显示。 */
  function _tokenTooltip(sc) {
    return function (params) {
      if (!params || !params.length) return '';
      var rows = '';
      for (var i = 0; i < params.length; i++) {
        var p = params[i];
        if (p.seriesName === 'Prompt' || p.seriesName === 'Completion') {
          rows += '<div>' + p.seriesName + '：' + fmtTok(p.value || 0) + '</div>';
        } else {
          var vRaw = p.value;
          var v = (vRaw && typeof vRaw === 'object') ? vRaw.value : vRaw;
          rows += '<div>' + p.seriesName + '：' +
            (sc.scope === 'cloud' ? _yuanFmt(v == null ? 0 : v)
                                   : fmtTok(v == null ? 0 : v)) + '</div>';
        }
      }
      return '<div><b>' + params[0].axisValue + '</b></div>' + rows;
    };
  }

  /* 本地 / 云端双卡（左右）：同一粒度下各渲染一张双轴图。
   * 柱（左轴）= 每桶 Prompt/Completion 堆叠；折线（右轴）= 窗口起点累积——
   * 本地 cumTok（token 量）、云端 cumCost（费用 ¥）。
   * 重绘节流：热缓存（TOKEN_CURVE_TTL 5min 内）+ 同档 → 跳过重绘（3s 轮询下不闪）；
   *           fetch 落地 / 切档 / 切 tab → 强制重绘一次。与功耗卡 _pgranRendered 同构。 */
  function renderTokenChart() {
    ensureCharts();
    if (!_charts.tokenLocal || !_charts.tokenCloud) return;
    // 热缓存 + 同档 + 已用当前 cache.at 渲染过 → 数据没变，跳过重绘（3s 轮询不闪图）。
    // fetch 落地（cache.at 更新）/ 切档（_tokenGran 变）/ 切 tab → at 不匹配 → 强制重绘。
    var hit = _tokenCurveCache[_tokenGran];
    if (hit && (Date.now() - hit.at) < TOKEN_CURVE_TTL
        && _tokenRenderedAt[_tokenGran] === hit.at) return;
    if (hit) _tokenRenderedAt[_tokenGran] = hit.at;
    renderCacheHitBadges();

    var scopes = [
      { chart: _charts.tokenLocal, empty: 'monTokenLocalEmpty', scope: 'local',
        cumKey: 'cumTok', cumName: '累计 Token', heroId: 'monTokenLocalTotal',
        totalText: function (v) { return '窗口累计 ' + fmtTok(v); } },
      { chart: _charts.tokenCloud, empty: 'monTokenCloudEmpty', scope: 'cloud',
        cumKey: 'cumCost', cumName: '累计费用', heroId: 'monTokenCloudTotal',
        totalText: function (v) { return '窗口累计 ' + _yuanFmt(v); } },
    ];
    for (var i = 0; i < scopes.length; i++) {
      var sc = scopes[i];
      var data = buildTokenData(_tokenGran, sc.scope);
      var cum = data[sc.cumKey];
      var last = cum.length ? cum[cum.length - 1] : 0;
      // 卡头窗口累计读数（本地 = token 量，云端 = ¥；全 0 时显式给 0/¥0.00）
      var hero = $(sc.heroId);
      if (hero) hero.textContent = sc.totalText(last || 0);

      var hasData = false;
      for (var j = 0; j < data.prompt.length; j++) {
        if (data.prompt[j] > 0 || data.completion[j] > 0) { hasData = true; break; }
      }
      showEmpty(sc.empty, !hasData);

      // 左轴 tokens：max = 每桶堆叠峰值，_niceCeil 6 等分（与功耗卡右轴同手法——
      // max/n 恒为 {1,2,5}×10^k 干净步长，fmtTok 出 1.2M/500K 式标签）。
      // 右轴累积：max = 窗口累计总量，同样 _niceCeil；两轴同 splitNumber:6 + min:0
      // → 6 等分像素位置重合，右轴只印标签（splitLine 隐藏），一套尺度线（左轴的）。
      var maxStack = 0;
      for (var k = 0; k < data.prompt.length; k++) {
        var s = data.prompt[k] + data.completion[k];
        if (s > maxStack) maxStack = s;
      }
      var leftMax = _niceCeil(maxStack, 6);
      var rightMax = _niceCeil(last || 0, 6);
      // 累积线：全部桶逐桶打点（对象式点位，方案 B 与功耗卡一致）——前导空桶
      // 累积 0 也带点，线从窗口起点连续到末端总量
      var cumPts = cum.map(function (v) {
        return { value: v, symbol: 'circle', symbolSize: 5 };
      });

      IFCharts.update(sc.chart, {
        xAxis: { data: data.xs, boundaryGap: true },
        yAxis: [
          { name: 'tokens', min: 0, max: leftMax, splitNumber: 6, position: 'left',
            axisLabel: { formatter: function (v) { return fmtTok(v); } } },
          { name: sc.scope === 'cloud' ? '¥' : '累积', min: 0, max: rightMax, splitNumber: 6,
            splitLine: { show: false }, position: 'right',
            axisLabel: { formatter: function (v) {
              return sc.scope === 'cloud' ? _yuanFmt(v) : fmtTok(v); } } },
        ],
        tooltip: { trigger: 'axis', formatter: _tokenTooltip(sc) },
        legend: { data: ['Prompt', 'Completion', sc.cumName] },
        series: [
          { type: 'bar', name: 'Prompt', stack: 'tok', yAxisIndex: 0,
            data: data.prompt, barWidth: '55%', z: 2,
            itemStyle: { color: _paletteColor(0, '#2563eb') } }, // 分量柱=蓝[0]（跨卡与功耗柱同色）
          { type: 'bar', name: 'Completion', stack: 'tok', yAxisIndex: 0,
            data: data.completion, barWidth: '55%', z: 2,
            itemStyle: { color: _paletteColor(2, '#0891b2') } }, // 分量柱=青[2]：跳过琥珀[1] 让给累计线
          { type: 'line', name: sc.cumName, yAxisIndex: 1, data: cumPts, z: 3,
            lineStyle: { color: _cumColor() },   // 线描边钉琥珀（面积自动继承）
            itemStyle: { color: _cumColor() },   // 符号点钉琥珀：对齐功耗卡累计线（palette[1]）
            areaStyle: { opacity: 0.08 } },
        ],
      }, { dualAxis: true });
    }
  }

  /* ── 3. 模型延迟趋势：时间轴 × 逐模型分色折线（v6.0 取代模型条形双卡）──
   * 共享控制条：窗口 分钟/小时/天（latwin，minute/hour/day，数据仅 30d 不做周）
   * + 分位 P50/P50+P95（latq）+ 模型 chip 选择器（≤4 个在用模型，默认全选中；
   * 颜色按 rank 定，chip 与图同色）。
   * 数据源：GET /api/latency?window=（时间分桶 × 逐模型 TTFT/TPOT 分位，只读 display filter）。 */

  /* v7.0: 趋势卡逐模型 rank→色统一走 PALETTES 数据槽（跳过琥珀[1] 累计线专用槽）。
   * 同屏上限 = _dataColors() 长度（4，CVD 驱动）；第 5+ 模型折叠为"其他"。
   * 模型 1..4 按请求数降序分色 [蓝, 青, 绿, 粉]，与 chip 同序 → 同色；
   * dark/light 同 hex（v7.0 统一），不再各自一组。 */
  var _latWin = 'hour';            // minute | hour | day（数据仅 30d，无周档）
  var _latQ = 'p50';               // 'p50' | 'p50p95'
  var _latSel = null;              // 选中模型名数组（null=默认全部在用模型）
  var _latCache = {};              // window -> { data, at }
  var _latFetchInflight = {};
  var _LAT_TTL = { minute: 300000, hour: 300000, day: 300000 };   // 5min，与后端 _LAT_CACHE_TTL 逐档对齐
  var _latWinText = { minute: '近 60min', hour: '近 24h', day: '近 30 天' };   // 副标题窗口文案

  function _modelColors() {
    /* v7.0 统一调色板数据槽（跳过琥珀[1]）；_dataColors 已含主题守卫。 */
    var dc = _dataColors();
    return dc.length ? dc : ['#2563eb', '#0891b2', '#15803d', '#db2777'];
  }

  function getLatSeries(win) {
    var c = _latCache[win];
    var now = Date.now();
    if (c && now - c.at < (_LAT_TTL[win] || 60000)) return c.data;
    if (!_latFetchInflight[win]) {
      _latFetchInflight[win] = true;
      fetch('/api/latency?window=' + win, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _latCache[win] = { data: data || {}, at: Date.now() };
          if (isMonitorActive()) renderLatencyCards();
        })
        .catch(function (e) { console.warn('[monitor] /api/latency fetch failed:', e); })
        .then(function () { _latFetchInflight[win] = false; });
    }
    return c ? c.data : {};
  }

  function _latAvailable(seriesObj) { return Object.keys(seriesObj || {}); }  // 请求数降序

  // chip 列表 = 全部在用模型（后端已按请求数截断 ≤5 = 调色板长度，CVD 驱动）；默认全选中
  function _latChipModels(seriesObj) {
    return _latAvailable(seriesObj).slice(0, _modelColors().length);
  }

  function _latSelected(seriesObj) {
    var chips = _latChipModels(seriesObj);
    // 仅在有模型数据时才固化默认选中：冷启动首渲染（数据未到）不得把 _latSel
    // 从 null 固化为 []（[] 为 truthy → 数据到达后默认全选中不再触发）。
    // 用户主动全取消（_latSel=[]）不受影响：有 chips 且 _latSel 非 null 时不重设。
    if (!_latSel && chips.length) _latSel = chips.slice();
    var sel = chips.filter(function (n) { return _latSel.indexOf(n) >= 0; });
    return sel.slice(0, _modelColors().length);   // 安全网：不会超调色板长度
  }

  function _latColorOf(name, seriesObj) {
    var i = _latAvailable(seriesObj).indexOf(name);
    return i >= 0 ? _modelColors()[i] : '#888';   // 按 rank 定色：chip 与图同色，绝不循环
  }

  function _latBucketLabel(win) { return ({ minute: '5min', hour: '1h', day: '1d' })[win] || '1h'; }

  function _lowPt(v, n, color) {
    // 低置信（0<n<30）→ 半透明小圆点；n=0/无值 → null（断线）。
    // house 规则 series 级 symbol:'none'（无逐点标记）下，仅 itemStyle.opacity
    // 的 data item 不渲染任何东西——必须显式 symbol:'circle' 才可见（fix round 1）。
    if (v == null) return null;
    var low = n > 0 && n < 30;
    return low
      ? { value: v, symbol: 'circle', symbolSize: 6,
          itemStyle: { color: color, opacity: 0.45 } }
      : v;
  }

  function renderLatChips(seriesObj) {
    var el = $('monLatChips'); if (!el) return;
    var chips = _latChipModels(seriesObj);
    if (!chips.length) { el.innerHTML = '<span class="muted" style="font-size:11px">暂无模型</span>'; return; }
    var sel = _latSelected(seriesObj);
    var html = '';
    chips.forEach(function (name) {
      var on = sel.indexOf(name) >= 0;
      html += '<button type="button" class="mon-lat-chip' + (on ? ' on' : '') +
        '" data-model="' + escHtml(name) + '" style="--mc:' + _latColorOf(name, seriesObj) + '">' +
        '<span class="dot"></span>' + escHtml(shortName(name)) + '</button>';
    });
    el.innerHTML = html;
  }

  function _latTrendTooltip(prefix) {
    return function (params) {
      if (!params || !params.length) return '';
      var bucket = params[0].axisValue;
      var rows = '';
      params.forEach(function (p) {
        if (p.value == null || (typeof p.value === 'object' && p.value == null)) return;
        var v = (p.value && typeof p.value === 'object') ? p.value.value : p.value;
        // tooltip 值归一：TTFT 秒（2 位小数）、TPOT ms/token（2 位小数）
        var txt = prefix === 'ttft'
          ? (v / 1000).toFixed(2) + ' s'
          : Number(v).toFixed(2) + ' ms/tok';
        rows += '<div>' + escHtml(p.seriesName) + '：' + txt + '</div>';
      });
      return '<div><b>' + bucket + '</b></div>' + rows;
    };
  }

  function renderLatCard(metric) {
    var isTtft = metric === 'ttft';
    var prefix = isTtft ? 'ttft' : 'tpot';
    if (!_charts[metric]) return;
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    var buckets = (data && data.buckets) || [];
    var sel = _latSelected(seriesObj);

    var series = [];
    var legendData = [];
    sel.forEach(function (name) {
      var s = seriesObj[name] || {};
      var color = _latColorOf(name, seriesObj);   // rank→色，与 chip 一致
      var n = s[prefix + '_n'] || [];
      var p50 = (s[prefix + '_p50'] || []).map(function (v, bi) { return _lowPt(v, n[bi], color); });
      series.push({
        name: name, type: 'line', data: p50, connectNulls: true,   // 中间空桶桥接（视觉平滑，不造数据点）；首尾空不延伸
        lineStyle: { color: color, width: 2 },
        itemStyle: { color: color },
      });
      legendData.push(name);
      if (_latQ === 'p50p95') {
        var p95 = (s[prefix + '_p95'] || []).map(function (v, bi) { return _lowPt(v, n[bi], color); });
        series.push({
          name: name + ' P95', type: 'line', data: p95, connectNulls: true,
          lineStyle: { color: color, width: 1, type: 'dashed' },
          itemStyle: { color: color },
        });
      }
    });

    var hasData = sel.length > 0 && buckets.length > 0;
    showEmpty(isTtft ? 'monTtftEmpty' : 'monTpotEmpty', !hasData);
    // y 轴刻度简化：TTFT 量级 ~1k-16k ms → 归一秒（/1000，1 位小数，如 3.0 s）；
    // TPOT 量级 0-18 ms/token、间隔整数 → 整数刻度（0/3/6/9…，不留小数）。
    var yFormatter = isTtft
      ? (function (v) { return (v / 1000).toFixed(1) + ' s'; })
      : (function (v) { return Math.round(v) + ' ms/tok'; });
    IFCharts.update(_charts[metric], {
      xAxis: { type: 'category', data: buckets, boundaryGap: false,
               axisLabel: { fontSize: 11, interval: 'auto' } },
      yAxis: { type: 'value', axisLabel: { formatter: yFormatter, fontSize: 11 } },
      legend: { show: series.length >= 1, data: legendData, textStyle: { fontSize: 11 } },
      tooltip: { trigger: 'axis', formatter: _latTrendTooltip(prefix) },
      series: series,
    }, { replaceSeries: true });   // series 数随 chip/P95 收缩 → 整替，防幽灵 series 残留
  }

  function renderLatencyCards() {
    ensureCharts();
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    renderLatChips(seriesObj);
    var bl = _latBucketLabel(_latWin);
    var sub1 = $('monTtftSub'), sub2 = $('monTpotSub'), sub0 = $('monLatSub');
    var winTxt = _latWinText[_latWin] || '近 24h';
    if (sub1) sub1.textContent = 'ms · ' + winTxt + ' · 每 ' + bl;
    if (sub2) sub2.textContent = 'ms/token · ' + winTxt + ' · 每 ' + bl;
    if (sub0) sub0.textContent = '时间分桶 · 桶内 ' + (_latQ === 'p50p95' ? 'P50/P95' : 'P50');
    renderLatCard('ttft');
    renderLatCard('tpot');
  }

  /* ── 4. 九联 KPI（3 行 × 3 列）──
   * 前 6 卡：GET /api/engine_metrics?model=<active> → kv_cache_usage_perc /
   * running_batch(live 在途并发, 0..max_batch) / seq_length /
   * tpot_seconds.mean(→ms) / ttft_seconds.mean(→s) / throughput
   * 后 3 卡（v6.1）：store.get('metrics_24h')（request_log 聚合器，3s 快照轮询
   * 已灌入，无新端点）→ E2E tok/s（P50）/ Req Rate（RPS）/ Avg Out Len */
  function kpiTile(label, val, tip) {
    return '<div class="kpi" title="' + escHtml(tip) + '">' +
      '<span class="kpi-label">' + escHtml(label) + '</span>' +
      '<span class="kpi-val mono">' + escHtml(val) + '</span>' +
    '</div>';
  }

  function drawKpis(data) {
    var el = $('monKpis');
    if (!el) return;
    if (!data) {
      el.innerHTML = '<div class="if-empty">引擎指标暂不可用</div>';
      return;
    }
    // 休眠模型或无指标数据（running_batch 存在 = 引擎存活，即便 0 也不算休眠）
    if (data.sleep_state === 0 && data.kv_cache_usage_perc == null &&
        data.seq_length == null && data.throughput == null &&
        data.ttft_seconds == null && data.tpot_seconds == null &&
        data.running_batch == null) {
      el.innerHTML = '<div class="if-empty">模型休眠中，无实时指标 — 到推理 TAB 唤醒</div>';
      return;
    }

    var kv = data.kv_cache_usage_perc;
    var batch = data.running_batch;        // live 在途并发（引擎实时 running 数，idle 时为 0）
    var maxBatch = data.max_batch;         // 引擎上限 max_concurrency / max_num_seqs
    var seq = data.seq_length;
    var tpot = data.tpot_seconds;
    var ttft = data.ttft_seconds;
    var thr = data.throughput;

    // v6.1: 新增 3 卡来自 request_log 聚合器（metrics_24h 随 3s 快照灌入 store，
    // 无需新端点）；键 = active 模型名（与 engine_metrics 同源），缺键/无数据 → "—"。
    // 与引擎 6 卡数据源解耦：引擎卡走 _engineCache TTL，新 3 卡随快照刷新。
    var m24 = (store.get('metrics_24h') || {}).models || {};
    var mm = m24[_engineModel] || {};
    var e2e = mm.e2e_tps_p50;      // E2E tok/s（P50）
    var rps = mm.rps;              // 24h 窗口请求速率
    var avgOut = mm.avg_out_len;   // 单请求平均输出长度

    // 单位统一：TPOT → ms（2 位小数）；TTFT → 秒 s（2 位小数）。
    // Batch Size = 引擎实时在途并发请求数（live running gauge，idle 时为 0；上限 max_batch），
    // 非累计完成数。KV / Batch / Throughput 均取自引擎 /metrics（经 VllmMetricsCollector）。
    el.innerHTML =
      '<div class="mon-kpi-grid">' +
        kpiTile('KV Cache', kv != null ? Number(kv).toFixed(1) + '%' : '—',
          'KV 缓存占用率（来自引擎 /metrics）') +
        kpiTile('Batch Size',
          batch != null ? (maxBatch ? batch + ' / ' + maxBatch : String(batch)) : '—',
          '在途并发请求数（live，引擎实时 running 数；上限 max_concurrency）') +
        kpiTile('Seq Length', seq != null ? UI.fmtNum(seq) : '—',
          '平均请求序列长度（prompt + generation tokens）') +
        kpiTile('TPOT', tpot && tpot.mean != null ? (tpot.mean * 1000).toFixed(2) + 'ms' : '—',
          'Time Per Output Token — 每输出 token 生成耗时（ms，保留 2 位）') +
        kpiTile('TTFT', ttft && ttft.mean != null ? ttft.mean.toFixed(2) + 's' : '—',
          'Time To First Token — 首 token 延迟（秒，保留 2 位）') +
        kpiTile('Throughput', thr != null ? UI.fmtNum(Math.round(thr)) + ' tok/s' : '—',
          'EMA 平滑吞吐（tokens/s）') +
        kpiTile('E2E tok/s', e2e != null ? Number(e2e).toFixed(1) : '—',
          '端到端速率 P50 = 输出 tokens ÷ 请求总时长（24h 窗口；不依赖 TTFT，流式/非流式通用）') +
        kpiTile('Req Rate', rps != null ? Number(rps).toFixed(4) + ' req/s' : '—',
          '请求速率 RPS = 24h 窗口请求数 ÷ 窗口秒数') +
        kpiTile('Avg Out Len', avgOut != null ? UI.fmtNum(avgOut) + ' tok' : '—',
          '单请求平均输出长度（tokens，24h 窗口）') +
      '</div>';
  }

  function renderKpis() {
    var el = $('monKpis');
    if (!el) return;

    var now = Date.now();
    if (_engineCache && now - _lastEngineFetch < ENGINE_TTL) {
      drawKpis(_engineCache);
      return;
    }

    var active = store.get('active_services') || [];
    if (!active.length) {
      _engineCache = null;
      _engineModel = null;
      el.innerHTML = '<div class="if-empty">无活跃模型 — 到推理 TAB 启动一个</div>';
      var mLabel = $('monKpiModel');
      if (mLabel) mLabel.textContent = '';
      return;
    }
    var model = active[0];
    var mLabel = $('monKpiModel');
    if (mLabel) mLabel.textContent = shortName(model);

    // 模型未变且有缓存 → 直接用
    if (_engineModel === model && _engineCache && now - _lastEngineFetch < ENGINE_TTL) {
      drawKpis(_engineCache);
      return;
    }

    UI.skeleton(el, 3);
    // GET only — no method specified = GET (read-only constraint)
    fetch('/api/engine_metrics?model=' + encodeURIComponent(model), {
      cache: 'no-store',
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        _engineCache = data;
        _engineModel = model;
        _lastEngineFetch = Date.now();
        drawKpis(data);
      })
      .catch(function () {
        el.innerHTML = '<div class="if-empty">引擎指标暂不可用</div>';
      });
  }

  /* ── 5. 请求日志表 ── */
  function renderLogTable() {
    var el = $('monLogTable');
    if (!el) return;
    var logs = store.get('request_log') || [];
    if (!logs.length) {
      el.innerHTML = '<div class="if-empty">暂无请求日志 — 经代理发起请求后在此记录</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < logs.length; i++) {
      var l = logs[i] || {};
      var ts = l.timestamp
        ? new Date(l.timestamp * 1000).toLocaleTimeString('zh-CN',
            { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '—';
      var stCls = (l.status || 0) < 400 ? 'ok' : 'crit';
      var tokIn = UI.fmtNum(l.tokens_in) || '0';
      var tokOut = UI.fmtNum(l.tokens_out) || '0';
      // 缓存命中率（Cache Hit Rate）= tokens_in_cached / tokens_in；
      // 双协议统一口径（OpenAI/Anthropic 均由 normalize_usage 归一化）
      var cacheRate = '—';
      if (l.tokens_in > 0) {
        var _c = Math.min(l.tokens_in_cached || 0, l.tokens_in);
        cacheRate = (_c / l.tokens_in * 100).toFixed(1) + '%';
      }
      var ttft = l.ttft_ms != null ? l.ttft_ms.toFixed(0) + 'ms' : '—';
      var dur = l.duration_ms != null ? l.duration_ms.toFixed(0) + 'ms' : '—';
      rows += '<tr>' +
        '<td class="mono">' + escHtml(ts) + '</td>' +
        '<td>' + escHtml(shortName(l.model)) + '</td>' +
        '<td><span class="badge ' + stCls + '">' + escHtml(l.status) + '</span></td>' +
        '<td class="mono num">' + escHtml(tokIn + ' / ' + tokOut) + '</td>' +
        '<td class="mono num">' + escHtml(cacheRate) + '</td>' +
        '<td class="mono num">' + escHtml(ttft) + '</td>' +
        '<td class="mono num">' + escHtml(dur) + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>模型</th><th>状态</th>' +
        '<th>Tokens in/out</th>' +
        '<th title="Cache Hit Rate（缓存命中率）= tokens_in_cached / tokens_in；OpenAI 系取 prompt_tokens_details.cached_tokens，Anthropic 系取 cache_read_input_tokens，口径统一">缓存命中率</th>' +
        '<th>TTFT</th><th>耗时</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>';
    var tsEl = $('monLogTs');
    if (tsEl) tsEl.textContent = new Date().toLocaleTimeString('zh-CN',
      { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /* ── 6. 切换历史表 ── */
  function renderHistTable() {
    var el = $('monHistTable');
    if (!el) return;
    var hist = store.get('history') || [];
    if (!hist.length) {
      el.innerHTML = '<div class="if-empty">暂无切换历史 — 切换模型后在此记录</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < hist.length; i++) {
      var h = hist[i] || {};
      // history.timestamp 来自 SQLite CURRENT_TIMESTAMP 字符串（'YYYY-MM-DD HH:MM:SS'），
      // 非 epoch 数字 → 不能 *1000（NaN→Invalid Date）。兼容两种格式。
      var ts = h.timestamp
        ? new Date(typeof h.timestamp === 'number' ? h.timestamp * 1000 : h.timestamp)
            .toLocaleString('zh-CN',
              { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '—';
      var from = shortName(h.from);
      var to = shortName(h.to);
      var dur = h.duration != null ? UI.fmtDur(h.duration) : '—';
      var st = h.status || '';
      var stCls = st === 'ok' ? 'ok' : (st === 'error' || st === 'fail' ? 'crit' : 'info');
      rows += '<tr>' +
        '<td class="mono">' + escHtml(ts) + '</td>' +
        '<td>' + escHtml(from) + '</td>' +
        '<td>' + escHtml(to) + '</td>' +
        '<td class="mono">' + escHtml(dur) + '</td>' +
        '<td>' + (st ? '<span class="badge ' + stCls + '">' + escHtml(st) + '</span>' : '—') + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>From</th><th>To</th><th>耗时</th><th>状态</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>';
  }

  /* ── 7. （v6.4 删除：费用概览卡）──
   * 滚动 24h 费用无参考价值（用户 2026-09-25 拍板删除）。费用改由 Token 双卡的
   * 云端「累计费用 ¥」折线覆盖（随粒度：小时=24h / 天=30d / 周=90d，可看趋势）。
   * 本地无价格（只花电费，已在 功耗/电费 卡）。 */

  /* ── 客户端 / Agent 用量卡函数（v6.5，布局 A：图左表右） ── */
  function getAgentStats() {
    // 热缓存 → 数据没变，跳过重绘（3s 轮询不闪图；与 Token/功耗卡同构）。
    // fetch 落地（_agentCache 更新）/ 切档切 scope（清缓存）/ 切 tab → 强制重绘。
    if (_agentCache && Date.now() - _agentLast < _AGENT_TTL) return;
    if (_agentInflight) return;
    _agentInflight = true;
    fetch('/api/agent-stats?granularity=' + _agentCfg.gran + '&scope=' + _agentCfg.scope,
          { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { _agentCache = d; _agentLast = Date.now(); renderAgentCard(); })
      .catch(function (e) { console.warn('[monitor] /api/agent-stats failed:', e); })
      .then(function () { _agentInflight = false; });
  }

  function renderAgentTable(totals) {
    var el = $('monAgentTable'); if (!el) return;
    if (!totals || !totals.length) {
      el.innerHTML = '<div class="if-empty">暂无客户端数据——经代理发起请求后在此统计</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < totals.length; i++) {
      var t = totals[i];
      // 待认领 = 无真实 def 的行：source 为 observed（无 def 回退）或 historical（无信号）。
      // 这些行 name 回退为「未识别」/「历史（无信号）」但 agent id 不是 'unknown'，
      // 之前只判 agent==='unknown' 导致 smoke-tmp 等 observed 残留显示「未识别」却无按钮。
      var claimable = (t.agent === 'unknown') || (t.source === 'observed');
      var isUnk = claimable;  // 统一用 claimable 控制样式与按钮
      rows += '<tr class="' + (isUnk ? 'agent-unk-row' : '') + '">' +
        '<td><span class="agent-dot" style="background:' + escHtml(t.color) + '"></span>' +
          escHtml(t.name) + (isUnk ? ' <span class="agent-unk-tag">待认领</span>' : '') + '</td>' +
        '<td class="mono num">' + UI.fmtNum(t.requests) + '</td>' +
        '<td class="mono num">' + escHtml(t.success_rate.toFixed(0)) + '%</td>' +
        '<td class="mono num">' + UI.fmtNum(t.tokens_in) + '/' + UI.fmtNum(t.tokens_out) + '</td>' +
        '<td class="mono num">' + (t.cost_yuan > 0 ? _yuanFmt(t.cost_yuan) : '—') + '</td>' +
        '<td class="mono num">' + (t.ttft_p50 != null ? escHtml(t.ttft_p50.toFixed(0)) + 'ms' : '—') + '</td>' +
        '<td class="mono">' + escHtml((t.top_models || []).slice(0, 1).map(function (m) { return shortName(m.model); }).join('')) + '</td>' +
        '<td>' + (claimable ? '<button class="mon-mini-btn" data-claim="' + escHtml(t.agent) + '" title="识别/认领">识别</button>' : '') +
          (_agentManage && !claimable && t.source !== 'builtin'
             ? '<button class="mon-mini-btn" data-del="' + escHtml(t.agent) + '" title="删除">✕</button>' : '') + '</td>' +
      '</tr>';
    }
    el.innerHTML = '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl mon-agent-tbl">' +
        '<thead><tr><th>客户端</th><th>请求</th><th>成功</th><th>Tokens in/out</th>' +
        '<th>费用</th><th>TTFT P50</th><th>Top 模型</th><th></th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table></div>';
  }

  function _agentPatternFromUA(ua) {
    ua = ua || '';
    var pre = (ua.match(/^[A-Za-z0-9]+/) || [''])[0];
    return pre ? ('^' + pre) : '.*';
  }

  function renderAgentUnassigned(list) {
    var box = $('monAgentUnassigned'); if (!box) return;
    var badge = $('monAgentUnkBadge'), num = $('monAgentUnkNum');
    if (num) num.textContent = (list || []).length;
    if (badge) badge.style.display = (list && list.length) ? '' : 'none';
    if (!list || !list.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    var rows = '';
    for (var i = 0; i < list.length; i++) {
      var u = list[i];
      rows += '<div class="agent-unk-item"><span class="mono">' + escHtml(u.ua) +
        '</span><span class="agent-unk-count">' + UI.fmtNum(u.requests) + ' 次</span>' +
        '<button class="mon-mini-btn" data-claim-ua="' + escHtml(u.ua) + '">识别</button></div>';
    }
    box.innerHTML = '<div class="agent-unk-head">未识别来源（待认领）</div>' + rows;
    box.style.display = '';
  }

  function renderAgentCard() {
    if (!isMonitorActive()) return;
    var d = _agentCache; if (!d) { getAgentStats(); return; }
    var emptyEl = $('monAgentEmpty');
    var have = d.window_requests > 0;
    renderAgentTable(d.totals);
    renderAgentUnassigned(d.unassigned);
    if (!have) {
      if (emptyEl) emptyEl.style.display = '';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    ensureCharts();
    if (_charts.agent) {
      // 堆叠柱（每 Agent 一色）；scope=云端 → y 轴切每桶累计费用 ¥（设计 §5）
      // x 轴标签用桶起点 b.t（墙钟对齐，与 Token/功耗卡同口径），弃裸下标 x。
      var isCloud = _agentCfg.scope === 'cloud';
      var cats = [];
      if (d.totals.length && d.series[d.totals[0].agent]) {
        cats = d.series[d.totals[0].agent].map(function (b) {
          return _tokenGranLabel(_agentCfg.gran, (b.t != null ? b.t : 0) * 1000);
        });
      }
      var series = [], names = [];
      for (var i = 0; i < d.totals.length; i++) {
        var t = d.totals[i];
        var pts = (d.series[t.agent] || []).map(function (b) { return isCloud ? b.cost : b.requests; });
        series.push({ name: t.name, type: 'bar', stack: 'a', barWidth: '70%',
                      itemStyle: { color: t.color }, data: pts });
        names.push(t.name);
      }
      IFCharts.update(_charts.agent, {
        grid: { left: 40, right: 12, top: 24, bottom: 26 },
        tooltip: { trigger: 'axis' },
        legend: { show: true, top: 0, type: 'scroll', textStyle: { fontSize: 11 } },
        xAxis: { type: 'category', data: cats, boundaryGap: true,
                 axisLabel: { fontSize: 11, interval: 'auto' } },
        yAxis: { type: 'value', name: isCloud ? '费用 ¥' : '请求' },
        series: series,
      }, { replaceSeries: true });   // series 数随档/认领收缩 → 整替，防幽灵 series 残留
    }
  }

  /* ── 一键认领（未识别 → 用户目录落盘） ── */
  function bindAgentEvents() {
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('[data-claim], [data-claim-ua], [data-del]') : null;
      if (!btn) return;
      ev.preventDefault();
      if (btn.hasAttribute('data-claim-ua')) { openClaimModal(btn.getAttribute('data-claim-ua')); return; }
      if (btn.hasAttribute('data-claim')) {
        // data-claim 带 agent id（observed 残留如 smoke-tmp）或 'unknown'。
        // 取 unassigned 首个 UA 预填；observed 残留无 unassigned 条目时用 agent id 当样本。
        var claimAgent = btn.getAttribute('data-claim') || 'unknown';
        var sampleUa = (_agentCache && _agentCache.unassigned && _agentCache.unassigned[0])
          ? _agentCache.unassigned[0].ua : '';
        if (!sampleUa && claimAgent !== 'unknown') sampleUa = claimAgent;
        openClaimModal(sampleUa); return;
      }
      if (btn.hasAttribute('data-del')) { delAgent(btn.getAttribute('data-del')); return; }
    });
  }

  function openClaimModal(ua) {
    _claimSample = ua || '';
    var rid = _agentPatternFromUA(_claimSample);
    var base = (rid.length > 1 ? rid.slice(1) : 'agent').toLowerCase()
        .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'agent';
    // 归入已有 Agent 的候选列表（排除 unknown）
    var opts = '';
    if (_agentCache && _agentCache.totals) {
      for (var i = 0; i < _agentCache.totals.length; i++) {
        var t = _agentCache.totals[i];
        if (t.agent === 'unknown') continue;
        if (t.source === 'observed') continue;  // 无 def 的历史残留，不可归入
        opts += '<option value="' + escHtml(t.agent) + '">' + escHtml(t.name) + '</option>';
      }
    }
    var holder = $('confirmModal');
    holder.innerHTML =
      '<div class="if-modal-backdrop">' +
        '<div class="if-modal" role="dialog" aria-modal="true">' +
          '<div class="if-modal-title" id="claimTitle">识别客户端</div>' +
          '<div class="if-modal-body if-agent-claim">' +
            '<div class="agent-claim-sample mono">样本: ' + escHtml(_claimSample || '—') + '</div>' +
            '<label>归属 <select id="claimMode">' +
              '<option value="new">新建独立 Agent</option>' +
              '<option value="alias"' + (opts ? '' : ' disabled') + '>归入已有 Agent（子工具）</option>' +
            '</select></label>' +
            '<div id="claimNewFields">' +
              '<label>名称 <input type="text" id="claimName" value="' + escHtml(base) + '" maxlength="40"></label>' +
              '<label>标识 id <input type="text" id="claimId" value="' + escHtml(base) + '" placeholder="小写字母数字连字符" pattern="[a-z0-9-]{1,64}"></label>' +
            '</div>' +
            '<div id="claimAliasFields" style="display:none">' +
              '<label>归入 <select id="claimParent">' + opts + '</select></label>' +
              '<div class="agent-claim-hint">该 UA 将作为子工具归入选定 Agent，命中即计入其用量。</div>' +
            '</div>' +
            '<label>匹配来源 <select id="claimHeader">' +
              '<option value="user-agent">User-Agent</option>' +
              '<option value="x-app">x-app</option></select></label>' +
            '<label>匹配规则 <input type="text" id="claimPattern" value="' + escHtml(rid) + '"></label>' +
            '<div class="agent-claim-preview" id="claimPreview"></div>' +
          '</div>' +
          '<div class="if-modal-actions">' +
            '<button type="button" class="btn btn-sec" id="claimCancel">取消</button>' +
            '<button type="button" class="btn btn-pri" id="claimSave">创建并生效</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    holder.style.display = 'block';
    bindClaimEvents();
    updateClaimPreview();
  }

  function updateClaimPreview() {
    var pat = $('claimPattern'), pre = $('claimPreview');
    if (!pat || !pre) return;
    var msg, ok = false;
    try {
      ok = new RegExp(pat.value).test(_claimSample || '');
      msg = '实时预览: 「' + (_claimSample || '—') + '」 ' + (ok ? '→ ✓ 命中' : '→ ✗ 不命中');
    } catch (e) { msg = '实时预览: regex 无法编译'; }
    pre.textContent = msg;
    pre.style.color = ok ? 'var(--ok)' : 'var(--crit)';
  }

  function bindClaimEvents() {
    var c = $('claimCancel'), s = $('claimSave');
    if (c) c.addEventListener('click', closeClaimModal);
    if (s) s.addEventListener('click', submitClaim);
    var pat = $('claimPattern');
    if (pat) pat.addEventListener('input', updateClaimPreview);
    // 归属模式切换：新建 vs 归入已有（alias）
    var mode = $('claimMode');
    if (mode) mode.addEventListener('change', function () {
      var isAlias = mode.value === 'alias';
      var nf = $('claimNewFields'), af = $('claimAliasFields');
      var title = $('claimTitle'), save = $('claimSave');
      if (nf) nf.style.display = isAlias ? 'none' : '';
      if (af) af.style.display = isAlias ? '' : 'none';
      if (title) title.textContent = isAlias ? '归入已有 Agent' : '识别为新 Agent';
      if (save) save.textContent = isAlias ? '归入并生效' : '创建并生效';
    });
    var idInput = $('claimId');
    if (idInput) idInput.addEventListener('input', function () { idInput.dataset.touched = '1'; });
    var nameInput = $('claimName');
    if (nameInput) nameInput.addEventListener('input', function () {
      var ipt = $('claimId');
      if (ipt && !ipt.dataset.touched) {
        ipt.value = nameInput.value.toLowerCase()
          .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'agent';
      }
    });
  }

  function closeClaimModal() {
    var holder = $('confirmModal');
    if (holder) { holder.innerHTML = ''; holder.style.display = 'none'; }
    _claimSample = '';
  }

  function submitClaim() {
    var header = $('claimHeader').value, pattern = $('claimPattern').value;
    var mode = $('claimMode');
    var isAlias = mode && mode.value === 'alias';
    var body;
    if (isAlias) {
      var parent = $('claimParent').value;
      body = { parent_id: parent, header: header, pattern: pattern };
    } else {
      body = { id: $('claimId').value, name: $('claimName').value,
               header: header, pattern: pattern, color: '#94a3b8' };
    }
    fetch('/api/agents', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, UI.adminHeaders()),
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { UI.toast(res.d.error || '认领失败', 'error'); return; }
        // 后端已在响应前完成 reclassify（handler._handle_post_agents），
        // 这里立即 _agentCache=null 刷新读到的是重分类后的新数据，
        // 不会被 _AGENT_TTL(5min) 锁住旧面板。toast 带回重分类行数作反馈。
        var re = res.d.reclassified;
        var extra = (typeof re === 'number' && re > 0) ? ('，重分类 ' + re + ' 行') : '';
        UI.toast(isAlias ? ('已归入 ' + body.parent_id + '，热重载已生效' + extra)
                        : ('已创建 Agent ' + body.id + '，热重载已生效' + extra), 'ok');
        closeClaimModal();
        _agentCache = null; getAgentStats();
      });
  }

  function delAgent(id) {
    UI.confirm({
      title: '删除 Agent ' + id,
      body: '仅删除 ~/.inferfabric/agents.d/' + id + '.yaml（内置不可删）。历史请求行不受影响。',
      danger: true,
      onOk: function () {
        fetch('/api/agents?id=' + encodeURIComponent(id), {
          method: 'DELETE', headers: UI.adminHeaders(),
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.ok) { UI.toast('已删除 ' + id, 'ok'); _agentCache = null; getAgentStats(); }
            else { UI.toast(d.error || '删除失败', 'error'); }
          });
      },
    });
  }

  /* ── 渲染入口 ── */
  function renderMonitor() {
    renderPowerCard();
    renderTokenChart();
    getAgentStats();
    // 延迟趋势卡不随 3s snapshot 重渲染（数据源 /api/latency 有独立 5min TTL 缓存，
    // 数据不变时重画纯属浪费且让图闪）。延迟卡由 getLatSeries 的 TTL 节流：
    // TTL 过期才 fetch → 落地 renderLatencyCards()；命中缓存则不 fetch 不重绘。
    // 交互回调（窗口/chip/分位切换）+ 切回 tab（renderLatencyCards 强制一次）仍即时渲染。
    renderKpis();
    renderLogTable();
    renderHistTable();
  }
  window.tabRenderers['tab-monitor'] = renderMonitor;

  /* ── 订阅：sync_meta → 仅 monitor tab 活跃时刷新 ──
   * 功耗采样在服务端（PowerSampler 60s，页面关着历史也连续），前端零积累——
   * 不再有 GPU ring buffer 采样 push。 */
  store.on('sync_meta', function () {
    if (isMonitorActive()) renderMonitor();
  });

  store.on('tab_active', function (tab) {
    if (tab === 'tab-monitor') {
      // 切到 monitor tab：立即渲染（charts 可能需要 init）
      renderMonitor();
      // 延迟卡 + 功耗卡：TTL 缓存命中即时画 / 过期触发 fetch 落地后重绘
      renderLatencyCards();
      renderPowerCard();
      // Agent 卡：切 tab 时图实例可能已重建，即便 TTL 命中也强制重画一次
      if (_agentCache) renderAgentCard();
    }
  });

  /* ── 事件委托：窗口/粒度切换（display filter，不触达服务端） ── */
  var tabEl = $('tab-monitor');
  if (tabEl) {
    tabEl.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.mon-seg-btn');
      if (btn && tabEl.contains(btn)) {
        var seg = btn.closest('.mon-seg');
        if (seg) {
          var segType = seg.getAttribute('data-seg');

          // 更新 active 态
          seg.querySelectorAll('.mon-seg-btn').forEach(function (b) {
            b.classList.remove('active');
          });
          btn.classList.add('active');

          if (segType === 'pgran') {
            var pgran = btn.getAttribute('data-gran');
            if (pgran && pgran !== _pgran) {
              _pgran = pgran;
              _pgranRendered = null;
              renderPowerCard();
            }
          } else if (segType === 'gran') {
            var gran = btn.getAttribute('data-gran');
            if (gran && gran !== _tokenGran) {
              _tokenGran = gran;
              renderTokenChart();
            }
          } else if (segType === 'latwin') {
            var latWin = btn.getAttribute('data-win');
            if (latWin && latWin !== _latWin) {
              _latWin = latWin;
              _latSel = null;   // 切窗口后重置默认（新窗口全部在用模型）
              renderLatencyCards();
            }
          } else if (segType === 'latq') {
            var q = btn.getAttribute('data-q');
            if (q && q !== _latQ) {
              _latQ = q;
              renderLatencyCards();
            }
          }
        }
        return;
      }
      // chip（不在 .mon-seg 内）
      var chip = ev.target.closest('.mon-lat-chip');
      if (chip && tabEl.contains(chip)) {
        var m = chip.getAttribute('data-model');
        var latData = getLatSeries(_latWin) || {};
        if (!_latSel) _latSel = _latSelected((latData.series || {}));
        var i = _latSel.indexOf(m);
        if (i >= 0) _latSel.splice(i, 1); else _latSel.push(m);
        // 无超限分支：chip 已封顶 top 5，选中 ≤ chip 数 = 调色板长度
        renderLatencyCards();
        return;
      }
    });
  }

  /* ── 客户端 Agent 卡：粒度/范围 seg + 管理开关 + claim/删除事件 ── */
  document.querySelectorAll('[data-seg="aggran"] .mon-seg-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var host = b.parentNode;
      host.querySelectorAll('.mon-seg-btn').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      _agentCfg.gran = b.getAttribute('data-gran');
      _agentCache = null; getAgentStats();
    });
  });
  document.querySelectorAll('[data-seg="agscope"] .mon-seg-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var host = b.parentNode;
      host.querySelectorAll('.mon-seg-btn').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      _agentCfg.scope = b.getAttribute('data-scope');
      _agentCache = null; getAgentStats();
    });
  });
  var mBtn = $('monAgentManage');
  if (mBtn) {
    mBtn.addEventListener('click', function () {
      _agentManage = !_agentManage;
      mBtn.classList.toggle('active', _agentManage);
      if (_agentCache) renderAgentTable(_agentCache.totals);
    });
  }
  bindAgentEvents();

  /* ── 主题变更：IFCharts 自动 dispose+重建，但我们需要重新注入数据 ── */
  if (IFCharts && typeof IFCharts.onThemeChange === 'function') {
    IFCharts.onThemeChange(function () {
      // 重建后实例已更新，重新渲染所有图表注入数据；热缓存 guard 需复位否则被跳过
      if (isMonitorActive()) {
        _pgranRendered = null;
        _tokenRenderedAt = {};     // Token guard 复位（实例重建，强制重画）
        renderPowerCard();
        renderTokenChart();
        if (_agentCache) renderAgentCard();
        renderLatencyCards();
      }
    });
  }
})();
