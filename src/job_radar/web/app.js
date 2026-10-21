(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  let boot = { defaults: {}, options: {}, templates: [], runs: [], auth: {}, csrf_token: '' };
  let activeRun = null;
  let poller = null;
  const countLabels = { requests: '采集调用', pages: '页', pairs_visited: '已访问搜索组合', pairs_completed: '已读完搜索组合', listed: '读取岗位', unique: '去重岗位', matched: '符合条件', details_requested: '请求 JD', details_fetched: '已补 JD', details_empty: 'JD 无正文' };
  const api = async (url, options = {}) => {
    const response = await fetch(url, { headers: { 'Content-Type': 'application/json', 'X-Job-Radar-Token': boot.csrf_token || '', ...(options.headers || {}) }, ...options });
    if (!response.ok) {
      let error = '';
      try { error = (await response.json()).error || ''; } catch (_) { error = await response.text(); }
      throw new Error(error || `请求失败（${response.status}）`);
    }
    return response.json();
  };
  const split = (value) => value.split(/[\n,，]/).map(x => x.trim()).filter(Boolean);
  const toast = (text) => { const el = $('#toast'); el.textContent = text; el.classList.add('show-toast'); setTimeout(() => el.classList.remove('show-toast'), 2500); };
  const option = (value, label = value) => `<option value="${escape(value)}">${escape(label)}</option>`;
  const escape = (value = '') => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function checkList(container, values, name, selected = []) {
    const wanted = new Set(selected || []);
    $(container).innerHTML = (values || []).map(value => `<label class="chip"><input type="checkbox" name="${name}" value="${escape(value)}" ${wanted.has(value) ? 'checked' : ''}>${escape(value)}</label>`).join('');
  }
  function setSelect(id, values, selected = '', blank = '不限') {
    const el = $(`#${id}`); if (!el) return;
    el.innerHTML = option('', blank) + (values || []).map(v => option(v)).join(''); el.value = selected || '';
  }
  function renderFilters(options, selected = {}) {
    const fields = [['salary', '薪资'], ['experience', '经验'], ['education', '学历'], ['industry', '行业'], ['scale', '公司规模'], ['stage', '融资阶段'], ['job_type', '岗位类型']];
    $('#filterSelects').innerHTML = fields.map(([key, label]) => `<label>${label}<select id="filter-${key}"></select></label>`).join('');
    fields.forEach(([key]) => setSelect(`filter-${key}`, options[key], selected[key]));
    checkList('#welfareChoices', options.welfare, 'welfare', selected.welfare);
  }
  function renderFields(fields, selected) {
    const chosen = new Set(selected || []);
    $('#fieldChoices').innerHTML = Object.entries(fields || {}).map(([key, label]) => `<label><input type="checkbox" value="${escape(key)}" ${chosen.has(key) ? 'checked' : ''}>${escape(label)}</label>`).join('');
  }
  function renderFormats(formats) {
    const selected = new Set(formats || ['xlsx']);
    $('#formatChoices').innerHTML = [['xlsx', 'Excel'], ['csv', 'CSV'], ['json', 'JSON']].map(([v, label]) => `<label class="chip"><input type="checkbox" value="${v}" ${selected.has(v) ? 'checked' : ''}>${label}</label>`).join('');
  }
  function numberValue(id, fallback) { const value = Number($(`#${id}`).value); return Number.isFinite(value) ? value : fallback; }
  function configFromForm() {
    return {
      name: $('#taskName').value.trim() || '未命名任务', keywords: split($('#keywords').value), cities: $$('input[name="city"]:checked').map(x => x.value), details: $('#details').value,
      filters: { salary: $('#filter-salary').value, experience: $('#filter-experience').value, education: $('#filter-education').value, industry: $('#filter-industry').value, scale: $('#filter-scale').value, stage: $('#filter-stage').value, job_type: $('#filter-job_type').value, welfare: $$('input[name="welfare"]:checked').map(x => x.value), include: split($('#include').value), exclude: split($('#exclude').value), exclude_internships: $('#excludeInternships').checked, exclude_hourly_daily: $('#excludeHourly').checked, unknown: $('#unknown').value },
      limits: { pages: numberValue('pages', 3), requests: numberValue('requests', 30), details: numberValue('detailLimit', 20), jobs: numberValue('jobs', 200), seconds: numberValue('seconds', 600) },
      export: { formats: $$('#formatChoices input:checked').map(x => x.value), fields: $$('#fieldChoices input:checked').map(x => x.value), scope: $('#exportScope').value }
    };
  }
  function fillForm(config = {}) {
    const d = boot.defaults || {}, c = { ...d, ...config }, f = { ...(d.filters || {}), ...(config.filters || {}) }, l = { ...(d.limits || {}), ...(config.limits || {}) }, ex = { ...(d.export || {}), ...(config.export || {}) };
    $('#taskName').value = c.name || ''; $('#keywords').value = (c.keywords || []).join('\n'); $('#details').value = c.details || 'matched';
    checkList('#cityChoices', boot.options.cities || [], 'city', c.cities || []); renderFilters(boot.options, f); renderFormats(ex.formats); renderFields(boot.options.fields, ex.fields);
    $('#include').value = (f.include || []).join('\n'); $('#exclude').value = (f.exclude || []).join('\n'); $('#excludeInternships').checked = !!f.exclude_internships; $('#excludeHourly').checked = !!f.exclude_hourly_daily; $('#unknown').value = f.unknown || 'keep';
    ['pages', 'requests', 'details', 'jobs', 'seconds'].forEach(k => $(`#${k === 'details' ? 'detailLimit' : k}`).value = l[k] ?? { pages: 3, requests: 30, details: 20, jobs: 200, seconds: 600 }[k]); $('#exportScope').value = ex.scope || 'matched';
  }
  function renderTemplates(templates) { $('#templates').innerHTML = '<option value="">载入已保存模板…</option>' + templates.map(t => `<option value="${escape(t.id)}">${escape(t.name)}</option>`).join(''); }
  function renderRunHistory(runs, chosen = activeRun?.id) { $('#runHistory').innerHTML = runs.length ? runs.map(run => `<option value="${escape(run.id)}">${escape(`${run.config?.name || '未命名任务'}${run.demo ? ' · 合成示例' : ''} · ${run.created_at || ''} · ${statusText(run)}`)}</option>`).join('') : '<option value="">暂无运行记录</option>'; $('#runHistory').value = chosen || runs[0]?.id || ''; }
  async function refreshRuns(chosen) { const data = await api('/api/runs'); boot.runs = data.runs || []; renderRunHistory(boot.runs, chosen); }
  function statusText(run) { const labels = { queued: '等待开始', running: '正在采集', completed: '采集完成', partial: '部分完成', stopped: '已停止', budget_stopped: '达到预算上限', risk_stopped: '因风险控制停止', auth_required: '需要重新登录', failed: '采集失败' }; return labels[run.status] || run.status; }
  function isEditingResults() { const focused = document.activeElement; return focused?.matches?.('.job-notes, .job-status'); }
  function renderRun(run, jobs = [], exports = [], preserveJobs = false) {
    activeRun = run; $('#resultTitle').textContent = run ? `${run.config?.name || '本次任务'} · ${statusText(run)}` : '还没有开始采集';
    if (run) { boot.runs = boot.runs.some(item => item.id === run.id) ? boot.runs.map(item => item.id === run.id ? run : item) : [run, ...boot.runs]; renderRunHistory(boot.runs, run.id); }
    const summary = run?.counts ? Object.entries(run.counts).map(([k, v]) => `${countLabels[k] || k} ${v}`).join(' · ') : '';
    const el = $('#runStatus'); el.className = `run-status ${['failed', 'auth_required', 'risk_stopped'].includes(run?.status) ? 'error' : ''}`; el.textContent = run ? [run.message || statusText(run), summary].filter(Boolean).join(' · ') : '设置好条件后开始。你可以先体验合成示例，熟悉岗位卡片和人工状态。';
    $('#stopRun').disabled = !['queued', 'running'].includes(run?.status); $('#resumeRun').disabled = !['stopped', 'partial', 'budget_stopped', 'failed', 'auth_required', 'risk_stopped'].includes(run?.status);
    $('#exportRun').disabled = !run?.id || ['queued', 'running'].includes(run?.status) || !!run?.export_pending;
    if (run?.demo) el.textContent = `合成示例（不连接 BOSS，不代表真实岗位）· ${el.textContent}`;
    if (run?.export_pending) el.textContent = `${el.textContent} · 正在生成导出文件`;
    const context = $('#runContext'); const words = run?.config?.keywords?.join('、') || '未设置'; const cities = run?.config?.cities?.join('、') || '不限城市'; context.textContent = run ? `本次条件：关键词 ${words} ｜ 城市 ${cities} ｜ 共 ${(run.config?.keywords?.length || 0) * (run.config?.cities?.length || 0)} 个搜索组合` : ''; context.hidden = !run;
    const bar = $('#exportBar'); bar.hidden = !exports.length; bar.innerHTML = exports.map(x => `<a href="${escape(x.url)}" download>${escape(x.name)}</a>`).join('');
    if (preserveJobs) return;
    const body = $('#jobsBody'); body.innerHTML = ''; if (!jobs.length) { body.innerHTML = '<tr class="empty-row"><td colspan="6">本次还没有可显示的岗位</td></tr>'; return; }
    jobs.forEach(job => body.appendChild(jobElement(job)));
  }
  function jobElement(job) {
    const row = $('#jobRow').content.firstElementChild.cloneNode(true);
    const link = $('.job-title', row); link.textContent = job.title || '未命名岗位'; if (job.url) link.href = job.url; else { link.removeAttribute('href'); link.removeAttribute('target'); }
    $('.job-meta', row).textContent = [job.city, job.education, job.job_type].filter(Boolean).join(' · '); $('.salary', row).textContent = job.salary || '—'; $('.experience', row).textContent = job.experience || '—'; $('.company', row).textContent = job.company || '—';
    const pill = $('.match-pill', row); pill.textContent = job.matched ? '符合条件' : (job.detail_status === 'pending' ? '待补 JD' : '已筛除'); if (!job.matched) pill.classList.add('no'); $('.filter-reason', row).textContent = job.filter_reason || '';
    const details = { not_requested: '未选择采集 JD', pending: '待采集 JD', skipped: '初筛未通过', fetched: '已采集 JD', empty: '详情未返回正文', budget_skipped: 'JD 因预算未采集' }; const detailLabel = details[job.detail_status] || job.detail_status || '未选择采集 JD'; const jd = $('.jd-content', row); jd.textContent = job.description || detailLabel; $('.jd-toggle', row).textContent = job.description ? '查看 JD' : detailLabel; $('.jd-toggle', row).addEventListener('click', () => { jd.hidden = !jd.hidden; $('.jd-toggle', row).textContent = jd.hidden ? (job.description ? '查看 JD' : detailLabel) : '收起 JD'; });
    const select = $('.job-status', row); select.value = job.status || '待看'; const notes = $('.job-notes', row); notes.value = job.notes || '';
    const save = async () => { try { await api(`/api/jobs/${encodeURIComponent(job.job_key)}/status`, { method: 'POST', body: JSON.stringify({ status: select.value, notes: notes.value }) }); toast('状态已保存；重新导出后会写入最新备注。'); } catch (err) { toast(err.message); } };
    select.addEventListener('change', save); notes.addEventListener('blur', save); return row;
  }
  async function openRun(id, fromPoll = false) { try { const data = await api(`/api/runs/${encodeURIComponent(id)}`); renderRun(data.run, data.jobs, data.exports, fromPoll && activeRun?.id === data.run.id && isEditingResults()); $('#runHistory').value = data.run.id; if (['queued', 'running'].includes(data.run.status) || data.run.export_pending) startPolling(); else stopPolling(); } catch (err) { toast(err.message); } }
  function startPolling() { stopPolling(); poller = setInterval(() => activeRun?.id && openRun(activeRun.id, true), 2500); }
  function stopPolling() { if (poller) clearInterval(poller); poller = null; }
  async function start(demo) { const config = configFromForm(); if (!config.keywords.length && !demo) { toast('先填写至少一个岗位关键词'); $('#keywords').focus(); return; } try { const data = await api('/api/runs', { method: 'POST', body: JSON.stringify({ config, demo }) }); await refreshRuns(data.id); await openRun(data.id); toast(demo ? '已打开合成示例' : '已开始采集'); } catch (err) { toast(err.message); } }
  async function init() {
    try { boot = await api('/api/bootstrap'); $('#authStatus').textContent = boot.auth?.message || (boot.auth?.ready ? 'Chrome 插件已连接' : '首次使用请在终端运行 uv run python -m job_radar setup'); $('#authStatus').classList.toggle('ready', !!boot.auth?.ready); fillForm(boot.defaults); renderTemplates(boot.templates || []); renderRunHistory(boot.runs || []); if (boot.runs?.[0]) openRun(boot.runs[0].id); } catch (err) { $('#authStatus').textContent = '无法连接本地服务'; $('#runStatus').className = 'run-status error'; $('#runStatus').textContent = `无法读取初始配置：${err.message}`; }
  }
  $('#startRun').addEventListener('click', () => start(false)); $('#demoRun').addEventListener('click', () => start(true));
  $('#stopRun').addEventListener('click', async () => { if (!activeRun) return; try { await api(`/api/runs/${activeRun.id}/stop`, { method: 'POST', body: '{}' }); openRun(activeRun.id); } catch (err) { toast(err.message); } });
  $('#resumeRun').addEventListener('click', async () => { if (!activeRun) return; try { await api(`/api/runs/${activeRun.id}/resume`, { method: 'POST', body: '{}' }); openRun(activeRun.id); } catch (err) { toast(err.message); } });
  $('#exportRun').addEventListener('click', async () => { if (!activeRun) return; try { const data = await api(`/api/runs/${encodeURIComponent(activeRun.id)}/export`, { method: 'POST', body: JSON.stringify({ config: configFromForm() }) }); renderRun(data.run, data.jobs, data.exports); toast('已按当前字段和范围重新导出。'); } catch (err) { toast(err.message); } });
  $('#saveTemplate').addEventListener('click', async () => { const name = window.prompt('给这个模板起个名字'); if (!name?.trim()) return; try { const template = await api('/api/templates', { method: 'POST', body: JSON.stringify({ config: { ...configFromForm(), name: name.trim() } }) }); boot.templates = [...boot.templates, template]; renderTemplates(boot.templates); $('#templates').value = template.id; toast('模板已保存'); } catch (err) { toast(err.message); } });
  $('#templates').addEventListener('change', e => { const template = boot.templates.find(x => String(x.id) === e.target.value); if (template) { fillForm(template.config); toast(`已载入：${template.name}`); } });
  $('#runHistory').addEventListener('change', e => { if (e.target.value) openRun(e.target.value); });
  $('#resetConfig').addEventListener('click', () => fillForm(boot.defaults)); window.addEventListener('beforeunload', stopPolling); init();
})();
