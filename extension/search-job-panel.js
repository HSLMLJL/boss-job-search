// Fixed, same-page BOSS job-panel reader.
//
// `chrome.scripting.executeScript({func})` serializes only the function source,
// not this module's lexical scope.  readSearchJobPanelMainWorld therefore keeps
// every DOM helper and constant inside its body.

/**
 * Read, inspect, or select visible same-page job DOM only.
 * No fetch, Vue state, storage, navigation, scrolling, or hidden DOM is used.
 */
export function readSearchJobPanelMainWorld(options = {}) {
  const JOB_URL = /^https:\/\/(?:www\.)?zhipin\.com\/job_detail\/([A-Za-z0-9_-]+)\.html(?:[?#].*)?$/;
  const RISK_TEXT = /环境存在异常|访问过于频繁|稍后再试|安全验证|验证码|拖动滑块|账户存在异常行为|账号存在异常行为|暂时被限制访问|扫码登录|密码登录|短信登录/;
  const text = value => String(value || '').replace(/\r\n?/g, '\n').trim();

  const visible = node => {
    if (!node || typeof node.getClientRects !== 'function' || !node.getClientRects().length) return false;
    const style = typeof getComputedStyle === 'function' ? getComputedStyle(node) : null;
    return !style || (style.display !== 'none' && style.visibility !== 'hidden');
  };
  const label = node => ({
    tag: text(node?.tagName).toLowerCase(),
    classes: text(node?.className).split(/\s+/).filter(Boolean).slice(0, 16),
  });
  const safety = () => {
    const href = text(location.href);
    const pageText = text(document.body?.innerText || document.body?.textContent);
    if (options.expectedUrl && href !== options.expectedUrl) return {ok: false, href, reason: 'url-mismatch'};
    if (RISK_TEXT.test(pageText)) return {ok: false, href, reason: 'risk-or-login'};
    if (document.readyState && document.readyState !== 'complete') return {ok: false, href, reason: 'loading'};
    return {ok: true, href};
  };
  const absoluteHref = anchor => {
    // A job-detail link can carry a transient securityId query.  The stable
    // job ID is in the path; never persist its query string in evidence.
    try {
      const url = new URL(text(anchor?.getAttribute?.('href') || anchor?.href), location.href);
      return url.origin + url.pathname;
    }
    catch { return ''; }
  };
  const stableId = anchor => visible(anchor) ? (absoluteHref(anchor).match(JOB_URL)?.[1] || '') : '';
  const anchors = root => [...root?.querySelectorAll?.('a[href]') || []]
    .filter(visible)
    .map(anchor => ({anchor, id: stableId(anchor), href: absoluteHref(anchor), text: text(anchor.innerText || anchor.textContent)}))
    .filter(item => item.id);
  const ancestors = (node, maximum = 12) => {
    const result = [];
    for (let current = node, index = 0; current && index < maximum; current = current.parentElement, index += 1) result.push(current);
    return result;
  };
  const isCard = node => Boolean(node && /(^|\s)job-card-(?:wrap|wrapper)(\s|$)/.test(text(node.className)));

  // A card is valid only if all visible detail anchors inside it name exactly
  // the selected stable ID.  Title/company matches are intentionally ignored.
  const cardFor = wantedId => {
    const cards = [];
    for (const anchor of [...document.querySelectorAll?.('a[href]') || []]) {
      if (stableId(anchor) !== wantedId) continue;
      const card = ancestors(anchor).find(isCard);
      if (!card || !visible(card)) continue;
      const ids = [...new Set(anchors(card).map(item => item.id))];
      if (ids.length !== 1 || ids[0] !== wantedId) return {ambiguous: true};
      cards.push(card);
    }
    const unique = [...new Set(cards)];
    return unique.length === 1 ? {card: unique[0]} : (unique.length ? {ambiguous: true} : null);
  };
  const headings = () => [...document.querySelectorAll?.('h1,h2,h3,h4,h5,h6,[role="heading"],.job-sec-title,.detail-title') || []]
    .filter(node => visible(node) && /^职位描述$/.test(text(node.innerText || node.textContent)));

  // Start at the visible heading and climb only until a unique visible
  // “查看更多信息” job-detail anchor identifies the exact panel.
  const panelFor = heading => {
    for (const container of ancestors(heading)) {
      if (!visible(container)) continue;
      const found = anchors(container);
      const ids = [...new Set(found.map(item => item.id))];
      const more = found.filter(item => /查看更多信息/.test(item.text));
      if (more.length === 1 && ids.length === 1 && more[0].id === ids[0]) {
        return {container, jobId: ids[0], detailUrl: more[0].href};
      }
      if (ids.length > 1) return {ambiguous: true};
    }
    return null;
  };
  const descriptionFor = (heading, panel) => {
    // The observed legacy layout is h3.title + p.desc inside job-detail-body.
    // Prefer this exact direct sibling before any broader fallback; the parent
    // also contains recruiter/App and address footer content.
    const direct = heading.nextElementSibling;
    if (direct && visible(direct) && /(^|\s)desc(\s|$)/.test(text(direct.className))) {
      const body = text(direct.innerText || direct.textContent);
      if (body) return body;
    }
    // Some variants put text beside the heading.  Stop at another named
    // section so adjacent requirements/company text cannot become JD.
    const values = [];
    for (let node = heading.nextElementSibling; node && panel.container.contains?.(node); node = node.nextElementSibling) {
      if (!visible(node)) continue;
      const body = text(node.innerText || node.textContent);
      if (!body) continue;
      const nodeClasses = text(node.className);
      if (/(^|\s)(?:job-boss-info|job-address|job-detail-operate)(\s|$)/.test(nodeClasses)
          || /^(?:职位描述|任职要求|岗位要求|职位要求|工作地址|公司信息|福利待遇|工商信息|面试流程)$/.test(body.split('\n')[0])
          || /查看更多信息/.test(body)) break;
      values.push(body);
    }
    return text(values.join('\n'));
  };
  const uniqueField = (panel, selectors) => {
    const values = [];
    for (const selector of selectors) {
      for (const node of panel.container?.querySelectorAll?.(selector) || []) {
        const value = text(node.innerText || node.textContent);
        if (visible(node) && value) values.push(value);
      }
    }
    const unique = [...new Set(values)];
    return unique.length === 1 ? unique[0] : '';
  };
  const preferredClickTarget = card => {
    const targets = [];
    for (const node of card?.querySelectorAll?.('.job-card-box,.job-card-body') || []) {
      const tag = text(node.tagName).toLowerCase();
      if (visible(node) && tag !== 'a' && tag !== 'button' && card.contains?.(node)) targets.push(node);
    }
    const unique = [...new Set(targets)];
    return unique.length === 1 ? unique[0] : card;
  };

  const state = safety();
  const mode = options.mode || 'read';
  const base = {mode, href: state.href, status: 'unknown', reason: state.reason || ''};
  if (!state.ok) return base;

  if (mode === 'inspect') {
    const wanted = text(options.expectedJobId);
    const cards = [];
    const roots = [...document.querySelectorAll?.('.job-card-wrap,.job-card-wrapper') || []];
    const cardNodes = roots.length ? roots : [...document.querySelectorAll?.('.job-card-box') || []];
    for (const card of cardNodes.filter(visible)) {
      const ids = [...new Set(anchors(card).map(item => item.id))];
      if (ids.length !== 1 || (wanted && ids[0] !== wanted)) continue;
      cards.push({stableJobId: ids[0], root: label(card)});
      if (cards.length >= (wanted ? 1 : 30)) break;
    }
    if (wanted && cards.length !== 1) return {...base, reason: 'visible-card-not-found'};
    const diagnostic = headings().slice(0, 6).map(node => ({
      text: text(node.innerText || node.textContent),
      ancestors: ancestors(node.parentElement, 3).map(label),
      anchors: anchors(ancestors(node).find(parent => anchors(parent).length) || node)
        .slice(0, 4).map(item => ({stableJobId: item.id, text: item.text.slice(0, 80)})),
    }));
    return {...base, status: 'ready', reason: '', cards, headings: diagnostic};
  }

  if (mode === 'select') {
    const wanted = text(options.expectedJobId);
    if (!wanted) return {...base, reason: 'expected-job-id-required', clicked: false};
    const found = cardFor(wanted);
    if (found?.ambiguous) return {...base, reason: 'ambiguous-visible-card', clicked: false};
    if (!found?.card) return {...base, reason: 'visible-card-not-found', clicked: false};
    // Native event only: no anchor click, default prevention, hidden loader,
    // navigation, or retry.  An internal body is preferred for site bindings.
    const target = preferredClickTarget(found.card);
    target.click();
    const after = safety();
    if (!after.ok) return {...base, href: after.href, reason: after.reason || 'url-mismatch-after-click', clicked: true};
    return {mode, href: after.href, status: 'selected', reason: '', clicked: true, stableJobId: wanted,
      card: {stableJobId: wanted, root: label(found.card), target: label(target)}};
  }

  const matches = [];
  for (const heading of headings()) {
    const panel = panelFor(heading);
    if (panel?.ambiguous) return {...base, reason: 'ambiguous-panel-identity'};
    if (!panel) continue;
    const description = descriptionFor(heading, panel);
    if (!description) return {...base, reason: 'description-not-rendered'};
    matches.push({heading, panel, description});
  }
  if (matches.length !== 1) return {...base, reason: matches.length ? 'ambiguous-description-panels' : 'description-panel-unavailable'};
  const {heading, panel, description} = matches[0];
  if (options.expectedJobId && panel.jobId !== text(options.expectedJobId)) return {...base, reason: 'selected-panel-id-mismatch'};
  const found = cardFor(panel.jobId);
  if (found?.ambiguous) return {...base, reason: 'ambiguous-visible-card'};
  const card = found?.card ? {stableJobId: panel.jobId, root: label(found.card)} : null;
  return {
    mode, href: state.href, status: 'ready', reason: '', stableJobId: panel.jobId, description,
    detailUrl: panel.detailUrl,
    jobTitle: uniqueField(panel, ['h1', '.job-name', '.detail-title', '[data-role="job-title"]']),
    companyName: uniqueField(panel, ['.company-name', '.brand-name', '[data-role="company-name"]']),
    hrName: uniqueField(panel, ['.boss-name', '.recruiter-name', '[data-role="hr-name"]']),
    // Online/active state is deliberately separate from a recruiter name.
    hrActivity: uniqueField(panel, ['.active-time', '.boss-active', '[data-role="hr-activity"]']),
    address: uniqueField(panel, ['.job-address-desc', '[data-role="job-address"]']),
    card,
    clickTarget: card ? {...card, operation: 'native-wrapper-click', readOnly: true} : null,
    heading: label(heading),
  };
}

// Test-facing wrappers only. The handler injects the self-contained function.
export const inspectSearchJobPanelMainWorld = options => readSearchJobPanelMainWorld({...options, mode: 'inspect'});
export const selectSearchJobCardMainWorld = options => readSearchJobPanelMainWorld({...options, mode: 'select'});

function commandError(cmd) {
  const mode = cmd?.panelMode || cmd?.mode || 'read';
  if (!cmd || cmd.action !== 'read-search-job-panel') return 'read-search-job-panel action required';
  if (cmd.workspace !== 'boss-radar') return 'boss-radar workspace required';
  if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0) return 'explicit positive claimed tabId required';
  if (typeof cmd.url !== 'string' || !cmd.url.startsWith('https://')) return 'exact HTTPS search URL required';
  if (!['read', 'inspect', 'select'].includes(mode)) return 'unsupported panel mode';
  if (mode === 'select' && !String(cmd.expectedJobId || '').trim()) return 'select requires expectedJobId';
  return '';
}

function publicPayload(result) {
  const card = result?.card?.root ? {stable_job_id: result.card.stableJobId, clickable: true, root: result.card.root} : null;
  if (!result || result.status !== 'ready') {
    if (result?.status === 'selected') return {status: 'selected', href: result.href || '', selected_job_id: result.stableJobId || '', click_count: result.clicked ? 1 : 0, cards: card ? [card] : []};
    return {status: 'unknown', href: result?.href || '', reason: result?.reason || 'unknown', cards: []};
  }
  if (result.mode === 'inspect') return {status: 'ready', href: result.href || '', cards: (result.cards || []).map(item => ({stable_job_id: item.stableJobId, clickable: true, root: item.root})), headings: result.headings || []};
  return {
    status: 'ready', href: result.href || '', stable_job_id: result.stableJobId || '', jd_text: result.description || '',
    title: result.jobTitle || '', company: result.companyName || '', hr_name: result.hrName || '',
    hr_activity: result.hrActivity || '', job_work_address: result.address || '',
    identity_evidence: {source: 'visible-search-dom', panel_heading: '职位描述', stable_job_id: result.stableJobId || '', detail_url: result.detailUrl || '', exact_url: result.href || ''},
    cards: card ? [card] : [],
  };
}

/** Background adapter; context is injected by background.js and never claims tabs. */
export async function handleReadSearchJobPanel(cmd, context) {
  const error = commandError(cmd);
  if (error) return {ok: false, error};
  if (!context || typeof context.getClaimedRadarTab !== 'function' || typeof context.inspectRadarPage !== 'function') return {ok: false, error: 'panel reader context is incomplete'};
  if (typeof context.isCaptureInFlight === 'function' && context.isCaptureInFlight(cmd.tabId)) return {ok: false, error: 'visible capture is already in progress for this tab'};
  const claimed = await context.getClaimedRadarTab();
  if (!claimed || claimed.id !== cmd.tabId || claimed.url !== cmd.url) return {ok: false, error: 'explicit tab is not the exact currently claimed search tab'};
  const inspected = await context.inspectRadarPage(cmd.tabId, cmd.url);
  if (!inspected || inspected.status !== 'ready' || inspected.href !== cmd.url) return {ok: false, error: 'claimed search page is not safely settled'};
  const before = await chrome.tabs.get(cmd.tabId);
  if (before.url !== cmd.url) return {ok: false, error: 'claimed search URL changed before panel read'};
  const injected = await chrome.scripting.executeScript({
    target: {tabId: cmd.tabId, frameIds: [0]}, world: 'MAIN', func: readSearchJobPanelMainWorld,
    args: [{expectedUrl: cmd.url, expectedJobId: cmd.expectedJobId || '', mode: cmd.panelMode || cmd.mode || 'read'}],
  });
  const after = await chrome.tabs.get(cmd.tabId);
  const data = injected?.[0]?.result;
  if (after.url !== before.url || after.url !== cmd.url || !data || data.href !== cmd.url) return {ok: false, error: 'claimed search URL changed while reading panel'};
  return {id: cmd.id, ok: true, data: publicPayload(data)};
}
