// Read-only, owned-window adapter for individual job-link verification.
// This module deliberately does not inspect, navigate, or claim the user's anchor tab.

const ownedTabs = new Map();
const inflightWorkspaces = new Set();
const JOB_URL = /^https:\/\/www\.zhipin\.com\/job_detail\/([A-Za-z0-9_-]+)\.html$/;
const WORKSPACE = /^boss-verify-links-[A-Za-z0-9_-]+$/;

function captureJobDom() {
  const riskPhrases = ['访问受限', '您的账户存在异常行为', '环境异常',
    '请完成安全验证', '操作过于频繁', '检测到访问异常'];
  const header = document.querySelector('.info-primary');
  const heading = header?.querySelector('h1');
  const visible = element => !!element && element.getClientRects().length > 0
    && getComputedStyle(element).visibility !== 'hidden'
    && getComputedStyle(element).display !== 'none';
  const controls = header ? [...header.querySelectorAll('a,button')] : [];
  const communicate = controls.find(element => element.innerText?.trim() === '立即沟通');
  const interest = controls.find(element => element.innerText?.trim() === '感兴趣');
  const text = document.body?.innerText || '';
  return {
    actual_url: document.URL,
    job_title: heading?.innerText?.trim() || '',
    header_text: header?.innerText?.slice(0, 1200) || '',
    communicate_visible: visible(communicate),
    communicate_enabled: visible(communicate) && !communicate.hasAttribute('disabled')
      && communicate.getAttribute('aria-disabled') !== 'true'
      && !/(?:^|\s)(?:disabled|btn-disabled)(?:\s|$)/.test(communicate.className),
    interest_visible: visible(interest),
    risk_text: riskPhrases.find(value => text.includes(value)) || '',
    login_required: !header && /扫码登录|短信登录|账号登录/.test(text),
  };
}

const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const stateKey = workspace => `verifyJobLinkState:${encodeURIComponent(workspace)}`;

function validOwnedState(state) {
  return !!state && Number.isInteger(state.tabId) && state.tabId > 0
    && Number.isInteger(state.anchorId) && state.anchorId > 0
    && typeof state.expectedUrl === 'string' && JOB_URL.test(state.expectedUrl)
    && typeof state.sourceUrl === 'string' && JOB_URL.test(state.sourceUrl);
}

async function loadOwnedState(workspace) {
  const remembered = ownedTabs.get(workspace);
  if (remembered) return remembered;
  const stored = (await chrome.storage.local.get(stateKey(workspace)))[stateKey(workspace)];
  if (!validOwnedState(stored)) return null;
  ownedTabs.set(workspace, stored);
  return stored;
}

async function saveOwnedState(workspace, state) {
  ownedTabs.set(workspace, state);
  await chrome.storage.local.set({[stateKey(workspace)]: state});
}

async function waitForComplete(tabId, expectedUrl) {
  for (let attempt = 0; attempt < 10; attempt++) {
    const tab = await chrome.tabs.get(tabId);
    // URL agreement is required before DOM capture proves the new document.
    if (tab.status === 'complete' && tab.url === expectedUrl) return tab;
    if (attempt < 9) await pause(1000);
  }
  throw new Error('owned verification tab did not finish loading within 10 seconds');
}

async function readOwnedCapture(tabId, requestedUrl, expectedUrl) {
  const before = await chrome.tabs.get(tabId);
  const injected = await chrome.scripting.executeScript({
    target: {tabId}, world: 'ISOLATED', func: captureJobDom,
  });
  const capture = injected?.[0]?.result;
  if (!capture || typeof capture.actual_url !== 'string') throw new Error('owned DOM capture returned no result');
  const after = await chrome.tabs.get(tabId);
  if (before.url !== after.url || after.url !== expectedUrl || capture.actual_url !== expectedUrl) {
    throw new Error('owned verification tab URL changed during capture');
  }
  return {requested_url: requestedUrl, ...capture};
}

