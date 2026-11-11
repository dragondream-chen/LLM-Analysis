/*
 * [Role] Present native msmodeling jobs beside the browser's DS41 estimator.
 * [Invariants] A result retains its submitted configuration; failures never use
 * browser estimates as substitutes. Accepted output is independent of SLOs.
 * [Timeouts & Locks] One active job; bounded HTTP requests and 20-minute polling.
 * [Side-Effect Policy] Only explicit run/cancel actions submit or cancel jobs.
 */
(function () {
  'use strict';

  var API = '/api/msmodeling/v1';
  var REQUEST_TIMEOUT_MS = 30000;
  var JOB_TIMEOUT_MS = 20 * 60 * 1000;
  var POLL_MS = 1000;
  var TERMINAL = ['completed', 'failed', 'cancelled'];

  function mount() {
    var container = document.getElementById('ds41-page-performance');
    if (!container || document.getElementById('msmodeling-panel')) return;
    if (typeof DS41 === 'undefined' || typeof DS41.read !== 'function') return;

    var panel = element('section', 'msm-panel');
    panel.id = 'msmodeling-panel';
    panel.setAttribute('aria-labelledby', 'msm-heading');
    var title = element('h3', '', '本地 msmodeling · 固定 batch 仿真');
    title.id = 'msm-heading';
    panel.appendChild(title);
    panel.appendChild(element('p', 'msm-intro',
      '由本机服务执行 TensorCast，结果单独展示。下方原页面仍使用浏览器解析估计；本面板不替换原结果。'));

    var connection = element('div', 'msm-connection');
    var endpoint = input('msm-endpoint', 'url', 'http://127.0.0.1:8765');
    endpoint.autocomplete = 'off';
    endpoint.spellcheck = false;
    connection.appendChild(field('仿真服务地址', endpoint));
    var connect = button('连接并检查服务');
    connection.appendChild(connect);
    panel.appendChild(connection);
    var healthText = element('p', 'msm-engine', '尚未连接。先启动仓库中的本地仿真服务，再检查连接。');
    panel.appendChild(healthText);

    var controls = element('div', 'msm-controls');
    var batch = input('msm-batch', 'number', '1');
    batch.min = '1';
    batch.step = '1';
    controls.appendChild(field('每 DP 实例 batch', batch));
    var accepted = input('msm-accepted', 'number', '3');
    accepted.min = '1';
    accepted.max = '6';
    accepted.step = '0.1';
    accepted.setAttribute('aria-describedby', 'msm-accepted-help');
    controls.appendChild(field('D 每周期平均提交 token 数', accepted));
    panel.appendChild(controls);
    var acceptedHelp = element('p', 'msm-help');
    acceptedHelp.id = 'msm-accepted-help';
    panel.appendChild(acceptedHelp);

    var actions = element('div', 'msm-actions');
    var runP = button('运行纯 P · 固定 batch');
    var runD = button('运行纯 D · 固定 batch');
    var cancel = button('取消任务');
    var download = button('下载结果 JSON');
    [runP, runD, cancel, download].forEach(function (item) { actions.appendChild(item); });
    panel.appendChild(actions);
    var status = element('p', 'msm-status', '等待连接。');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    panel.appendChild(status);
    var stale = element('p', 'msm-stale');
    stale.hidden = true;
    panel.appendChild(stale);
    var output = element('div', 'msm-result');
    panel.appendChild(output);

    var assumptions = element('details', 'msm-assumptions');
    assumptions.appendChild(element('summary', '', '本次接入口径与限制'));
    assumptions.appendChild(element('p', '',
      '纯 P 使用原生全 40 层参考路径；Engram 使用 Host 放置。专家采用 W4A8，非专家采用 W8A8，和网页 checkpoint 的量化口径不同。'));
    assumptions.appendChild(element('p', '',
      '纯 D 在 input + (output − 1) / 2 的中点上下文估算单周期，再折算请求吞吐。该结果不包含请求排队、连续 batching 或动态接受长度。'));
    assumptions.appendChild(element('p', '',
      '网页的 CED、量化和放置选项不一定对应原生引擎执行方式。以结果的 effective 和 warnings 为准；解析仿真结果需要真机校准。'));
    assumptions.appendChild(element('p', '',
      'GitHub Pages 只托管静态页面。本机服务需要保持运行，并允许此网页来源访问；浏览器提示访问本地网络时需要允许。也可从本地服务打开同源页面。'));
    panel.appendChild(assumptions);
    container.insertBefore(panel, container.firstChild);

    var health = null;
    var healthBase = null;
    var active = null;
    var latest = null;
    var connecting = false;
    var submitting = false;
    var cancelling = false;
    var draftAccepted = '3';
    var lastMode = null;

    function getBase() {
      var url = new URL(endpoint.value.trim());
      if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash) {
        throw new Error('服务地址需为不含账号、查询参数或片段的 HTTP / HTTPS 地址。');
      }
      return url.href.replace(/\/+$/, '');
    }

    function configSnapshot() {
      return JSON.parse(JSON.stringify(DS41.read()));
    }

    function tell(message, kind) {
      status.textContent = message;
      status.dataset.state = kind || 'info';
    }

    function updateControls() {
      var mode = configSnapshot().decode;
      var busy = submitting || !!active;
      if (mode !== lastMode) {
        if (lastMode === 'dspark') draftAccepted = accepted.value;
        accepted.value = mode === 'ar' ? '1' : draftAccepted;
        lastMode = mode;
      }
      accepted.disabled = mode === 'ar' || (active && active.phase === 'p') || submitting;
      acceptedHelp.textContent = mode === 'ar'
        ? '当前为 AR：每周期提交 1 token。纯 P 不使用此参数。'
        : '当前为 DSpark：请输入独立测得或假设的平均提交数（包含 anchor / bonus，1–6）；默认 3。纯 P 不使用此参数。';
      var ready = health && health.ready && endpointMatchesHealth();
      connect.disabled = connecting;
      runP.disabled = busy || !ready;
      runD.disabled = busy || !ready;
      batch.disabled = busy;
      cancel.disabled = !active || !active.id || cancelling;
      download.disabled = !latest;
      if (latest) {
        var changed = JSON.stringify(configSnapshot()) !== JSON.stringify(latest.request.config);
        stale.hidden = !changed;
        stale.textContent = '当前网页参数已变化。下方保留上次提交时的结果与配置快照；重新运行后才会更新。';
      }
    }

    function endpointMatchesHealth() {
      try { return getBase() === healthBase; } catch (_) { return false; }
    }

    async function request(base, path, options, signal) {
      var controller = new AbortController();
      var timedOut = false;
      var timer = setTimeout(function () { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
      var abort = function () { controller.abort(); };
      if (signal) {
        if (signal.aborted) controller.abort();
        signal.addEventListener('abort', abort, { once: true });
      }
      try {
        var response = await fetch(base + API + path, Object.assign({
          credentials: 'omit', cache: 'no-store'
        }, options || {}, { signal: controller.signal }));
        var body = await response.text();
        var value;
        try { value = JSON.parse(body); } catch (_) {
          throw new Error('服务返回非 JSON 响应（HTTP ' + response.status + '）：' + body.slice(0, 1000));
        }
        if (!response.ok) {
          var error = value.error || value.reason || value.message || body;
          throw new Error('HTTP ' + response.status + '：' + (typeof error === 'string' ? error : JSON.stringify(error)));
        }
        return value;
      } catch (error) {
        if (timedOut) throw new Error('服务请求超过 30 秒。' + (options && options.method === 'POST'
          ? '服务端操作状态未知，请检查服务日志；不会自动重试。' : '请检查服务状态。'));
        throw error;
      } finally {
        clearTimeout(timer);
        if (signal) signal.removeEventListener('abort', abort);
      }
    }

    async function checkConnection() {
      connecting = true;
      health = null;
      healthBase = null;
      tell('正在检查本地服务…');
      updateControls();
      try {
        var base = getBase();
        var response = await request(base, '/health');
        if (!endpointMatches(base)) return;
        health = response;
        healthBase = base;
        var engine = response.engine || {};
        healthText.textContent = '引擎版本：' + (engine.revision || '未提供') + '；Python：' + (engine.python || '未提供');
        if (response.warnings && response.warnings.length) healthText.textContent += '；' + response.warnings.join('；');
        tell(response.ready ? '服务就绪。使用网页当前 DeepSeek 参数提交固定 batch 仿真。'
          : '服务未就绪：' + (response.reason || '请查看本地服务输出。'), response.ready ? 'success' : 'error');
      } catch (error) {
        tell('连接失败：' + error.message + '。请确认服务已启动、地址正确，并检查浏览器本地网络权限及服务 CORS 来源配置。', 'error');
        healthText.textContent = '未连接到可用的 msmodeling 服务。';
      } finally {
        connecting = false;
        updateControls();
      }
    }

    function endpointMatches(base) {
      try { return getBase() === base; } catch (_) { return false; }
    }

    function waitForPoll(signal) {
      return new Promise(function (resolve, reject) {
        var timer = setTimeout(done, POLL_MS);
        function done() { signal.removeEventListener('abort', aborted); resolve(); }
        function aborted() { clearTimeout(timer); reject(new Error('已停止等待任务。')); }
        signal.addEventListener('abort', aborted, { once: true });
        if (signal.aborted) aborted();
      });
    }

    async function run(phase) {
      if (active || submitting) return;
      var context;
      try {
        var batchValue = Number(batch.value);
        if (!Number.isSafeInteger(batchValue) || batchValue < 1) throw new Error('batch 必须为正整数。');
        var config = configSnapshot();
        var acceptedValue = phase === 'p' || config.decode === 'ar' ? 1 : Number(accepted.value);
        if (!Number.isFinite(acceptedValue) || acceptedValue < 1 || acceptedValue > 6) {
          throw new Error('每周期平均提交 token 数必须在 1–6 之间。');
        }
        var base = getBase();
        if (!health || !health.ready || healthBase !== base) throw new Error('请先连接并检查服务。');
        context = {
          base: base, phase: phase, startedAt: Date.now(), controller: new AbortController(),
          request: { config: config, phase: phase, batch: batchValue, acceptedTokensPerCycle: acceptedValue }
        };
      } catch (error) { tell(error.message, 'error'); return; }
      submitting = true;
      tell('正在提交纯 ' + phase.toUpperCase() + ' 仿真…' + (latest ? '下方暂时保留上次结果。' : ''));
      updateControls();
      try {
        var job = await request(context.base, '/jobs', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(context.request)
        }, context.controller.signal);
        if (!job.id) throw new Error('服务没有返回任务 ID；提交状态未知，请检查服务日志。');
        context.id = job.id;
        active = context;
        submitting = false;
        updateControls();
        while (TERMINAL.indexOf(job.status) === -1) {
          if (Date.now() - context.startedAt > JOB_TIMEOUT_MS) {
            throw new Error('等待已超过 20 分钟，停止轮询。服务端任务状态未知；任务 ID：' + context.id + '。请检查服务日志后再操作。');
          }
          if (job.status !== 'queued' && job.status !== 'running') throw new Error('服务返回未知任务状态：' + job.status);
          tell('任务 ' + context.id + '：' + (job.status === 'queued' ? '排队中' : '正在仿真') + '…');
          await waitForPoll(context.controller.signal);
          job = await request(context.base, '/jobs/' + encodeURIComponent(context.id), null, context.controller.signal);
        }
        latest = {
          service: context.base, submittedAt: new Date(context.startedAt).toISOString(),
          request: context.request, job: job
        };
        render(latest);
        if (job.status === 'completed' && job.result) tell('任务完成。下方为 msmodeling 返回的仿真结果。', 'success');
        else if (job.status === 'cancelled') tell('任务已取消。', 'info');
        else tell('仿真失败：' + (job.error ? errorText(job.error) : '服务未返回结果或错误详情。'), 'error');
      } catch (error) {
        tell('仿真未完成：' + error.message + (context.id
          ? '（任务 ' + context.id + '，服务端最终状态尚未确认）'
          : '。提交状态尚未确认，请检查服务日志后再操作；不会自动重试。'), 'error');
      } finally {
        context.controller.abort();
        active = null;
        submitting = false;
        cancelling = false;
        updateControls();
      }
    }

    async function cancelJob() {
      if (!active || !active.id || cancelling) return;
      var context = active;
      cancelling = true;
      updateControls();
      try {
        await request(context.base, '/jobs/' + encodeURIComponent(context.id) + '/cancel', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        if (active === context) tell('取消请求已提交，等待服务确认任务最终状态…');
      } catch (error) {
        tell('取消失败：' + error.message + '。仍继续查询任务最终状态。', 'error');
      } finally {
        cancelling = false;
        updateControls();
      }
    }

    function render(snapshot) {
      output.replaceChildren();
      var job = snapshot.job;
      var result = job.result;
      output.appendChild(element('h4', '', 'msmodeling 结果 · ' + snapshot.request.phase.toUpperCase() +
        ' · batch/DP=' + snapshot.request.batch));
      output.appendChild(element('p', 'msm-help', '提交时间：' + new Date(snapshot.submittedAt).toLocaleString() +
        '；任务 ID：' + job.id + '；状态：' + job.status));
      if (result) {
        var grid = element('dl', 'msm-metrics');
        var isP = snapshot.request.phase === 'p';
        metric(grid, isP ? 'P 单批耗时' : 'D 单周期耗时', seconds(isP ? result.latencySeconds : result.cycleSeconds));
        if (!isP) metric(grid, '平均每输出 token 耗时', seconds(result.tpotSeconds));
        metric(grid, '请求吞吐', number(result.requestQps, 3) + ' req/s');
        metric(grid, isP ? '输入吞吐' : '输出吞吐', number(result.tokensPerSecond, 2) + ' token/s');
        metric(grid, '峰值设备显存', gib(result.peakMemoryGiB));
        metric(grid, '显存预算检查', result.memoryFeasible ? '满足预算' : '超出预算 · 配置不可行');
        metric(grid, '页面时延上限检查', result.sloFeasible ? '满足设置上限' : '超过设置上限');
        metric(grid, '权重 / KV / 激活', gib(result.weightsGiB) + ' / ' + gib(result.kvGiB) + ' / ' + gib(result.activationsGiB));
        if (typeof result.hostMemoryGiB === 'number') metric(grid, 'Engram Host 表容量', gib(result.hostMemoryGiB));
        if (typeof result.hostPeakMemoryMiB === 'number') metric(grid, '本机仿真峰值 RAM', number(result.hostPeakMemoryMiB, 1) + ' MiB');
        if (typeof result.simulatorWallSeconds === 'number') metric(grid, '本机引擎执行耗时', seconds(result.simulatorWallSeconds));
        if (!isP) metric(grid, '每周期平均提交', number(result.acceptedTokensPerCycle, 3) + ' token');
        output.appendChild(grid);
        output.appendChild(element('p', 'msm-engine', '执行路径：' + (result.executionPath || '未提供') +
          '；引擎版本：' + ((result.engine || {}).revision || '未提供')));
        if (result.warnings && result.warnings.length) {
          var warnings = element('ul', 'msm-warnings');
          result.warnings.forEach(function (warning) { warnings.appendChild(element('li', '', String(warning))); });
          output.appendChild(warnings);
        }
        var links = element('div', 'msm-artifacts');
        artifact(links, result.traceUrl, '打开仿真时间线 Trace', snapshot.service);
        artifact(links, result.logUrl, '查看原始运行日志', snapshot.service);
        output.appendChild(links);
        if (result.operators && result.operators.length) {
          var details = element('details');
          details.appendChild(element('summary', '', '算子总耗时分解（求和不一定等于多流关键路径）'));
          var wrap = element('div', 'msm-table-wrap');
          var table = element('table');
          var head = element('thead');
          var header = element('tr');
          ['算子', '调用次数', '累计耗时'].forEach(function (text) {
            var th = element('th', '', text); th.scope = 'col'; header.appendChild(th);
          });
          head.appendChild(header);
          table.appendChild(head);
          var body = element('tbody');
          result.operators.forEach(function (operator) {
            var row = element('tr');
            [operator.name, number(operator.calls, 0), seconds(operator.totalSeconds)].forEach(function (text) {
              row.appendChild(element('td', '', String(text)));
            });
            body.appendChild(row);
          });
          table.appendChild(body);
          wrap.appendChild(table);
          details.appendChild(wrap);
          output.appendChild(details);
        }
      }
      if (job.error) output.appendChild(element('pre', 'msm-error', errorText(job.error)));
      var configDetails = element('details');
      configDetails.appendChild(element('summary', '', '查看提交快照与引擎实际配置 effective'));
      configDetails.appendChild(element('pre', 'msm-json', JSON.stringify({
        request: snapshot.request, effective: result && result.effective, service: snapshot.service
      }, null, 2)));
      output.appendChild(configDetails);
    }

    function artifact(container, path, label, base) {
      if (!path) return;
      var url;
      try { url = new URL(path, base + '/'); } catch (_) {
        container.appendChild(element('span', 'msm-help', label + '：服务返回了无效链接。'));
        return;
      }
      if (!/^https?:$/.test(url.protocol)) {
        container.appendChild(element('span', 'msm-help', label + '：服务返回的链接协议不受支持。'));
        return;
      }
      var link = element('a', '', label);
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      container.appendChild(link);
    }

    connect.addEventListener('click', checkConnection);
    runP.addEventListener('click', function () { run('p'); });
    runD.addEventListener('click', function () { run('d'); });
    cancel.addEventListener('click', cancelJob);
    endpoint.addEventListener('input', function () {
      health = null;
      healthBase = null;
      healthText.textContent = '服务地址已修改，请重新检查连接。正在运行的任务继续使用原服务地址。';
      updateControls();
    });
    accepted.addEventListener('input', function () { if (lastMode === 'dspark') draftAccepted = accepted.value; });
    document.addEventListener('change', function (event) { if (!panel.contains(event.target)) updateControls(); });
    download.addEventListener('click', function () {
      if (!latest) return;
      var blob = new Blob([JSON.stringify(latest, null, 2) + '\n'], { type: 'application/json;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var anchor = element('a');
      anchor.href = url;
      anchor.download = 'msmodeling-' + latest.request.phase + '-' + latest.submittedAt.replace(/[:.]/g, '-') + '.json';
      anchor.click();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
    window.addEventListener('pagehide', function () {
      if (active) active.controller.abort();
    });
    updateControls();
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function input(id, type, value) {
    var node = element('input'); node.id = id; node.type = type; node.value = value; return node;
  }
  function field(label, control) {
    var wrapper = element('label', 'msm-field');
    wrapper.htmlFor = control.id;
    wrapper.appendChild(element('span', '', label));
    wrapper.appendChild(control);
    return wrapper;
  }
  function button(label) {
    var node = element('button', '', label); node.type = 'button'; return node;
  }
  function metric(container, label, value) {
    var group = element('div');
    group.appendChild(element('dt', '', label));
    group.appendChild(element('dd', '', value));
    container.appendChild(group);
  }
  function number(value, digits) {
    return typeof value === 'number' && Number.isFinite(value)
      ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : '—';
  }
  function seconds(value) { return typeof value === 'number' && Number.isFinite(value) ? number(value * 1000, 3) + ' ms' : '—'; }
  function gib(value) { return typeof value === 'number' && Number.isFinite(value) ? number(value, 3) + ' GiB' : '—'; }
  function errorText(error) { return typeof error === 'string' ? error : JSON.stringify(error, null, 2); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
}());