async function captureAfterPaint(tabId, requestedUrl, expectedUrl) {
  let capture = await readOwnedCapture(tabId, requestedUrl, expectedUrl);
  for (let attempt = 0; attempt < 4 && !capture.header_text && !capture.risk_text
    && !capture.login_required; attempt++) {
    await pause(1000);
    capture = await readOwnedCapture(tabId, requestedUrl, expectedUrl);
  }
  return capture;
}

function unsafe(capture) {
  if (capture.risk_text) return `risk gate: ${capture.risk_text}`;
  if (capture.login_required) return 'login gate';
  if (!JOB_URL.test(capture.actual_url)) return 'unsafe verification URL';
  return '';
}

function invalid(message) { return {ok: false, error: message}; }

function stoppedCapture(capture, message) {
  return {ok: false, error: message, data: {...capture, error: message}};
}

function validateCommand(cmd) {
  if (!cmd || cmd.action !== 'verify-job-link') return 'verify-job-link action required';
  if (!WORKSPACE.test(cmd.workspace || '') || cmd.workspace.length > 100) return 'verification workspace required';
  if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0) return 'positive explicit anchor tabId required';
  if (!JOB_URL.test(cmd.url || '')) return 'canonical Zhipin job URL required';
  let proof;
  try { proof = JSON.parse(cmd.code); } catch { return 'code must be JSON'; }
  if (!proof || typeof proof.sourceUrl !== 'string' || !JOB_URL.test(proof.sourceUrl)
    || Object.keys(proof).length !== 1 || Object.keys(proof)[0] !== 'sourceUrl') return 'strict sourceUrl proof required';
  return proof;
}

/** Handle one native bridge command using a dedicated verification window only. */
export async function handleVerifyJobLink(cmd) {
  const proof = validateCommand(cmd);
  if (typeof proof === 'string') return invalid(proof);
  if (inflightWorkspaces.has(cmd.workspace)) return invalid('verification workspace already in progress');
  inflightWorkspaces.add(cmd.workspace);

  try {
    let state = await loadOwnedState(cmd.workspace);
    let tabId = state?.tabId;
    let owned;
    if (tabId === undefined) {
      const anchor = await chrome.tabs.get(cmd.tabId);
      if (anchor.url !== proof.sourceUrl || !JOB_URL.test(anchor.url || '')) {
        return invalid('anchor URL does not match sourceUrl proof');
      }
      const created = await chrome.windows.create({url: cmd.url, type: 'normal', focused: false});
      tabId = created?.tabs?.[0]?.id;
      if (!Number.isInteger(tabId)) return invalid('verification window did not provide a tab');
      if (tabId === cmd.tabId) return invalid('verification window must not alias the anchor tab');
      state = {tabId, expectedUrl: cmd.url, anchorId: cmd.tabId, sourceUrl: proof.sourceUrl};
      await saveOwnedState(cmd.workspace, state);
      owned = await waitForComplete(tabId, cmd.url);
    } else {
      if (state.anchorId !== cmd.tabId || state.sourceUrl !== proof.sourceUrl) {
        return invalid('anchor proof does not match owned verification state');
      }
      try { owned = await chrome.tabs.get(tabId); }
      catch { return invalid('owned verification tab is closed'); }
      const preflight = await readOwnedCapture(tabId, state.expectedUrl, state.expectedUrl);
      const blocked = unsafe(preflight);
      if (blocked) return stoppedCapture(preflight, blocked);
      if (!preflight.header_text) return stoppedCapture(preflight, 'blank owned verification document');
      if (owned.url !== cmd.url) {
        await chrome.tabs.update(tabId, {url: cmd.url});
        owned = await waitForComplete(tabId, cmd.url);
      }
    }

    const capture = await captureAfterPaint(tabId, cmd.url, cmd.url);
    const blocked = unsafe(capture);
    if (blocked) return stoppedCapture(capture, blocked);
    if (state.expectedUrl !== cmd.url) {
      state = {...state, expectedUrl: cmd.url};
      await saveOwnedState(cmd.workspace, state);
    }
    return {ok: true, data: capture};
  } catch (error) {
    return invalid(String(error?.message || error));
  } finally {
    inflightWorkspaces.delete(cmd.workspace);
  }
}
