/**
 * BOSS Agent Bridge — Service Worker (background script).
 *
 * Connects to boss-agent-cli daemon via WebSocket, receives commands,
 * dispatches them to Chrome APIs (debugger/tabs), returns results.
 */

import { handleVerifyJobLink } from './job-link-verification.js';
import { handleReadSearchJobPanel } from './search-job-panel.js';

const DAEMON_PORT = 19826;
const DAEMON_WS_URL = `ws://127.0.0.1:${DAEMON_PORT}/ext`;
const DAEMON_PING_URL = `http://127.0.0.1:${DAEMON_PORT}/ping`;
const BOSS_HOME_URL = 'https://www.zhipin.com/';
const DIAGNOSTIC_URL = `http://127.0.0.1:${DAEMON_PORT}/ping`;
const JOBLIST_PATH = '/wapi/zpgeek/search/joblist.json';
// Yesterday's successful captures arrived just under 20s. Give a slow normal
// page load room to finish without issuing any additional platform request.
const JOBLIST_CAPTURE_TIMEOUT = 45000;
const VISIBLE_FIRST_PAGE_POLL_MS = 500;
const VISIBLE_FIRST_PAGE_TIMEOUT_MS = 45000;
const VISIBLE_CAPTURE_TIMEOUT_MS = 45000;
const FRESH_START_TOTAL_TIMEOUT_MS = 50000;
// Leave time for the result to reach the daemon before its 60-second deadline.
const VISIBLE_FRESH_TOTAL_TIMEOUT_MS = 55000;
const VISIBLE_CREATED_TAB_COMMIT_TIMEOUT_MS = 30000;
const CLAIMED_RADAR_TAB_KEY = 'claimedRadarTabId';
const RADAR_TAB_KIND_KEY = 'radarTabKind';
const MAX_CONSUMED_FRESH_COMMAND_IDS = 128;

let ws = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_EAGER_ATTEMPTS = 6;
// This starts with the service worker. Do not recompute it on reconnect:
// Chrome can update packaged files while an older worker is still alive, and
// that worker must attest to the bytes it started with.
const extensionBuildHashPromise = getExtensionBuildHash();
const attached = new Set();

// ── Automation window ────────────────────────────────────────────────

let automationWindowId = null;
const pageCaptureState = new Map();
const visibleCaptureState = new Map();
const currentCardsCaptureState = new Map();
const visibleCaptureInFlightTabs = new Set();
let claimedRadarTabId = null;
const consumedFreshCommandIds = new Set();
const INSPECT_DIAGNOSTIC_STAGES = new Set([
	'received', 'before-tab-get', 'execute-script-dispatched', 'execute-script-settled',
	'after-tab-get', 'result-ready',
]);

// A quiet window may be waiting for a person to scan or verify. Inactivity is
// not permission to close it. Only an explicit close-window command (or the
// user closing Chrome) may remove the owned window.

async function getAutomationWindow() {
	if (automationWindowId) {
		try {
			await chrome.windows.get(automationWindowId);
			return automationWindowId;
		} catch {
			automationWindowId = null;
		}
	}
	const win = await chrome.windows.create({
		url: BOSS_HOME_URL,
		focused: false,
		width: 1280,
		height: 900,
		type: 'normal',
	});
	automationWindowId = win.id;
	await new Promise(r => setTimeout(r, 200));
	return automationWindowId;
}

chrome.windows.onRemoved.addListener((windowId) => {
	if (windowId === automationWindowId) {
		automationWindowId = null;
		pageCaptureState.clear();
	}
});

// ── CDP via chrome.debugger ──────────────────────────────────────────

async function ensureAttached(tabId) {
	let tab = await chrome.tabs.get(tabId);
	for (let attempt = 0; attempt < 10 && !tab.url?.startsWith('http'); attempt += 1) {
		await new Promise(resolve => setTimeout(resolve, 200));
		tab = await chrome.tabs.get(tabId);
	}
	if (!tab.url?.startsWith('http')) {
		attached.delete(tabId);
		throw new Error(`Cannot debug tab ${tabId}: URL is ${tab.url}`);
	}

	if (attached.has(tabId)) {
		try {
			await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
				expression: '1', returnByValue: true,
			});
			return;
		} catch {
			attached.delete(tabId);
		}
	}

	try {
		await chrome.debugger.attach({ tabId }, '1.3');
	} catch (e) {
		// A failed attach never grants ownership. Do not detach an existing
		// debugger (DevTools or another extension), or compete by attaching again.
		throw new Error(`attach failed: ${e.message}`);
	}
	attached.add(tabId);

	try { await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable'); } catch {}
}

async function evaluate(tabId, code) {
	await ensureAttached(tabId);
	const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
		expression: code,
		returnByValue: true,
		awaitPromise: true,
	});
	if (result.exceptionDetails) {
		throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'Eval error');
	}
	return result.result?.value;
}

// ── Tab management ───────────────────────────────────────────────────

function isBossSiteUrl(rawUrl) {
	try {
		const url = new URL(rawUrl);
		const host = url.hostname.toLowerCase().replace(/\.$/, '');
		return url.protocol === 'https:' && (host === 'zhipin.com' || host.endsWith('.zhipin.com'));
	} catch {
		return false;
	}
}

function isBossLegacySearchPage(rawUrl) {
        try {
                const url = new URL(rawUrl);
                return isBossSiteUrl(rawUrl)
			&& url.pathname === '/web/geek/jobs'
			&& Boolean(url.searchParams.get('city'))
			&& Boolean(url.searchParams.get('query'));
	} catch {
		return false;
	}
}

function isBossVerificationUrl(rawUrl) {
	try {
		const url = new URL(rawUrl);
		if (!isBossSiteUrl(rawUrl)) return false;
		const code = url.searchParams.get('code');
		return url.pathname.includes('/verify') || code === '32' || code === '36' || code === '37';
	} catch {
		return false;
	}
}

function isBossAccountRiskUrl(rawUrl) {
	try {
		const url = new URL(rawUrl);
		return isBossSiteUrl(rawUrl) && url.searchParams.get('code') === '32';
	} catch {
		return false;
	}
}

function isBossAccountRiskDiagnostic(diagnostic) {
	return diagnostic?.accountRiskSignal === true;
}

function bossRiskError(url, diagnostic = null) {
	if (isBossAccountRiskUrl(url) || isBossAccountRiskDiagnostic(diagnostic)) {
		return `ACCOUNT_RISK: BOSS reports that this account is temporarily restricted (code=32); url=${url || ''}`;
	}
	return `BOSS security verification detected (code=36/37); url=${url || ''}`;
}

function freshStartBlockerError(url, diagnostic) {
	if (isBossAccountRiskDiagnostic(diagnostic)) return bossRiskError(url, diagnostic);
	return `BOSS page requires user action before a fresh scan; pageState=${JSON.stringify(diagnostic)}`;
}

function isFreshStartBlockedDiagnostic(diagnostic) {
	return Boolean(
		diagnostic?.accountRiskSignal
		|| diagnostic?.strongLoginRequiredSignal
		|| diagnostic?.captchaSignal
		|| diagnostic?.riskSignal,
	);
}

function isFreshStartUnreadableDiagnostic(diagnostic) {
	return !diagnostic
		|| typeof diagnostic !== 'object'
		|| Array.isArray(diagnostic)
		|| Boolean(diagnostic.diagnosticError);
}

async function consumeFreshCommandId(commandId) {
        if (typeof commandId !== 'string' || !commandId || commandId.length > 512) {
                throw new Error('startFresh requires a non-empty command id');
        }
        if (consumedFreshCommandIds.has(commandId)) return false;
        try {
                const key = 'consumedFreshCommandIds';
                const stored = (await chrome.storage.session.get(key))[key];
                if (stored !== undefined && !(stored?.schema === 1 && Array.isArray(stored.ids)
                        && stored.ids.length <= MAX_CONSUMED_FRESH_COMMAND_IDS
                        && stored.ids.every(id => typeof id === 'string' && id.length > 0 && id.length <= 512))) {
                        throw new Error('stored replay guard is invalid');
                }
                const ids = stored?.ids || [];
                if (ids.includes(commandId)) {
                        consumedFreshCommandIds.add(commandId);
                        return false;
                }
                const next = [...ids, commandId].slice(-MAX_CONSUMED_FRESH_COMMAND_IDS);
                await chrome.storage.session.set({ [key]: { schema: 1, ids: next } });
                consumedFreshCommandIds.add(commandId);
                return true;
        } catch {
                throw new Error('startFresh replay guard persistence is unavailable; refusing navigation');
        }
}

function isBossCurrentLayoutSearchPage(rawUrl) {
        try {
                const url = new URL(rawUrl);
                return isBossSiteUrl(rawUrl)
                        && /^\/c\d{9}\/?$/.test(url.pathname)
                        && Boolean(url.searchParams.get('query'));
        } catch {
                return false;
        }
}

function bossSearchIdentity(rawUrl) {
        try {
                const url = new URL(rawUrl);
                if (isBossLegacySearchPage(rawUrl)) {
                        return {
                                layout: 'legacy', city: url.searchParams.get('city') || '',
                                query: url.searchParams.get('query') || '',
                        };
                }
                if (isBossCurrentLayoutSearchPage(rawUrl)) {
                        const city = (url.pathname.match(/^\/c(\d{9})\/?$/) || [])[1] || '';
                        return {
                                layout: 'current', city,
                                query: url.searchParams.get('query') || '',
                        };
                }
        } catch {}
        return null;
}

function isBossSearchPage(rawUrl) {
        return bossSearchIdentity(rawUrl) !== null;
}

async function loadClaimedRadarTabId() {
	if (Number.isInteger(claimedRadarTabId)) return claimedRadarTabId;
	const stored = await chrome.storage.local.get(CLAIMED_RADAR_TAB_KEY);
	const candidate = stored?.[CLAIMED_RADAR_TAB_KEY];
	claimedRadarTabId = Number.isInteger(candidate) ? candidate : null;
	return claimedRadarTabId;
}

async function clearClaimedRadarTab() {
	claimedRadarTabId = null;
	await chrome.storage.local.remove([CLAIMED_RADAR_TAB_KEY, RADAR_TAB_KIND_KEY]);
}

async function getClaimedRadarTab() {
	const tabId = await loadClaimedRadarTabId();
	if (!Number.isInteger(tabId)) return null;
	try {
		const tab = await chrome.tabs.get(tabId);
		const stored = await chrome.storage.local.get(RADAR_TAB_KIND_KEY);
		const kind = stored?.[RADAR_TAB_KIND_KEY];
		// A closed tab id can eventually be reused. Never retain a claim if it now
		// points outside BOSS. BOSS pages remain claimed unless the migration rule
		// below identifies the known automatic verification tab from 1.2.4.
		if (!isBossSiteUrl(tab.url || '') && tab.url !== DIAGNOSTIC_URL) {
			await clearClaimedRadarTab();
			return null;
		}
		// Version 1.2.4 created an automatic tab without recording ownership. If
		// that one stopped on verification, do not reuse it after the safer normal-
		// navigation upgrade. Leave the visible tab open but forget its claim.
		if (!kind && isBossVerificationUrl(tab.url || '')) {
			await clearClaimedRadarTab();
			return null;
		}
		return tab;
	} catch {
		await clearClaimedRadarTab();
		return null;
	}
}

async function radarTabStatus() {
	const tab = await getClaimedRadarTab();
	if (!tab) {
		return {
			prepared: false,
			automaticPreparation: true,
			reason: '运行雷达时将自动创建一张专用搜索页；也可以手动绑定当前 BOSS 结果页。',
		};
	}
	const prepared = isBossSearchPage(tab.url || '');
	return {
		prepared,
		automaticPreparation: true,
		tabId: tab.id,
		title: tab.title || 'BOSS 搜索页',
		url: tab.url || '',
		reason: prepared ? '' : '已绑定页签当前不是包含城市和关键词的 BOSS 搜索结果页。',
	};
}

async function createDedicatedRadarTab(initialUrl = DIAGNOSTIC_URL) {
	const win = await chrome.windows.create({
		url: initialUrl,
		focused: true,
		width: 1280,
		height: 900,
		type: 'normal',
	});
	let tab = win.tabs?.[0];
	if (!tab?.id) {
		const tabs = await chrome.tabs.query({ windowId: win.id });
		tab = tabs[0];
	}
	if (!tab?.id) throw new Error('Cannot create the dedicated radar tab');
	claimedRadarTabId = tab.id;
	await chrome.storage.local.set({
		[CLAIMED_RADAR_TAB_KEY]: tab.id,
		[RADAR_TAB_KIND_KEY]: 'automatic',
	});
	pageCaptureState.clear();
	return tab;
}

async function claimActiveRadarTab() {
	const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
	const tab = tabs[0];
	if (!tab?.id || !isBossSearchPage(tab.url || '')) {
		throw new Error('当前页不是 BOSS 搜索结果页；请先手动选择城市并搜索关键词。');
	}
	const oldTabId = await loadClaimedRadarTabId();
	if (Number.isInteger(oldTabId) && oldTabId !== tab.id && attached.has(oldTabId)) {
		try { await chrome.debugger.detach({ tabId: oldTabId }); } catch {}
		attached.delete(oldTabId);
	}
	claimedRadarTabId = tab.id;
	await chrome.storage.local.set({
		[CLAIMED_RADAR_TAB_KEY]: tab.id,
		[RADAR_TAB_KIND_KEY]: 'manual',
	});
	pageCaptureState.clear();
	return await radarTabStatus();
}

async function resolveTabId(tabId, workspace, initialRadarUrl = DIAGNOSTIC_URL) {
	if (workspace === 'boss-radar') {
		const claimed = await getClaimedRadarTab() || await createDedicatedRadarTab(initialRadarUrl);
		if (!claimed?.id) {
			throw new Error('Radar tab is not prepared; bind a visible BOSS search result page in the extension popup');
		}
		if (tabId !== undefined && tabId !== claimed.id) {
			throw new Error('Explicit radar tab does not match the user-claimed BOSS search tab');
		}
		return claimed.id;
	}

	if (tabId !== undefined) {
		try {
			const tab = await chrome.tabs.get(tabId);
			if (automationWindowId && tab.windowId === automationWindowId && tab.url?.startsWith('http')) {
				return tabId;
			}
		} catch {}
		throw new Error('Explicit tab is not part of the Bridge automation window');
	}

	// 未显式指定 tab 时只使用扩展自己的自动化窗口。绝不能占用任意现有
	// BOSS 页签；该页签可能正等待扫码/验证码，导航会表现为反复刷新并让登录失败。
	const windowId = await getAutomationWindow();
	const tabs = await chrome.tabs.query({ windowId });
	const good = tabs.find(t => t.url?.startsWith('http') || t.url?.startsWith('data:'));
	if (good?.id) return good.id;
	const newTab = await chrome.tabs.create({ windowId, url: BOSS_HOME_URL, active: true });
	return newTab.id;
}

async function resolveVisibleCaptureTab(cmd, workspace) {
    // Visible capture must never create a radar page on loopback and then reuse
    // it. Some Chrome profiles block that local diagnostic navigation, leaving a
    // late error document which can race a later BOSS search. Keep the old tab
    // visible for diagnosis, but detach it from radar ownership before creating
    // the one permitted target search tab.
    if (workspace !== 'boss-radar') {
        return {
            tabId: await resolveTabId(cmd.tabId, workspace, cmd.page === 1 ? cmd.url : DIAGNOSTIC_URL),
            createdTarget: false,
        };
    }
    let claimed = await getClaimedRadarTab();
    // `chrome-error://chromewebdata/` is what Chrome exposes when the former
    // loopback diagnostic page was blocked. It is as unusable as the original
    // loopback URL, but must remain open for diagnosis rather than be updated
    // or closed by a later fresh capture.
    const claimedUrl = claimed?.url || '';
    const claimedIsDiagnosticOrError = claimedUrl === DIAGNOSTIC_URL
        || /^https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?(?:\/|$)/i.test(claimedUrl)
        || /^(?:chrome-error|chrome):\/\//i.test(claimedUrl);
    if (claimed && claimedIsDiagnosticOrError) {
        await clearClaimedRadarTab();
        claimed = null;
    }
    if (claimed) {
        if (cmd.tabId !== undefined && cmd.tabId !== claimed.id) {
            throw new Error('Explicit radar tab does not match the user-claimed BOSS search tab');
        }
        return { tabId: claimed.id, createdTarget: false };
    }
    if (cmd.tabId !== undefined) {
        throw new Error('Explicit radar tab does not match the user-claimed BOSS search tab');
    }
    if (cmd.page !== 1) {
        throw new Error('Visible page beyond page 1 requires a claimed BOSS search tab and durable predecessor checkpoint');
    }
    const created = await createDedicatedRadarTab(cmd.url);
    return { tabId: created.id, createdTarget: true };
}

async function waitForCreatedRadarTargetCommit(tabId, expectedUrl, timeoutMs) {
    const deadline = Date.now() + Math.max(0, timeoutMs);
    while (Date.now() < deadline) {
        const tab = await chrome.tabs.get(tabId);
        const url = tab.url || '';
        const pendingUrl = tab.pendingUrl || '';
        // A risk redirect is terminal even if the original target remains in
        // `url` while Chrome is committing the redirect.
        if (isBossVerificationUrl(url) || isBossVerificationUrl(pendingUrl)) {
            return { ok: false, error: bossRiskError(pendingUrl || url) };
        }
        if (pendingUrl && pendingUrl !== expectedUrl) {
            return { ok: false, error: 'New radar tab is leaving the requested BOSS search URL' };
        }
        if (url === expectedUrl) return { ok: true };
        // Chrome documents that a newly-created tab can have no committed URL
        // yet, with the requested URL only in pendingUrl. This is the sole
        // transient state allowed here. Do not navigate, reload, or reuse it.
        const uncommittedInitial = !url || url === 'about:blank';
        if (!uncommittedInitial || (pendingUrl && pendingUrl !== expectedUrl)) {
            return { ok: false, error: 'New radar tab did not commit the requested BOSS search URL' };
        }
        await new Promise(resolve => setTimeout(resolve, Math.min(VISIBLE_FIRST_PAGE_POLL_MS, deadline - Date.now())));
    }
    return { ok: false, error: 'New radar tab did not commit the requested BOSS search URL before timeout' };
}

function isAllowedNavigation(rawUrl) {
	try {
		const url = new URL(rawUrl);
		const host = url.hostname.toLowerCase().replace(/\.$/, '');
		const isBoss = url.protocol === 'https:' && (host === 'zhipin.com' || host.endsWith('.zhipin.com'));
		const isDiagnostic = url.href === DIAGNOSTIC_URL;
		return isBoss || isDiagnostic;
	} catch {
		return false;
	}
}

function isBossJobListUrl(rawUrl) {
	try {
		const url = new URL(rawUrl);
		const host = url.hostname.toLowerCase().replace(/\.$/, '');
		return url.protocol === 'https:'
			&& (host === 'zhipin.com' || host.endsWith('.zhipin.com'))
			&& url.pathname === JOBLIST_PATH;
	} catch {
		return false;
	}
}

function isExpectedBossJobListUrl(rawUrl, expectedSearchUrl, expectedPage) {
	if (!isBossJobListUrl(rawUrl)) return false;
	try {
		const responseUrl = new URL(rawUrl);
		const expectedUrl = new URL(expectedSearchUrl);
		for (const name of ['city', 'query']) {
			const expected = expectedUrl.searchParams.get(name) || '';
			if (expected && responseUrl.searchParams.get(name) !== expected) return false;
		}
		const rawPage = responseUrl.searchParams.get('page') || '1';
		return Number.parseInt(rawPage, 10) === expectedPage;
	} catch {
		return false;
	}
}

function decodeResponseBody(body, base64Encoded) {
	if (!base64Encoded) return body;
	const binary = atob(body);
	const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
	return new TextDecoder().decode(bytes);
}

async function captureTimeoutDiagnostic(tabId) {
	try {
		// Keep the page script literal raw. In particular, its RegExp literals
		// contain escaped `/` characters which a normal outer template would eat.
		return await evaluate(tabId, String.raw`(() => {
			const text = document.body?.innerText || '';
			return {
				href: location.href,
				title: document.title,
				readyState: document.readyState,
				bodyLength: text.length,
				loginSignal: /\u767b\u5f55|\u626b\u7801\u767b\u5f55/.test(text),
				strongLoginRequiredSignal: /\u626b\u7801\u767b\u5f55|\u8bf7(?:\u4f7f\u7528)?(?:\u624b\u673a)?\u626b\u7801|\u5bc6\u7801\u767b\u5f55|\u77ed\u4fe1\u767b\u5f55/.test(text)
					|| /\/(?:login|web\/user)(?:[/?]|$)/.test(location.pathname),
				captchaSignal: /\u9a8c\u8bc1\u7801|\u5b89\u5168\u9a8c\u8bc1|\u62d6\u52a8\u6ed1\u5757/.test(text),
				riskSignal: /\u73af\u5883\u5b58\u5728\u5f02\u5e38|\u8bbf\u95ee\u8fc7\u4e8e\u9891\u7e41|\u7a0d\u540e\u518d\u8bd5/.test(text),
				accountRiskSignal: /(?:\u8d26\u6237|\u8d26\u53f7)(?:\u5b58\u5728)?\u5f02\u5e38\u884c\u4e3a|\u6682\u65f6\u88ab\u9650\u5236\u8bbf\u95ee|\u6682\u65f6\u88ab\u7981\u6b62\u4f7f\u7528/.test(text),
				jobListSignal: Boolean(document.querySelector('.job-card-wrapper, .job-list-box')),
			};
		})()`);
	} catch (error) {
		return { diagnosticError: error.message || String(error) };
	}
}

async function waitForJobList(tabId, expectedSearchUrl, expectedPage, trigger) {
	await ensureAttached(tabId);
	await chrome.debugger.sendCommand({ tabId }, 'Network.enable');

	return await new Promise((resolve, reject) => {
		const matchingRequestIds = new Set();
		let settled = false;

		const cleanup = () => {
			chrome.debugger.onEvent.removeListener(listener);
			clearTimeout(timer);
		};
		const finish = (error, data) => {
			if (settled) return;
			settled = true;
			cleanup();
			if (error) reject(error);
			else resolve(data);
		};
		const listener = async (source, method, params) => {
			if (source.tabId !== tabId) return;
			if (
				method === 'Network.responseReceived'
				&& isExpectedBossJobListUrl(params.response?.url || '', expectedSearchUrl, expectedPage)
			) {
				matchingRequestIds.add(params.requestId);
				return;
			}
			if (method !== 'Network.loadingFinished' || !matchingRequestIds.has(params.requestId)) return;
			try {
				const response = await chrome.debugger.sendCommand(
					{ tabId },
					'Network.getResponseBody',
					{ requestId: params.requestId },
				);
				const text = decodeResponseBody(response.body || '', Boolean(response.base64Encoded));
				const payload = JSON.parse(text);
				if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
					throw new Error('joblist response is not a JSON object');
				}
				finish(null, payload);
			} catch (error) {
				finish(new Error(`Cannot read joblist response: ${error.message || String(error)}`));
			}
		};
		const timer = setTimeout(async () => {
			const diagnostic = await captureTimeoutDiagnostic(tabId);
			finish(new Error(
				`Timed out waiting for the BOSS job list response; pageState=${JSON.stringify(diagnostic)}`,
			));
		}, JOBLIST_CAPTURE_TIMEOUT);

		chrome.debugger.onEvent.addListener(listener);
		Promise.resolve().then(trigger).catch(error => finish(error));
	});
}

function isExpectedBossSearchPage(rawUrl, expectedSearchUrl) {
        // Keep this guard self-contained: it protects a visible-tab capture
        // before any page action, so it must not accept a lookalike host or a
        // mixed legacy/current layout merely because city and query match.
        const identity = value => {
                try {
                        const url = new URL(value);
                        const host = url.hostname.toLowerCase().replace(/\.$/, '');
                        if (url.protocol !== 'https:' || (host !== 'zhipin.com' && !host.endsWith('.zhipin.com'))) return null;
                        if (url.pathname === '/web/geek/jobs') {
                                const city = url.searchParams.get('city') || '';
                                const query = url.searchParams.get('query') || '';
                                return city && query ? { layout: 'legacy', city, query } : null;
                        }
                        const city = (url.pathname.match(/^\/c(\d{9})\/?$/) || [])[1] || '';
                        const query = url.searchParams.get('query') || '';
                        return city && query ? { layout: 'current', city, query } : null;
                } catch {
                        return null;
                }
        };
        const current = identity(rawUrl);
        const expected = identity(expectedSearchUrl);
        return Boolean(current && expected
                && current.layout === expected.layout
                && current.city === expected.city
                && current.query === expected.query);
}

// A BOSS SPA can replace the current city route with its legacy route while a
// page is settling.  That is not a licence to change searches: it only lets a
// previously recorded native next-link be attempted once for the same city and
// keyword.  The subsequent card read still requires the current route.
function isSameBossSearchIdentity(rawUrl, expectedSearchUrl) {
        const current = bossSearchIdentity(rawUrl);
        const expected = bossSearchIdentity(expectedSearchUrl);
        return Boolean(current && expected
                && current.city === expected.city
                && current.query === expected.query);
}

function currentCardsUrlInfo(rawUrl) {
        try {
                const url = new URL(rawUrl);
                const city = (url.pathname.match(/^\/c(\d{9})\/?$/) || [])[1] || '';
                const queries = url.searchParams.getAll('query');
                const pages = url.searchParams.getAll('page');
                if (url.protocol !== 'https:' || !city || url.username || url.password || url.port
                        || queries.length !== 1 || !queries[0] || pages.length > 1
                        || url.searchParams.getAll('city').length !== 0) return null;
                const page = pages.length === 0 ? 1 : Number(pages[0]);
                if (!Number.isInteger(page) || page < 1 || page > 100) return null;
                return { url, city, query: queries[0], page };
        } catch {
                return null;
        }
}

function isValidCurrentCardsNextHref(nextHref, expectedUrl, expectedPage) {
        const expected = currentCardsUrlInfo(expectedUrl);
        const candidate = currentCardsUrlInfo(nextHref);
        return Boolean(expected && candidate && candidate.url.origin === expected.url.origin
                && candidate.city === expected.city && candidate.query === expected.query
                && candidate.page === expectedPage);
}

function inspectRadarPageMainWorld() {
	// This function is deliberately self-contained: chrome.scripting serializes it
	// into the page's MAIN world. It only reads already-rendered page state.
	const limitText = (value, maximum) => typeof value === 'string' ? value.slice(0, maximum) : '';
	const text = limitText(document.body?.innerText || '', 200000);
	const path = location.pathname || '';
	const loginSignal = /登录|扫码登录/.test(text);
	const strongLoginRequiredSignal = /扫码登录|请(?:使用)?(?:手机)?扫码|密码登录|短信登录/.test(text)
		|| /\/(?:login|web\/user)(?:[/?]|$)/.test(path);
	const captchaSignal = /验证码|安全验证|拖动滑块/.test(text);
	const riskSignal = /环境存在异常|访问过于频繁|稍后再试/.test(text);
	const accountRiskSignal = /(?:账户|账号)(?:存在)?异常行为|暂时被限制访问|暂时被禁止使用/.test(text);
        const geometryFor = node => ({
                scrollTop: Math.ceil(Number(node?.scrollTop)),
                clientHeight: Math.ceil(Number(node?.clientHeight)),
                scrollHeight: Math.ceil(Number(node?.scrollHeight)),
        });
        const base = {
                href: limitText(location.href || '', 2048), readyState: limitText(document.readyState || '', 32), bodyLength: text.length,
                visibilityState: limitText(document.visibilityState || '', 32),
                hidden: typeof document.hidden === 'boolean' ? document.hidden : null,
                rootGeometry: geometryFor(document.documentElement), bodyGeometry: geometryFor(document.body),
                loginSignal, strongLoginRequiredSignal, captchaSignal, riskSignal, accountRiskSignal,
                componentPresent: false, city: '', query: '', page: null, loading: false, moreLoading: false,
                loadingValid: false, moreLoadingValid: false,
                hasMore: null, hasMoreValid: false, outerScrollTop: null, outerScrollTopValid: false,
                isScrollIng: null, isScrollIngValid: false, jobListCount: 0, preview: [],
	};
	if (accountRiskSignal || strongLoginRequiredSignal || captchaSignal || riskSignal) {
		return { ...base, status: 'risk', reason: 'visible page reports a login, verification, or risk signal' };
	}
	try {
		const usable = candidate => Boolean(candidate && candidate._isDestroyed !== true
			&& Array.isArray(candidate.jobList) && candidate.pageVo && typeof candidate.loadMoreHandle === 'function');
		let component = null;
		for (const selector of ['.loading-wait', '.job-list-box', '.job-card-wrapper']) {
			for (const node of document.querySelectorAll(selector)) {
				let candidate = node.__vue__;
				for (let depth = 0; candidate && depth < 12; depth += 1, candidate = candidate.$parent) {
					if (usable(candidate)) { component = candidate; break; }
				}
				if (component) break;
			}
			if (component) break;
		}
		if (!component) return { ...base, status: 'not-ready', reason: 'job list component is unavailable' };
		const form = component.formData && typeof component.formData === 'object' ? component.formData : {};
		const city = limitText(form.city, 64);
		const query = limitText(form.query, 160);
		const current = new URL(location.href);
		const matchesCurrentUrl = city === String(current.searchParams.get('city') || '')
			&& query === String(current.searchParams.get('query') || '');
                const rawPage = component.pageVo?.page;
                const page = Number.isInteger(rawPage) && rawPage >= 1 && rawPage <= 1000 ? rawPage : null;
                // BOSS has shipped both pagination shapes.  Prefer the original
                // boolean when present; newer builds expose the inverse as
                // isComplete.  Do not infer a value when neither is explicit.
                const resolvedHasMore = typeof component.hasMore === 'boolean'
                        ? component.hasMore
                        : (typeof component.isComplete === 'boolean' ? !component.isComplete : null);
                const hasMoreValid = typeof resolvedHasMore === 'boolean';
		const loadingValid = typeof component.loading === 'boolean';
                const moreLoadingValid = typeof component.moreLoading === 'boolean';
                const outerScrollTopValid = Number.isFinite(component.outerScrollTop)
                        && component.outerScrollTop >= 0 && component.outerScrollTop <= 10000000;
                const isScrollIngValid = typeof component.isScrollIng === 'boolean';
		const loading = loadingValid ? component.loading : false;
		const moreLoading = moreLoadingValid ? component.moreLoading : false;
		const preview = component.jobList.slice(0, 3).filter(item => item && typeof item === 'object').map(item => ({
			jobName: limitText(item.jobName, 160), encryptJobId: limitText(item.encryptJobId, 160), cityName: limitText(item.cityName, 80),
		}));
		const result = {
			...base, componentPresent: true, city, query, page, loading, moreLoading, loadingValid, moreLoadingValid,
                        hasMore: hasMoreValid ? resolvedHasMore : null, hasMoreValid,
                        outerScrollTop: outerScrollTopValid ? Math.ceil(component.outerScrollTop) : null,
                        outerScrollTopValid, isScrollIng: isScrollIngValid ? component.isScrollIng : null, isScrollIngValid,
			jobListCount: Math.min(component.jobList.length, 10000), preview,
		};
		if (!result.city || !result.query || !matchesCurrentUrl || page === null || !hasMoreValid
			|| !loadingValid || !moreLoadingValid || loading || moreLoading) {
			return { ...result, status: 'not-ready', reason: 'job list state is incomplete, mismatched, or still loading' };
		}
		return { ...result, status: 'ready', reason: '' };
	} catch {
		return { ...base, status: 'not-ready', reason: 'job list state could not be read' };
	}
}

function normaliseRadarPageProbe(value) {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
	const text = (field, maximum) => typeof value[field] === 'string' && value[field].length <= maximum ? value[field] : null;
	const boolean = field => typeof value[field] === 'boolean' ? value[field] : null;
        const finiteInteger = (field, minimum, maximum) => Number.isInteger(value[field]) && value[field] >= minimum && value[field] <= maximum ? value[field] : null;
        const geometry = field => {
                const candidate = value[field];
                if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
                const scrollTop = Number.isInteger(candidate.scrollTop) && candidate.scrollTop >= 0 && candidate.scrollTop <= 10000000
                        ? candidate.scrollTop : null;
                const clientHeight = Number.isInteger(candidate.clientHeight) && candidate.clientHeight >= 0 && candidate.clientHeight <= 10000000
                        ? candidate.clientHeight : null;
                const scrollHeight = Number.isInteger(candidate.scrollHeight) && candidate.scrollHeight >= 0 && candidate.scrollHeight <= 10000000
                        ? candidate.scrollHeight : null;
                return scrollTop === null || clientHeight === null || scrollHeight === null ? null : { scrollTop, clientHeight, scrollHeight };
        };
	const href = text('href', 2048);
	const readyState = text('readyState', 32);
	const status = text('status', 32);
	const reason = text('reason', 240);
	const preview = Array.isArray(value.preview) && value.preview.length <= 3 ? value.preview.map(item => {
		if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
		const jobName = typeof item.jobName === 'string' && item.jobName.length <= 160 ? item.jobName : '';
		const encryptJobId = typeof item.encryptJobId === 'string' && item.encryptJobId.length <= 160 ? item.encryptJobId : '';
		const cityName = typeof item.cityName === 'string' && item.cityName.length <= 80 ? item.cityName : '';
		return { jobName, encryptJobId, cityName };
	}) : null;
	if (!href || !readyState || !status || reason === null || preview === null || !['ready', 'not-ready', 'risk'].includes(status)) return null;
        const bodyLength = finiteInteger('bodyLength', 0, 200000);
        const jobListCount = finiteInteger('jobListCount', 0, 10000);
        const visibilityState = text('visibilityState', 32);
        const rootGeometry = geometry('rootGeometry');
        const bodyGeometry = geometry('bodyGeometry');
        const outerScrollTop = value.outerScrollTop === null ? null : finiteInteger('outerScrollTop', 0, 10000000);
	const page = value.page === null ? null : finiteInteger('page', 1, 1000);
        const requiredBooleans = ['hidden', 'loginSignal', 'strongLoginRequiredSignal', 'captchaSignal', 'riskSignal', 'accountRiskSignal', 'componentPresent', 'loading', 'moreLoading', 'loadingValid', 'moreLoadingValid', 'hasMoreValid', 'outerScrollTopValid', 'isScrollIngValid'];
        if (bodyLength === null || jobListCount === null || !['visible', 'hidden', 'prerender', 'unloaded'].includes(visibilityState)
                || !rootGeometry || !bodyGeometry || (value.page !== null && page === null)
                || requiredBooleans.some(field => boolean(field) === null)) return null;
        if (value.hasMore !== null && typeof value.hasMore !== 'boolean') return null;
        if (value.outerScrollTopValid === true && outerScrollTop === null) return null;
        if (value.outerScrollTopValid === false && value.outerScrollTop !== null) return null;
        if (value.isScrollIngValid === true && boolean('isScrollIng') === null) return null;
        if (value.isScrollIngValid === false && value.isScrollIng !== null) return null;
        return {
                href, readyState, bodyLength, visibilityState, hidden: value.hidden, rootGeometry, bodyGeometry,
                loginSignal: value.loginSignal, strongLoginRequiredSignal: value.strongLoginRequiredSignal,
		captchaSignal: value.captchaSignal, riskSignal: value.riskSignal, accountRiskSignal: value.accountRiskSignal,
		componentPresent: value.componentPresent, city: text('city', 64) || '', query: text('query', 160) || '', page,
		loading: value.loading, moreLoading: value.moreLoading, loadingValid: value.loadingValid,
                moreLoadingValid: value.moreLoadingValid, hasMore: value.hasMore, hasMoreValid: value.hasMoreValid,
                outerScrollTop, outerScrollTopValid: value.outerScrollTopValid,
                isScrollIng: value.isScrollIng, isScrollIngValid: value.isScrollIngValid,
		jobListCount, preview, status, reason,
	};
}

function readVisibleJobsMainWorld(includeJobs = true) {
	// Serialized into the page's MAIN world. Keep this self-contained and
	// read-only: no debugger, eval, fetch, DOM mutation, scrolling, or storage.
	// While a native load-more call is in flight, polling this function runs at
	// 500 ms.  Do not repeatedly serialize every previously-loaded job and its
	// description on those polls: the Vue list is cumulative and that work grows
	// with page depth.  The caller first asks for lightweight pagination state,
	// then asks once for the full validated list after the requested page settles.
	const wantJobs = includeJobs === true;
	const limitText = (value, maximum) => typeof value === 'string' ? value.slice(0, maximum) : '';
	const text = limitText(document.body?.innerText || '', 200000);
	const path = location.pathname || '';
	const loginSignal = /登录|扫码登录/.test(text);
	const strongLoginRequiredSignal = /扫码登录|请(?:使用)?(?:手机)?扫码|密码登录|短信登录/.test(text)
		|| /\/(?:login|web\/user)(?:[/?]|$)/.test(path);
	const captchaSignal = /验证码|安全验证|拖动滑块/.test(text);
	const riskSignal = /环境存在异常|访问过于频繁|稍后再试/.test(text);
	const accountRiskSignal = /(?:账户|账号)(?:存在)?异常行为|暂时被限制访问|暂时被禁止使用/.test(text);
        const base = {
                href: limitText(location.href || '', 2048), loginSignal, strongLoginRequiredSignal,
                captchaSignal, riskSignal, accountRiskSignal, city: '', query: '', page: null,
                visibilityState: limitText(document.visibilityState || '', 32),
                hidden: typeof document.hidden === 'boolean' ? document.hidden : null,
                pageSize: null, loading: false, moreLoading: false, hasMore: null,
		jobs: [], jobsIncluded: wantJobs, status: 'not-ready', reason: '',
	};
	if (accountRiskSignal || strongLoginRequiredSignal || captchaSignal || riskSignal) {
		return { ...base, status: 'risk', reason: 'visible page reports login, verification, or risk' };
	}
	try {
		const usable = candidate => Boolean(candidate && candidate._isDestroyed !== true
			&& Array.isArray(candidate.jobList) && candidate.pageVo && typeof candidate.loadMoreHandle === 'function');
		let component = null;
		for (const selector of ['.loading-wait', '.job-list-box', '.job-card-wrapper']) {
			for (const node of document.querySelectorAll(selector)) {
				let candidate = node.__vue__;
				for (let depth = 0; candidate && depth < 12; depth += 1, candidate = candidate.$parent) {
					if (usable(candidate)) { component = candidate; break; }
				}
				if (component) break;
			}
			if (component) break;
		}
		if (!component) return { ...base, reason: 'job list component is unavailable' };
		const form = component.formData && typeof component.formData === 'object' ? component.formData : {};
		const city = limitText(form.city, 64);
		const query = limitText(form.query, 160);
		const current = new URL(location.href);
		const page = component.pageVo?.page;
		// Only restart recovery needs a page size. Normal sequential collection
		// uses the observed ID prefix, so a missing pageSize must not reject an
		// otherwise settled list. Never invent the customary value of 15.
		const rawPageSize = component.pageVo?.pageSize;
		const parsedPageSize = typeof rawPageSize === 'string' && /^[1-9]\d{0,2}$/.test(rawPageSize)
			? Number(rawPageSize) : rawPageSize;
            const pageSize = Number.isInteger(parsedPageSize) && parsedPageSize >= 1 && parsedPageSize <= 100
                    ? parsedPageSize : null;
            // BOSS has shipped both `hasMore` and the inverse `isComplete`.
            // Only use the latter when it is an explicit boolean; absence of
            // both remains a hard stop rather than a guessed continuation.
            const resolvedHasMore = typeof component.hasMore === 'boolean'
                    ? component.hasMore
                    : (typeof component.isComplete === 'boolean' ? !component.isComplete : null);
            if (
			!city || !query || city !== String(current.searchParams.get('city') || '')
			|| query !== String(current.searchParams.get('query') || '')
			|| !Number.isInteger(page) || page < 1 || page > 1000
			|| typeof component.loading !== 'boolean' || typeof component.moreLoading !== 'boolean'
                    || typeof resolvedHasMore !== 'boolean'
		) {
			const invalid = [];
			if (!city || city !== String(current.searchParams.get('city') || '')) invalid.push('city');
			if (!query || query !== String(current.searchParams.get('query') || '')) invalid.push('query');
			if (!Number.isInteger(page) || page < 1 || page > 1000) invalid.push(`page(${typeof page})`);
                    for (const field of ['loading', 'moreLoading']) {
                            if (typeof component[field] !== 'boolean') invalid.push(`${field}(${typeof component[field]})`);
                    }
                    if (typeof resolvedHasMore !== 'boolean') {
                            invalid.push(`hasMore(${typeof component.hasMore}),isComplete(${typeof component.isComplete})`);
                    }
			return { ...base, city, query, reason: `schema-invalid: ${invalid.join(', ')}` };
		}
		const state = {
			...base, city, query, page, pageSize, loading: component.loading, moreLoading: component.moreLoading,
                    hasMore: resolvedHasMore,
		};
		if (component.loading || component.moreLoading) return { ...state, reason: 'job list is still loading' };
		// Vue may retain every previously loaded page. This is bounded by the
		// command contract (100 pages × maximum native page size 100), not by a
		// single page's usual 15 rows.
		if (component.jobList.length > 10000) return { ...state, reason: 'job list exceeds the bounded cumulative limit' };
		if (!wantJobs) return { ...state, jobsIncluded: false, status: 'ready', reason: '' };
		const jobs = [];
		for (const item of component.jobList) {
			if (!item || typeof item !== 'object') return { ...state, reason: 'job list contains an invalid item' };
			const encryptJobId = limitText(item.encryptJobId, 160);
			const jobName = limitText(item.jobName, 160);
			if (!encryptJobId || !jobName) return { ...state, reason: 'job list has a missing stable id or name' };
			const skills = Array.isArray(item.skills) ? item.skills : (Array.isArray(item.jobSkills) ? item.jobSkills : []);
			const labels = Array.isArray(item.jobLabels) ? item.jobLabels : [];
			const cleanSkills = skills.filter(value => typeof value === 'string').slice(0, 24).map(value => limitText(value, 80));
			const aiEvidence = [...cleanSkills, ...labels.filter(value => typeof value === 'string').slice(0, 24)]
				.map(value => limitText(value, 80)).filter(value => /(?:\bAI\b|人工智能|大模型|LLM|机器学习|AIGC)/i.test(value)).slice(0, 12);
			jobs.push({
				encryptJobId, jobName, brandName: limitText(item.brandName, 160), cityName: limitText(item.cityName, 80),
				areaDistrict: limitText(item.areaDistrict || item.businessDistrict, 80), salaryDesc: limitText(item.salaryDesc, 80),
				experienceName: limitText(item.jobExperience || item.experienceName, 80), degreeName: limitText(item.jobDegree || item.degreeName, 80),
				jobExperience: limitText(item.jobExperience, 80), jobDegree: limitText(item.jobDegree, 80),
				jobLabels: labels.filter(value => typeof value === 'string').slice(0, 24).map(value => limitText(value, 80)),
				postDescription: limitText(item.postDescription, 1000), jobDescription: limitText(item.jobDescription, 1000),
				skills: cleanSkills, aiEvidence,
			});
		}
		return { ...state, jobs, jobsIncluded: true, status: 'ready', reason: '' };
	} catch {
		return { ...base, reason: 'job list state could not be read' };
	}
}

function triggerVisibleLoadMoreMainWorld(expectedCity, expectedQuery, expectedPage, allowActiveClaimedBackground) {
 // Revalidate immediately before the one permitted normal page action.  The
 // PageJobs checks document geometry even when its loader is called directly.
 // Choose ONE action: scroll when far from the native threshold, or invoke the
 // native loader when already there. Never fall back from one action to another.
    try {
            const href = String(location.href || '');
            const visibilityState = String(document.visibilityState || '');
            const hidden = document.hidden;
            const text = String(document.body?.innerText || '').slice(0, 200000);
		const accountRiskSignal = /(?:账户|账号)(?:存在)?异常行为|暂时被限制访问|暂时被禁止使用/.test(text);
		const captchaSignal = /验证码|安全验证|拖动滑块/.test(text);
		const strongLoginRequiredSignal = /扫码登录|请(?:使用)?(?:手机)?扫码|密码登录|短信登录/.test(text)
			|| /\/(?:login|web\/user)(?:[/?]|$)/.test(location.pathname || '');
		const riskSignal = /环境存在异常|访问过于频繁|稍后再试/.test(text);
            if (accountRiskSignal || captchaSignal || strongLoginRequiredSignal || riskSignal) {
                    return { ok: false, risk: true, href, accountRiskSignal, captchaSignal, strongLoginRequiredSignal, riskSignal, reason: 'visible page reports login, verification, or risk' };
            }
            // A page can report hidden merely because the user switched to
            // Codex, even while its BOSS result tab remains the user's active
            // Chrome tab. The caller may relax that browser quirk only after a
            // fresh active-tab proof. It remains one native page action.
            if ((visibilityState !== 'visible' || hidden !== false)
                    && allowActiveClaimedBackground !== true) {
                return { ok: false, reason: 'visible page is not currently visible' };
            }
		const currentUrl = new URL(href);
		if (
			currentUrl.protocol !== 'https:' || !/(?:^|\.)zhipin\.com$/i.test(currentUrl.hostname)
			|| currentUrl.pathname !== '/web/geek/jobs'
			|| currentUrl.searchParams.get('city') !== expectedCity || currentUrl.searchParams.get('query') !== expectedQuery
		) return { ok: false, reason: 'current URL changed before load-more' };
		const usable = candidate => Boolean(candidate && candidate._isDestroyed !== true
			&& Array.isArray(candidate.jobList) && candidate.pageVo && typeof candidate.loadMoreHandle === 'function');
		let component = null;
		for (const selector of ['.loading-wait', '.job-list-box', '.job-card-wrapper']) {
			for (const node of document.querySelectorAll(selector)) {
				let candidate = node.__vue__;
				for (let depth = 0; candidate && depth < 12; depth += 1, candidate = candidate.$parent) {
					if (usable(candidate)) { component = candidate; break; }
				}
				if (component) break;
			}
			if (component) break;
		}
		if (!component) return { ok: false, reason: 'job list component is unavailable' };
		const form = component.formData || {};
		if (String(form.city || '') !== expectedCity || String(form.query || '') !== expectedQuery) {
			return { ok: false, reason: 'visible city or query changed' };
		}
            const resolvedHasMore = typeof component.hasMore === 'boolean'
                    ? component.hasMore
                    : (typeof component.isComplete === 'boolean' ? !component.isComplete : null);
            if (typeof component.loading !== 'boolean'
                    || typeof component.moreLoading !== 'boolean' || typeof resolvedHasMore !== 'boolean') {
			return { ok: false, reason: 'visible pagination state is invalid' };
		}
          if (component.pageVo?.page !== expectedPage) {
                  return { ok: false, reason: 'visible pagination state is invalid' };
          }
          if (component.loading || component.moreLoading || resolvedHasMore === false) {
                  return { ok: false, reason: 'visible pagination is unavailable' };
          }
          const root = document.documentElement;
          const body = document.body;
          const scrollTop = Math.ceil(Number(root?.scrollTop || body?.scrollTop));
          const clientHeight = Math.ceil(Number(root?.clientHeight || body?.clientHeight));
          const scrollHeight = Math.ceil(Number(root?.scrollHeight || body?.scrollHeight));
          if (![scrollTop, clientHeight, scrollHeight].every(Number.isFinite)
              || scrollTop < 0 || clientHeight <= 0 || scrollHeight <= 0
              || scrollTop + clientHeight > scrollHeight + 1) {
              return { ok: false, reason: 'visible scroll geometry is invalid' };
          }
          const geometry = { scrollTop, clientHeight, scrollHeight };
          if (scrollTop + clientHeight + 196 < scrollHeight) {
              // Reaching the native threshold is not itself a BOSS request.
              // Some page builds bind load-more to the scroll event, while
              // others expose the same site's handler without that binding.
              // Scroll once, then only invoke the native handler if the scroll
              // did not already advance (or begin loading) the exact list.
              // Thus each requested result page still has at most one site-side
              // load-more invocation.
              window.scrollTo({ left: 0, top: scrollHeight - clientHeight, behavior: 'instant' });
              if (component.pageVo?.page !== expectedPage || component.loading || component.moreLoading) {
                  return { ok: true, mode: 'scroll-native', geometry };
              }
              const afterScrollTop = Math.ceil(Number(root?.scrollTop || body?.scrollTop));
              const afterClientHeight = Math.ceil(Number(root?.clientHeight || body?.clientHeight));
              const afterScrollHeight = Math.ceil(Number(root?.scrollHeight || body?.scrollHeight));
              if (![
                  afterScrollTop, afterClientHeight, afterScrollHeight,
              ].every(Number.isFinite) || afterScrollTop < 0 || afterClientHeight <= 0
                      || afterScrollHeight <= 0 || afterScrollTop + afterClientHeight > afterScrollHeight + 1
                      || afterScrollTop + afterClientHeight + 196 < afterScrollHeight
                      || component.pageVo?.page !== expectedPage || resolvedHasMore !== true) {
                  return { ok: false, reason: 'visible scroll did not reach the native load-more threshold' };
              }
              component.loadMoreHandle();
              return { ok: true, mode: 'scroll-direct', geometry };
          }
          component.loadMoreHandle();
          return { ok: true, mode: 'direct', geometry };
	} catch {
		return { ok: false, reason: 'load-more state could not be read' };
	}
}

function normaliseVisibleJobsProbe(value) {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
	const text = (field, maximum) => typeof value[field] === 'string' && value[field].length <= maximum ? value[field] : null;
	const bool = field => typeof value[field] === 'boolean' ? value[field] : null;
	const page = Number.isInteger(value.page) && value.page >= 1 && value.page <= 1000 ? value.page : null;
	const pageSize = Number.isInteger(value.pageSize) && value.pageSize >= 1 && value.pageSize <= 100 ? value.pageSize : null;
	const jobsIncluded = value.jobsIncluded === true;
	const jobs = Array.isArray(value.jobs) && value.jobs.length <= 10000 ? value.jobs : null;
        const visibilityState = text('visibilityState', 32);
        if (!text('href', 2048) || !text('status', 32) || text('reason', 240) === null || jobs === null
                || !['ready', 'not-ready', 'risk'].includes(value.status)
                || !['visible', 'hidden', 'prerender', 'unloaded'].includes(visibilityState)
                || ['hidden', 'loginSignal', 'strongLoginRequiredSignal', 'captchaSignal', 'riskSignal', 'accountRiskSignal', 'loading', 'moreLoading'].some(field => bool(field) === null)) return null;
	if (value.status === 'ready' && (page === null || bool('hasMore') === null)) return null;
	const cleaned = [];
	if (!jobsIncluded && jobs.length !== 0) return null;
	for (const item of jobs) {
		if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
		const encryptJobId = textFrom(item.encryptJobId, 160);
		const jobName = textFrom(item.jobName, 160);
		if (!encryptJobId || !jobName) return null;
		const list = (field, maximum, count) => Array.isArray(item[field]) && item[field].length <= count
			&& item[field].every(value => typeof value === 'string' && value.length <= maximum) ? item[field] : null;
		const skills = list('skills', 80, 24);
		const aiEvidence = list('aiEvidence', 80, 12);
		if (!skills || !aiEvidence) return null;
		const jobLabels = list('jobLabels', 80, 24);
		if (!jobLabels) return null;
		cleaned.push({ encryptJobId, jobName, brandName: textFrom(item.brandName, 160), cityName: textFrom(item.cityName, 80),
			areaDistrict: textFrom(item.areaDistrict, 80), salaryDesc: textFrom(item.salaryDesc, 80),
			experienceName: textFrom(item.experienceName, 80), degreeName: textFrom(item.degreeName, 80), skills, aiEvidence });
		cleaned[cleaned.length - 1].jobExperience = textFrom(item.jobExperience, 80);
		cleaned[cleaned.length - 1].jobDegree = textFrom(item.jobDegree, 80);
		cleaned[cleaned.length - 1].jobLabels = jobLabels;
		cleaned[cleaned.length - 1].postDescription = textFrom(item.postDescription, 1000);
		cleaned[cleaned.length - 1].jobDescription = textFrom(item.jobDescription, 1000);
	}
	return {
                href: value.href, status: value.status, reason: value.reason, visibilityState, hidden: value.hidden,
                city: text('city', 64) || '', query: text('query', 160) || '', page, pageSize,
		loginSignal: value.loginSignal, strongLoginRequiredSignal: value.strongLoginRequiredSignal,
		captchaSignal: value.captchaSignal, riskSignal: value.riskSignal, accountRiskSignal: value.accountRiskSignal,
		loading: value.loading, moreLoading: value.moreLoading, hasMore: bool('hasMore'), jobsIncluded, jobs: cleaned,
	};
	function textFrom(value, maximum) { return typeof value === 'string' && value.length <= maximum ? value : ''; }
}

async function readVisibleJobs(tabId, expectedUrl, { allowDifferentSearch = false, includeJobs = true } = {}) {
	const before = await chrome.tabs.get(tabId);
	if (!isBossSearchPage(before.url || '') || (!allowDifferentSearch && !isExpectedBossSearchPage(before.url || '', expectedUrl))) {
		return { status: 'not-ready', reason: 'radar tab URL does not match city and query' };
	}
	let injected;
	try {
		injected = await chrome.scripting.executeScript({
			target: { tabId, frameIds: [0] }, world: 'MAIN', func: readVisibleJobsMainWorld, args: [includeJobs === true],
		});
	} catch (error) {
		return { status: 'not-ready', reason: `visible page injection failed: ${error.message || String(error)}` };
	}
	const after = await chrome.tabs.get(tabId);
	const probe = normaliseVisibleJobsProbe(injected?.[0]?.result);
	if (after.url !== before.url) return { status: 'not-ready', reason: 'visible tab URL changed while being read' };
	// A transient empty MAIN-world result is not evidence of navigation after the
	// tab URL has been confirmed stable. Keep it retryable within the existing
	// bounded, read-only poll; the URL observations remain terminal checks.
	if (!probe) return { status: 'not-ready', reason: 'visible page probe is missing or invalid' };
	if (probe.href !== before.url) return { status: 'not-ready', reason: 'visible page href did not match tab URL' };
	if (!allowDifferentSearch && !isExpectedBossSearchPage(probe.href, expectedUrl)) {
		return { status: 'not-ready', reason: 'visible page URL does not match city and query' };
	}
	return probe;
}

async function executeVisibleMainWorld(tabId, func, args = []) {
	return await chrome.scripting.executeScript({
		target: { tabId, frameIds: [0] }, world: 'MAIN', func, args,
	});
}

async function waitForVisibleJobs(tabId, expectedUrl, expectedPage, timeoutMs = VISIBLE_CAPTURE_TIMEOUT_MS) {
	const deadline = Date.now() + timeoutMs;
	let last = { status: 'not-ready', reason: 'visible page is not ready' };
	while (Date.now() < deadline) {
		last = await readVisibleJobs(tabId, expectedUrl, { includeJobs: false });
		if (last.status === 'risk') return last;
		if (last.status === 'ready' && last.page === expectedPage) {
			// Do the expensive cumulative-list extraction only once, after the
			// lightweight state proves that this exact page is settled.
			const captured = await readVisibleJobs(tabId, expectedUrl, { includeJobs: true });
			if (captured.status === 'risk' || (captured.status === 'ready' && captured.page === expectedPage)) return captured;
			last = captured;
		}
		// Re-reading an incompatible schema cannot turn it into a supported one.
		// Stop once with the actual invalid fields instead of a 45-second loop.
		if (last.reason.startsWith('schema-invalid:')) return last;
		if (last.reason === 'visible tab URL changed while being read'
			|| last.reason === 'visible page href did not match tab URL'
			|| last.reason === 'visible page URL does not match city and query'
			|| last.reason.includes('URL does not match')) return last;
		await new Promise(resolve => setTimeout(resolve, Math.min(VISIBLE_FIRST_PAGE_POLL_MS, deadline - Date.now())));
	}
	return { ...last, timedOut: true };
}

async function readVisibleFirstSearchPage(tabId, expectedSearchUrl) {
	const tab = await chrome.tabs.get(tabId);
	if (!isExpectedBossSearchPage(tab.url || '', expectedSearchUrl)) {
		return { ready: false, reason: 'visible tab does not match the requested city and query' };
	}
	return await evaluate(tabId, `(() => {
		const text = document.body?.innerText || '';
		if (/(?:\u8d26\u6237|\u8d26\u53f7)(?:\u5b58\u5728)?\u5f02\u5e38\u884c\u4e3a|\u6682\u65f6\u88ab\u9650\u5236\u8bbf\u95ee|\u6682\u65f6\u88ab\u7981\u6b62\u4f7f\u7528/.test(text)) {
			return {
				ready: false,
				risk: true,
				accountRiskSignal: true,
				reason: 'BOSS reports that this account is temporarily restricted (code=32)',
			};
		}
		const findJobListComponent = () => {
			const usable = candidate => Boolean(
				candidate
					&& candidate._isDestroyed !== true
					&& Array.isArray(candidate.jobList)
					&& candidate.pageVo
					&& typeof candidate.loadMoreHandle === 'function'
			);
			const cached = window.__bossAgentRadarJobListComponent;
			if (usable(cached)) return cached;
			for (const selector of ['.loading-wait', '.job-list-box', '.job-card-wrapper']) {
				for (const node of document.querySelectorAll(selector)) {
					let candidate = node.__vue__;
					for (let depth = 0; candidate && depth < 12; depth += 1, candidate = candidate.$parent) {
						if (!usable(candidate)) continue;
						window.__bossAgentRadarJobListComponent = candidate;
						return candidate;
					}
				}
			}
			return null;
		};
		const component = findJobListComponent();
		if (!component) return { ready: false, reason: 'job list component is unavailable' };
		const expected = new URL(${JSON.stringify(expectedSearchUrl)});
		const form = component.formData || {};
		const currentPage = Number(component.pageVo?.page || 0);
		const pageSize = Number(component.pageVo?.pageSize || 15);
		const matches = String(form.city || '') === String(expected.searchParams.get('city') || '')
			&& String(form.query || '') === String(expected.searchParams.get('query') || '');
		const loading = Boolean(component.loading || component.moreLoading);
		if (!matches || currentPage !== 1 || loading) {
			return { ready: false, reason: 'visible search state is not a settled first page' };
		}
		if (typeof component.hasMore !== 'boolean') {
			return {
				ready: false,
				stateError: 'job list has non-boolean hasMore',
				reason: 'job list has non-boolean hasMore',
			};
		}
		return {
			ready: true,
			items: component.jobList.slice(0, pageSize),
			hasMore: component.hasMore,
		};
	})()`);
}

async function waitForVisibleFirstSearchPage(
	tabId,
	expectedSearchUrl,
	timeoutMs = VISIBLE_FIRST_PAGE_TIMEOUT_MS,
) {
	let last = { ready: false, reason: 'page is not ready' };
	const deadline = Date.now() + Math.max(0, timeoutMs);
	while (Date.now() < deadline) {
		last = await readVisibleFirstSearchPage(tabId, expectedSearchUrl);
		if (last?.ready || last?.risk) return last;
		if (last?.reason === 'visible tab does not match the requested city and query') return last;
		const remainingMs = deadline - Date.now();
		if (remainingMs > 0) {
			await new Promise(resolve => setTimeout(resolve, Math.min(VISIBLE_FIRST_PAGE_POLL_MS, remainingMs)));
		}
	}
	return last;
}

async function navigateRadarTabNormally(tabId, url, {
        allowSameUrl = false, allowSameSearchIdentity = false, timeoutMs = 30000,
} = {}) {
	const current = await chrome.tabs.get(tabId);
	if (current.url === url && !allowSameUrl) {
		throw new Error('Refusing to reload an already-open BOSS search page');
	}
	// Navigation itself must be a normal Chrome tab update with no debugger
	// attached. Attach only after the page has settled, when reading Vue state.
	if (attached.has(tabId)) {
		try { await chrome.debugger.detach({ tabId }); } catch {}
		attached.delete(tabId);
	}
	const completion = new Promise((resolve, reject) => {
		let done = false;
		let sawLoading = false;
		const finish = (error = null) => {
			if (done) return;
			done = true;
			chrome.tabs.onUpdated.removeListener(listener);
			clearTimeout(timer);
			if (error) reject(error);
			else resolve();
		};
		const listener = (id, info) => {
			if (id !== tabId) return;
			if (info.status === 'loading') sawLoading = true;
			if (info.status === 'complete' && sawLoading) finish();
		};
		const timer = setTimeout(
			() => finish(new Error(`Timed out waiting for normal Chrome navigation to ${url}`)),
			Math.max(1, timeoutMs),
		);
		chrome.tabs.onUpdated.addListener(listener);
		Promise.resolve(chrome.tabs.update(tabId, { url })).catch(error => finish(error));
	});
	await completion;
	const settled = await chrome.tabs.get(tabId);
    if (settled.url !== url) {
        if (isBossVerificationUrl(settled.url || '')) {
            throw new Error(bossRiskError(settled.url || ''));
        }
        if (allowSameSearchIdentity && isSameBossSearchIdentity(settled.url || '', url)) return;
        throw new Error(`Normal Chrome navigation did not commit the expected URL: ${url}`);
	}
}

// ── WebSocket connection ─────────────────────────────────────────────

/**
 * Produce an integrity value for the exact service-worker source Chrome loaded.
 * This deliberately reads the extension package through runtime.getURL rather
 * than relying on a build-time constant, so a stale loaded directory cannot
 * claim to be the source tree the CLI is checking.
 */
async function getExtensionBuildHash() {
	try {
		const response = await fetch(chrome.runtime.getURL('background.js'), { cache: 'no-store' });
		if (!response.ok) return null;
		const digest = await crypto.subtle.digest('SHA-256', await response.arrayBuffer());
		return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
	} catch (err) {
		console.warn('[boss-bridge] Unable to hash loaded background.js:', err);
		return null;
	}
}

async function sendHello(socket = ws) {
	const extensionBuildHash = await extensionBuildHashPromise;
	// A hash failure must be observable by the daemon/CLI; never substitute a
	// local or remembered value here.
	// Do not let a previous socket's delayed hash send its hello on a newer one.
	if (ws !== socket) return;
	socket?.send(JSON.stringify({
		type: 'hello',
		version: chrome.runtime.getManifest().version,
		extensionId: chrome.runtime.id || null,
		extensionBuildHash,
	}));
}

async function connect() {
	if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;

	try {
		const res = await fetch(DAEMON_PING_URL, { signal: AbortSignal.timeout(1000) });
		if (!res.ok) return;
	} catch {
		return;
	}
	// Another wake-up (alarm, startup, or install) may have completed its ping
	// while this call awaited. Re-check before creating a competing socket.
	if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;

	let socket;
	try {
		socket = new WebSocket(DAEMON_WS_URL);
	} catch {
		scheduleReconnect();
		return;
	}
	ws = socket;

	socket.onopen = async () => {
		console.log('[boss-bridge] Connected to daemon');
		reconnectAttempts = 0;
		if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
		await sendHello(socket);
	};

	socket.onmessage = async (event) => {
		try {
			const cmd = JSON.parse(event.data);
			const reportStage = (stage) => sendInspectDiagnostic(socket, cmd?.id, stage);
			const result = await handleCommand(cmd, reportStage);
			// A result must go only to the socket that delivered its command. The
			// daemon binds pending replies to that socket, so never cross-send it.
			sendCommandResult(socket, result);
		} catch (err) {
			console.error('[boss-bridge] Command error:', err);
		}
	};

	socket.onclose = () => {
		if (ws !== socket) return;
		ws = null;
		scheduleReconnect();
	};

	socket.onerror = () => socket.close();
}

function sendInspectDiagnostic(socket, commandId, stage) {
	if (
		typeof commandId !== 'string' || !commandId || !INSPECT_DIAGNOSTIC_STAGES.has(stage)
		|| socket?.readyState !== WebSocket.OPEN
	) return;
	try {
		socket.send(JSON.stringify({ type: 'inspect-diagnostic', id: commandId, stage }));
	} catch {
		// Telemetry is best-effort only. Never hide a real probe result because a
		// local diagnostic message could not be delivered.
	}
}

function sendCommandResult(socket, result) {
	if (socket?.readyState !== WebSocket.OPEN) return false;
	try {
		socket.send(JSON.stringify(result));
		return true;
	} catch {
		return false;
	}
}

function scheduleReconnect() {
	if (reconnectTimer) return;
	reconnectAttempts++;
	if (reconnectAttempts > MAX_EAGER_ATTEMPTS) return;
	const delay = Math.min(2000 * Math.pow(2, reconnectAttempts - 1), 60000);
	reconnectTimer = setTimeout(() => {
		reconnectTimer = null;
		connect();
	}, delay);
}

// ── Command dispatcher ───────────────────────────────────────────────

async function handleCommand(cmd, reportStage = null) {
	try {
		switch (cmd.action) {
		case 'verify-job-link': return { ...(await handleVerifyJobLink(cmd)), id: cmd.id };
                case 'read-search-job-panel': return await handleSearchJobPanel(cmd, reportStage);
		case 'exec': return await handleExec(cmd);
		case 'navigate': return await handleNavigate(cmd);
                case 'capture-job-list': return await handleCaptureJobList(cmd);
                case 'capture-visible-jobs': return await handleCaptureVisibleJobs(cmd, reportStage);
                case 'capture-current-job-cards': return await handleCaptureCurrentJobCards(cmd, reportStage);
                case 'capture-visible-loaded-pages': return await handleCaptureVisibleLoadedPages(cmd, reportStage);
                case 'inspect-radar-page': return await handleInspectRadarPage(cmd, reportStage);
                case 'present-radar-tab': return await handlePresentRadarTab(cmd, reportStage);
                case 'radar-tab-status': return await handleRadarTabStatus(cmd);
		case 'close-window': return await handleCloseWindow(cmd);
			default: return { id: cmd.id, ok: false, error: `Unknown action: ${cmd.action}` };
		}
	} catch (err) {
		return { id: cmd.id, ok: false, error: err.message || String(err) };
	}
}

async function handleRadarTabStatus(cmd) {
	return { id: cmd.id, ok: true, data: await radarTabStatus() };
}

async function handleSearchJobPanel(cmd, reportStage = null) {
        // The same mutex as list loading: panel selection must never race a page
        // load or another panel operation. No debugger connection is used here.
        if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0 || visibleCaptureInFlightTabs.has(cmd.tabId)) {
                return { id: cmd.id, ok: false, error: 'Search panel tab is missing or busy' };
        }
        visibleCaptureInFlightTabs.add(cmd.tabId);
        try {
                return await handleReadSearchJobPanel(cmd, {
                        getClaimedRadarTab,
                        inspectRadarPage: async () => {
                                const result = await handleInspectRadarPage({ id: cmd.id, tabId: cmd.tabId }, reportStage);
                                return result.ok ? result.data : null;
                        },
                });
        } finally {
                visibleCaptureInFlightTabs.delete(cmd.tabId);
        }
}

async function handleInspectRadarPage(cmd, reportStage = null) {
	const stage = value => { if (typeof reportStage === 'function') reportStage(value); };
	stage('received');
	if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0) return { id: cmd.id, ok: false, error: 'inspect-radar-page requires an explicit positive tabId' };
	stage('before-tab-get');
	const before = await chrome.tabs.get(cmd.tabId);
	if (!isBossSearchPage(before.url || '')) return { id: cmd.id, ok: false, error: 'inspect-radar-page only reads an HTTPS BOSS search results tab' };
	let injected;
	try {
		stage('execute-script-dispatched');
		injected = await chrome.scripting.executeScript({
			target: { tabId: cmd.tabId, frameIds: [0] }, world: 'MAIN', func: inspectRadarPageMainWorld,
		});
		stage('execute-script-settled');
	} catch (error) {
		return { id: cmd.id, ok: false, error: `inspect-radar-page could not read the current page: ${error.message || String(error)}` };
	}
	stage('after-tab-get');
	const after = await chrome.tabs.get(cmd.tabId);
	const probe = normaliseRadarPageProbe(injected?.[0]?.result);
	if (!probe) return { id: cmd.id, ok: false, error: 'inspect-radar-page returned invalid page state' };
	if (after.url !== before.url || probe.href !== before.url || !isBossSearchPage(probe.href)) {
		return { id: cmd.id, ok: false, error: 'inspect-radar-page refused a page that changed while being read' };
	}
	stage('result-ready');
        return { id: cmd.id, ok: true, data: probe };
}

async function handlePresentRadarTab(cmd, reportStage = null) {
        const stage = value => { if (typeof reportStage === 'function') reportStage(value); };
        stage('received');
        if (cmd.workspace !== 'boss-radar') {
                return { id: cmd.id, ok: false, error: 'present-radar-tab only supports the boss-radar workspace' };
        }
        if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0) {
                return { id: cmd.id, ok: false, error: 'present-radar-tab requires an explicit positive tabId' };
        }
        const claimed = await getClaimedRadarTab();
        if (!claimed?.id || claimed.id !== cmd.tabId) {
                return { id: cmd.id, ok: false, error: 'present-radar-tab only presents the currently claimed radar tab' };
        }
        // Reuse the strictly read-only probe before changing window focus.  It
        // checks the URL before/after MAIN-world inspection and never attaches
        // a debugger, creates a tab, or navigates.
        const first = await handleInspectRadarPage({ id: cmd.id, tabId: cmd.tabId }, reportStage);
        if (!first.ok) return first;
        if (first.data.status === 'risk') {
                return { id: cmd.id, ok: false, error: visibleCaptureError(first.data) };
        }
        const initialTab = await chrome.tabs.get(cmd.tabId);
        if (!Number.isInteger(initialTab.windowId) || initialTab.windowId <= 0
                || initialTab.url !== first.data.href
                || (isBossSearchPage(claimed.url || '') && initialTab.url !== claimed.url)) {
                return { id: cmd.id, ok: false, error: 'present-radar-tab refused because the claimed tab changed after inspection' };
        }
        const initialWindow = await chrome.windows.get(initialTab.windowId);
        let restored = false;
        let focused = false;
        let activated = false;
        // At most one window update and one tab update.  Neither operation
        // reloads, navigates, attaches a debugger, nor invokes a BOSS loader.
        if (initialWindow.state === 'minimized') {
                await chrome.windows.update(initialWindow.id, { state: 'normal', focused: true });
                restored = true;
                focused = true;
        } else if (!initialWindow.focused) {
                await chrome.windows.update(initialWindow.id, { focused: true });
                focused = true;
        }
        const afterFocusTab = await chrome.tabs.get(cmd.tabId);
        if (afterFocusTab.url !== initialTab.url || afterFocusTab.windowId !== initialTab.windowId) {
                return { id: cmd.id, ok: false, error: 'present-radar-tab refused because the claimed tab changed while being presented' };
        }
        if (!afterFocusTab.active) {
                await chrome.tabs.update(cmd.tabId, { active: true });
                activated = true;
        }
        const second = await handleInspectRadarPage({ id: cmd.id, tabId: cmd.tabId }, reportStage);
        if (!second.ok) return second;
        if (second.data.status === 'risk') {
                return { id: cmd.id, ok: false, error: visibleCaptureError(second.data) };
        }
        const finalTab = await chrome.tabs.get(cmd.tabId);
        const finalWindow = await chrome.windows.get(finalTab.windowId);
        if (finalTab.url !== initialTab.url || finalTab.windowId !== initialTab.windowId) {
                return { id: cmd.id, ok: false, error: 'present-radar-tab refused because the claimed tab changed while being read' };
        }
        stage('result-ready');
        return {
                id: cmd.id,
                ok: true,
                data: {
                        before: first.data,
                        after: second.data,
                        window: { id: finalWindow.id, state: finalWindow.state, focused: finalWindow.focused },
                        tab: { id: finalTab.id, active: finalTab.active, windowId: finalTab.windowId },
                        restored, focused, activated,
                        note: 'No navigation or loader action was sent; a previously dispatched page scroll may settle naturally after presentation.',
                },
        };
}

function visibleCaptureError(probe) {
	if (probe?.status === 'risk') {
		if (probe.accountRiskSignal === true) return bossRiskError(probe.href || '', probe);
		if (probe.captchaSignal === true) return 'BOSS security verification is visible; user action is required before capture';
		if (probe.riskSignal === true) return 'BOSS_RISK: visible page reports an environment or rate-limit risk signal';
		if (probe.strongLoginRequiredSignal === true) {
			return 'AUTH_REQUIRED: BOSS login is required before visible capture can continue';
		}
		return 'BOSS_RISK: visible page reports an unclassified risk signal';
	}
        if (probe?.status === 'ready' && !probe.reason) {
                return `Visible BOSS page is ready at page=${probe.page}, hasMore=${probe.hasMore}, but has not reached the requested page`;
        }
        return `Visible BOSS page is not a settled result page: ${probe?.reason || 'unknown state'}`;
}

function visibleCaptureStorageKey(workspace) {
        return `visibleCaptureState:${encodeURIComponent(String(workspace))}`;
}

function validVisibleCaptureState(value) {
        return Boolean(value && typeof value === 'object' && !Array.isArray(value)
                && value.schema === 1
                && Number.isInteger(value.tabId) && value.tabId > 0
                && typeof value.url === 'string' && value.url.length <= 2048
                && isExpectedBossSearchPage(value.url, value.url)
                && Number.isInteger(value.page) && value.page >= 1 && value.page <= 100
                && Array.isArray(value.ids) && value.ids.length <= 10000
                && value.ids.every(id => typeof id === 'string' && id.length > 0 && id.length <= 160));
}

function visibleCaptureStateMatchesProbe(state, probe, tabId, url) {
        return validVisibleCaptureState(state)
                && state.tabId === tabId && state.url === url
                && probe?.status === 'ready' && probe.page === state.page
                && !probe.loading && !probe.moreLoading
                && Array.isArray(probe.jobs) && probe.jobs.length === state.ids.length
                && probe.jobs.every((item, index) => item.encryptJobId === state.ids[index]);
}

function visibleCaptureAdvancedStateMatchesCheckpoint(state, probe, tabId, url) {
        return validVisibleCaptureState(state)
                && state.tabId === tabId && state.url === url
                && probe?.status === 'ready' && !probe.loading && !probe.moreLoading
                && Array.isArray(probe.jobs) && probe.jobs.length >= state.ids.length
                && state.ids.every((id, index) => probe.jobs[index]?.encryptJobId === id);
}

async function saveVisibleCaptureState(workspace, state) {
        const checkpoint = { schema: 1, tabId: state.tabId, url: state.url, page: state.page, ids: state.ids.slice() };
        if (!validVisibleCaptureState(checkpoint)) throw new Error('Visible capture checkpoint is invalid');
        await chrome.storage.session.set({ [visibleCaptureStorageKey(workspace)]: checkpoint });
        visibleCaptureState.set(workspace, checkpoint);
        return checkpoint;
}

async function loadVisibleCaptureState(workspace) {
        const inMemory = visibleCaptureState.get(workspace);
        if (validVisibleCaptureState(inMemory)) return { state: inMemory, source: 'memory' };
        const key = visibleCaptureStorageKey(workspace);
        try {
                const stored = (await chrome.storage.session.get(key))[key];
                if (stored === undefined) return { state: null, source: 'missing' };
                if (!validVisibleCaptureState(stored)) return { state: null, source: 'mismatch' };
                return { state: stored, source: 'session' };
        } catch {
                // A storage read failure must not authorize a blind continuation.
                return { state: null, source: 'unavailable' };
        }
}

async function discardVisibleCaptureState(workspace) {
        visibleCaptureState.delete(workspace);
        try { await chrome.storage.session.remove(visibleCaptureStorageKey(workspace)); } catch {}
}

function visibleCaptureSequenceError(checkpointSource) {
        const source = ['memory', 'session', 'missing', 'unavailable', 'mismatch'].includes(checkpointSource)
                ? checkpointSource : 'missing';
        return `Visible capture must be sequential and start at page 1 (checkpoint=${source})`;
}

function visiblePageItems(probe, previousIds = null) {
	if (!probe || probe.status !== 'ready' || typeof probe.hasMore !== 'boolean') return null;
	const ids = probe.jobs.map(item => item.encryptJobId);
	if (!previousIds) return probe.jobs;
	const isPrefix = previousIds.length <= ids.length && previousIds.every((id, index) => ids[index] === id);
	if (isPrefix) return probe.jobs.slice(previousIds.length);
	// Some Vue builds replace the visible list rather than append. Keep that
	// position-defined page intact even if BOSS sorting repeats an ID across page
	// boundaries; the persistent data layer performs final identity de-duplication.
	return probe.jobs;
}

function visibleCaptureEnvelope(probe, items) {
	return {
		code: 0,
		zpData: {
			jobList: items,
			hasMore: probe.hasMore,
			pageState: {
				currentPage: probe.page,
				pageSize: probe.pageSize,
				hasMore: probe.hasMore,
				identityMatched: true,
				terminal: probe.hasMore === false,
			},
		},
	};
}

function readCurrentJobCardsMainWorld(expectedCity, expectedQuery, expectedPage) {
        // BOSS's current city route is a separate implementation from the
        // retired Vue page-list.  Keep this self-contained because Chrome
        // serializes it into the page's MAIN world.  It only reads rendered
        // cards: no fetch, storage, scrolling, clicking, or DOM mutation.
        const text = (value, maximum = 1000) => String(value || '').trim().slice(0, maximum);
        const pageText = text(document.body?.innerText, 200000);
        const href = text(location.href, 2048);
        const path = location.pathname || '';
        const loginSignal = /登录|扫码登录/.test(pageText);
        const strongLoginRequiredSignal = /扫码登录|请(?:使用)?(?:手机)?扫码|密码登录|短信登录/.test(pageText)
                || /\/(?:login|web\/user)(?:[/?]|$)/.test(path);
        const captchaSignal = /验证码|安全验证|拖动滑块/.test(pageText);
        const riskSignal = /环境存在异常|访问过于频繁|稍后再试/.test(pageText);
        const accountRiskSignal = /(?:账户|账号)(?:存在)?异常行为|暂时被限制访问|暂时被禁止使用/.test(pageText);
        const base = {
                href, status: 'not-ready', reason: '', jobs: [], page: null, pageSize: null,
                hasMore: null, terminal: false, nextHref: null, paginationProven: false,
                loginSignal, strongLoginRequiredSignal,
                captchaSignal, riskSignal, accountRiskSignal,
        };
        if (accountRiskSignal || strongLoginRequiredSignal || captchaSignal || riskSignal) {
                return { ...base, status: 'risk', reason: 'visible page reports login, verification, or risk' };
        }
        try {
                const url = new URL(href);
                const match = url.pathname.match(/^\/c(\d{9})\/?$/);
                if (!match || match[1] !== String(expectedCity) || url.searchParams.get('query') !== String(expectedQuery)) {
                        return { ...base, reason: 'current city route does not match the requested city and query' };
                }
                const root = document.querySelector('.job-list');
                if (!root) return { ...base, reason: 'current-layout job list is not rendered yet' };
                const jobs = [];
                for (const card of root.querySelectorAll('.job-primary')) {
                        const primary = card.querySelector('.primary-box[data-jid]') || card.querySelector('.primary-box');
                        const encryptJobId = text(primary?.dataset?.jid || primary?.getAttribute('data-jid'), 160);
                        const jobName = text(card.querySelector('.job-name a')?.getAttribute('title') || card.querySelector('.job-name a')?.textContent, 160);
                        if (!encryptJobId || !jobName) continue;
                        const area = text(card.querySelector('.job-area')?.textContent, 100);
                        const [cityName = '', ...district] = area.split('·').map(part => text(part, 40)).filter(Boolean);
                        const limit = text(card.querySelector('.job-limit p')?.textContent, 160);
                        const degree = (limit.match(/学历不限|不限|大专|本科|硕士|博士/) || [''])[0];
                        const experience = (limit.match(/经验不限|在校\/应届|应届生|\d+(?:-\d+)?年(?:以上)?/) || [''])[0];
                        const tags = [...card.querySelectorAll('.tags span, .tags a, .tags li')]
                                .map(node => text(node.textContent, 80)).filter(Boolean).slice(0, 24);
                        const detailText = text(card.innerText, 1000);
                        const aiEvidence = [jobName, ...tags, detailText]
                                .filter(value => /(?:\bAI\b|人工智能|大模型|LLM|机器学习|AIGC|智能体|Agent)/i.test(value))
                                .map(value => text(value, 160)).slice(0, 12);
                        jobs.push({
                                encryptJobId,
                                securityId: text(primary?.dataset?.securityid, 160),
                                jobName,
                                brandName: text(card.querySelector('.info-company .name a')?.getAttribute('title') || card.querySelector('.info-company .name a')?.textContent, 160),
                                cityName, areaDistrict: district.join('·'),
                                salaryDesc: text(card.querySelector('.job-limit .red')?.textContent, 80),
                                experienceName: experience, degreeName: degree,
                                jobExperience: experience, jobDegree: degree,
                                jobLabels: tags, skills: tags, aiEvidence,
                                welfareList: [text(card.querySelector('.info-desc')?.textContent, 160)].filter(Boolean),
                                postDescription: detailText, jobDescription: detailText,
                        });
                }
                // Read, but never click, BOSS's own pager.  An absent pager or
                // a pager without an explicit disabled-next control is not an
                // end-of-results signal.
                const pager = document.querySelector('.page');
                if (!pager) return { ...base, jobs, pageSize: jobs.length,
                        reason: 'current-layout pager is absent; continuation is unproven' };
                const currentNodes = pager.querySelectorAll('.cur');
                const currentText = currentNodes.length === 1 ? text(currentNodes[0].textContent, 32) : '';
                if (!/^[1-9]\d{0,2}$/.test(currentText)) {
                        return { ...base, jobs, pageSize: jobs.length,
                                reason: 'current-layout pager has no unambiguous current page' };
                }
                const page = Number(currentText);
                if (page !== expectedPage) return { ...base, jobs, pageSize: jobs.length,
                        page, reason: 'current-layout pager page does not match the requested page' };
                const urlPages = url.searchParams.getAll('page');
                const urlPage = urlPages.length === 0 ? 1 : Number(urlPages[0]);
                if (url.searchParams.getAll('query').length !== 1 || url.searchParams.getAll('city').length !== 0
                        || urlPages.length > 1 || !Number.isInteger(urlPage) || urlPage !== page) {
                        return { ...base, jobs, pageSize: jobs.length, page,
                                reason: 'current-layout URL page or query does not match the pager' };
                }
                // Prefer the native anchor explicitly. A non-anchor `.next`
                // is accepted only to recognize a disabled terminal control.
                const next = pager.querySelector('a.next') || pager.querySelector('.next');
                if (!next) return { ...base, jobs, pageSize: jobs.length, page,
                        reason: 'current-layout pager has no explicit next or terminal control' };
                const disabled = next.classList?.contains('disabled')
                        || next.getAttribute('aria-disabled') === 'true'
                        || next.hasAttribute('disabled');
                if (disabled) return { ...base, status: 'ready', reason: '', jobs, page,
                        pageSize: jobs.length, hasMore: false, terminal: true, paginationProven: true };
                if (next.tagName !== 'A' || !next.getAttribute('href')) {
                        return { ...base, jobs, pageSize: jobs.length, page,
                                reason: 'current-layout next control has no native href' };
                }
                const nextUrl = new URL(next.getAttribute('href'), href);
                const nextPageText = nextUrl.searchParams.get('page') || '';
                const nextCurrentRoute = /^\/c\d{9}\/?$/.test(nextUrl.pathname)
                        && (nextUrl.pathname.match(/^\/c(\d{9})\/?$/) || [])[1] === String(expectedCity);
                if (nextUrl.origin !== url.origin || nextUrl.searchParams.get('query') !== String(expectedQuery)
                        || !nextCurrentRoute || nextUrl.username || nextUrl.password || nextUrl.port
                        || nextUrl.searchParams.getAll('query').length !== 1
                        || nextUrl.searchParams.getAll('page').length !== 1
                        || nextUrl.searchParams.getAll('city').length !== 0
                        || !/^[1-9]\d{0,2}$/.test(nextPageText) || Number(nextPageText) !== page + 1) {
                        return { ...base, jobs, pageSize: jobs.length, page,
                                reason: 'current-layout next href does not prove the next same-search page' };
                }
                return { ...base, status: 'ready', reason: '', jobs, page, pageSize: jobs.length,
                        hasMore: true, terminal: false, nextHref: nextUrl.href, paginationProven: true };
        } catch {
                return { ...base, reason: 'current-layout cards could not be read' };
        }
}

function currentJobCardsEnvelope(probe) {
        return {
                code: 0,
                zpData: {
                        jobList: probe.jobs,
                        hasMore: probe.hasMore,
                        pageState: {
                                currentPage: probe.page,
                                pageSize: probe.pageSize,
                                hasMore: probe.hasMore,
                                identityMatched: true,
                                terminal: probe.terminal === true,
                                nextHref: probe.nextHref,
                        },
                },
        };
}

async function readCurrentJobCards(tabId, expectedUrl, expectedPage) {
        const before = await chrome.tabs.get(tabId);
        if (!isExpectedBossSearchPage(before.url || '', expectedUrl)
                || !isBossCurrentLayoutSearchPage(before.url || '')) {
                return { status: 'not-ready', reason: 'radar tab URL does not match current city route' };
        }
        const expected = bossSearchIdentity(expectedUrl);
        let injected;
        try {
                injected = await executeVisibleMainWorld(tabId, readCurrentJobCardsMainWorld,
                        [expected.city, expected.query, expectedPage]);
        } catch (error) {
                return { status: 'not-ready', reason: `current-layout page injection failed: ${error.message || String(error)}` };
        }
        const after = await chrome.tabs.get(tabId);
        const probe = injected?.[0]?.result;
        if (after.url !== before.url) return { status: 'not-ready', reason: 'visible tab URL changed while being read' };
        if (!probe || typeof probe !== 'object' || !Array.isArray(probe.jobs)) {
                return { status: 'not-ready', reason: 'current-layout page probe is missing or invalid' };
        }
        if (probe.href !== before.url) return { status: 'not-ready', reason: 'visible page href did not match tab URL' };
        return probe;
}

function currentCardsCaptureStorageKey(workspace) {
        return `currentCardsCaptureState:${encodeURIComponent(String(workspace))}`;
}

function validCurrentCardsCaptureState(value) {
        return Boolean(value && typeof value === 'object' && !Array.isArray(value)
                && value.schema === 1 && Number.isInteger(value.tabId) && value.tabId > 0
                && typeof value.url === 'string' && isBossCurrentLayoutSearchPage(value.url)
                && Number.isInteger(value.page) && value.page >= 1 && value.page <= 100
                && Array.isArray(value.pageIds) && value.pageIds.length <= 100
                && value.pageIds.every(id => typeof id === 'string' && id.length > 0 && id.length <= 160)
                && Array.isArray(value.seenIds) && value.seenIds.length <= 10000
                && value.seenIds.every(id => typeof id === 'string' && id.length > 0 && id.length <= 160)
                && (value.nextHref === null || (typeof value.nextHref === 'string' && value.nextHref.length <= 2048
                        && isValidCurrentCardsNextHref(value.nextHref, value.url, value.page + 1))));
}

async function saveCurrentCardsCaptureState(workspace, state) {
        const checkpoint = { schema: 1, tabId: state.tabId, url: state.url, page: state.page,
                pageIds: state.pageIds.slice(), seenIds: state.seenIds.slice(), nextHref: state.nextHref };
        if (!validCurrentCardsCaptureState(checkpoint)) throw new Error('Current-card capture checkpoint is invalid');
        await chrome.storage.session.set({ [currentCardsCaptureStorageKey(workspace)]: checkpoint });
        currentCardsCaptureState.set(workspace, checkpoint);
        return checkpoint;
}

async function loadCurrentCardsCaptureState(workspace) {
        const inMemory = currentCardsCaptureState.get(workspace);
        if (validCurrentCardsCaptureState(inMemory)) return { state: inMemory, source: 'memory' };
        try {
                const stored = (await chrome.storage.session.get(currentCardsCaptureStorageKey(workspace)))[currentCardsCaptureStorageKey(workspace)];
                return validCurrentCardsCaptureState(stored) ? { state: stored, source: 'session' }
                        : { state: null, source: stored === undefined ? 'missing' : 'mismatch' };
        } catch {
                return { state: null, source: 'unavailable' };
        }
}

async function handleCaptureCurrentJobCards(cmd, reportStage = null) {
        const stage = value => { if (typeof reportStage === 'function') reportStage(value); };
        stage('received');
        if (!cmd.url || !isBossCurrentLayoutSearchPage(cmd.url) || !isExpectedBossSearchPage(cmd.url, cmd.url)) {
                return { id: cmd.id, ok: false, error: 'capture-current-job-cards requires an exact BOSS current city route and query' };
        }
        if (cmd.workspace !== 'boss-radar' || !Number.isInteger(cmd.page) || cmd.page < 1 || cmd.page > 100
                || typeof cmd.startFresh !== 'boolean' || (cmd.page > 1 && cmd.startFresh)) {
                return { id: cmd.id, ok: false, error: 'capture-current-job-cards requires boss-radar sequential pages and explicit startFresh only on page 1' };
        }
        if (cmd.startFresh && !await consumeFreshCommandId(cmd.id)) {
                return { id: cmd.id, ok: false, error: 'Duplicate startFresh command refused before navigation' };
        }
        const resolvedTab = await resolveVisibleCaptureTab(cmd, cmd.workspace);
        const tabId = resolvedTab.tabId;
        if (visibleCaptureInFlightTabs.has(tabId)) {
                return { id: cmd.id, ok: false, error: 'Visible capture is already in progress for this tab; execution result unknown' };
        }
        visibleCaptureInFlightTabs.add(tabId);
        try {
                stage('before-tab-get');
                if (resolvedTab.createdTarget) {
                        const commit = await waitForCreatedRadarTargetCommit(tabId, cmd.url, VISIBLE_CREATED_TAB_COMMIT_TIMEOUT_MS);
                        if (!commit.ok) return { id: cmd.id, ok: false, error: commit.error };
                }
                let tab = await chrome.tabs.get(tabId);
                if (isBossVerificationUrl(tab.url || '')) return { id: cmd.id, ok: false, error: bossRiskError(tab.url || '') };
                if (cmd.page < 2) {
                        if (cmd.startFresh && !resolvedTab.createdTarget) {
                                await navigateRadarTabNormally(tabId, cmd.url, { allowSameUrl: true, timeoutMs: 15000 });
                        } else if (!isExpectedBossSearchPage(tab.url || '', cmd.url)) {
                                return { id: cmd.id, ok: false,
                                        error: 'Current-card page 1 is not the requested existing city route; startFresh is required before navigation' };
                        }
                }
                if (cmd.page > 1) {
                        const loaded = await loadCurrentCardsCaptureState(cmd.workspace);
                        const checkpoint = loaded.state;
                        if (!checkpoint || checkpoint.tabId !== tabId || !isSameBossSearchIdentity(checkpoint.url, cmd.url)
                                || checkpoint.page + 1 !== cmd.page || !checkpoint.nextHref
                                || !isValidCurrentCardsNextHref(checkpoint.nextHref, checkpoint.url, cmd.page)) {
                                return { id: cmd.id, ok: false, error: `Current-card capture must follow its durable native next-link checkpoint (checkpoint=${loaded.source})` };
                        }
                        if (!isSameBossSearchIdentity(tab.url || '', checkpoint.url)) {
                                return { id: cmd.id, ok: false, error: 'Current-card capture stopped because the claimed tab changed city or query' };
                        }
                        const diagnostic = await captureTimeoutDiagnostic(tabId);
                        if (isFreshStartBlockedDiagnostic(diagnostic)) {
                                return { id: cmd.id, ok: false, error: freshStartBlockerError(tab.url || '', diagnostic) };
                        }
                        if (tab.url !== checkpoint.nextHref) {
                                await navigateRadarTabNormally(tabId, checkpoint.nextHref,
                                        { allowSameSearchIdentity: true, timeoutMs: 15000 });
                        }
                }
                for (let attempt = 0; attempt < 5; attempt += 1) {
                        stage('execute-script-dispatched');
                        const probe = await readCurrentJobCards(tabId, cmd.url, cmd.page);
                        stage('execute-script-settled');
                        if (probe.status === 'risk') return { id: cmd.id, ok: false, error: visibleCaptureError(probe) };
                        if (probe.status === 'ready' && probe.paginationProven === true
                                && typeof probe.hasMore === 'boolean') {
                                const previous = cmd.page === 1 ? null : (await loadCurrentCardsCaptureState(cmd.workspace)).state;
                                const ids = probe.jobs.map(item => item.encryptJobId);
                                const seen = previous ? previous.seenIds : [];
                                const pageIds = [...new Set(ids)];
                                const newIds = pageIds.filter(id => !seen.includes(id));
                                const sameWindow = previous && pageIds.length === previous.pageIds.length
                                        && pageIds.every((id, index) => id === previous.pageIds[index]);
                                if (cmd.page > 1 && (!newIds.length || sameWindow)) {
                                        return { id: cmd.id, ok: false,
                                                error: 'Current-card capture stopped at a repeated native result window' };
                                }
                                try {
                                        await saveCurrentCardsCaptureState(cmd.workspace, {
                                                tabId, url: cmd.url, page: cmd.page,
                                                pageIds, seenIds: [...new Set([...seen, ...pageIds])], nextHref: probe.nextHref,
                                        });
                                } catch {
                                        return { id: cmd.id, ok: false, error: 'Current-card page settled but durable checkpoint persistence failed; capture stopped' };
                                }
                                stage('result-ready');
                                return { id: cmd.id, ok: true, data: currentJobCardsEnvelope(probe) };
                        }
                        if (probe.reason?.includes('URL changed') || probe.reason?.includes('URL does not match')) {
                                return { id: cmd.id, ok: false, error: `Current BOSS city route did not remain available: ${probe.reason}` };
                        }
                        if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 120));
                }
                return { id: cmd.id, ok: false, error: 'Current BOSS city route did not render readable job cards after its one normal navigation' };
        } finally {
                visibleCaptureInFlightTabs.delete(tabId);
        }
}

async function handleCaptureVisibleLoadedPages(cmd, reportStage = null) {
        const stage = value => { if (typeof reportStage === 'function') reportStage(value); };
        stage('received');
        // This is a deliberately narrow adoption of a page the site has already
        // loaded. It never resolves a tab (which could create one) and it never
        // invokes the normal paging action.
        if (cmd.workspace !== 'boss-radar') {
                return { id: cmd.id, ok: false, error: 'capture-visible-loaded-pages only supports the boss-radar workspace' };
        }
        if (!Number.isInteger(cmd.tabId) || cmd.tabId <= 0) {
                return { id: cmd.id, ok: false, error: 'capture-visible-loaded-pages requires an explicit positive tabId' };
        }
        if (!Number.isInteger(cmd.page) || cmd.page < 2 || cmd.page > 20) {
                return { id: cmd.id, ok: false, error: 'capture-visible-loaded-pages only permits a settled cumulative page 2 through 20 adoption' };
        }
        if (!cmd.url || !isExpectedBossSearchPage(cmd.url, cmd.url)) {
                return { id: cmd.id, ok: false, error: 'capture-visible-loaded-pages requires an exact HTTPS BOSS city and query URL' };
        }
        const claimed = await getClaimedRadarTab();
        if (!claimed?.id || claimed.id !== cmd.tabId || claimed.url !== cmd.url) {
                return { id: cmd.id, ok: false, error: 'capture-visible-loaded-pages only reads the exact currently claimed BOSS search tab' };
        }
        if (visibleCaptureInFlightTabs.has(cmd.tabId)) {
                return { id: cmd.id, ok: false, error: 'Visible capture is already in progress for this tab; adoption result unknown' };
        }
        visibleCaptureInFlightTabs.add(cmd.tabId);
        try {
                stage('execute-script-dispatched');
                const probe = await readVisibleJobs(cmd.tabId, cmd.url, { includeJobs: true });
                stage('execute-script-settled');
                if (probe.href !== cmd.url || probe.status !== 'ready' || probe.page !== cmd.page || probe.loading || probe.moreLoading
                        || !Number.isInteger(probe.pageSize) || probe.pageSize <= 0 || !Array.isArray(probe.jobs)) {
                        return { id: cmd.id, ok: false, error: `Visible loaded pages are not a settled page ${cmd.page}: ${visibleCaptureError(probe)}` };
                }
                const totalCount = probe.jobs.length;
                const fullCount = cmd.page * probe.pageSize;
                const priorCount = (cmd.page - 1) * probe.pageSize;
                const completeCumulative = probe.hasMore === true
                        ? totalCount === fullCount
                        : totalCount > priorCount && totalCount <= fullCount;
                if (!completeCumulative || probe.jobs.some(item => !item?.encryptJobId)) {
                        return { id: cmd.id, ok: false, error: 'Visible loaded pages do not prove a complete cumulative page-1-to-current list' };
                }
                const batches = [];
                for (let page = 1; page <= cmd.page; page += 1) {
                        const jobList = probe.jobs.slice((page - 1) * probe.pageSize, page * probe.pageSize);
                        const terminal = page === cmd.page && probe.hasMore === false;
                        if (!jobList.length || (page < cmd.page && jobList.length !== probe.pageSize)
                                || (page === cmd.page && !terminal && jobList.length !== probe.pageSize)) {
                                return { id: cmd.id, ok: false, error: 'Visible loaded pages cannot be split into complete ordered batches' };
                        }
                        batches.push({
                                page, jobList, hasMore: page < cmd.page ? true : probe.hasMore,
                                pageState: {
                                        currentPage: page, pageSize: probe.pageSize,
                                        hasMore: page < cmd.page ? true : probe.hasMore,
                                        identityMatched: true, terminal,
                                },
                        });
                }
                stage('after-tab-get');
                try {
                        await saveVisibleCaptureState(cmd.workspace, {
                                tabId: cmd.tabId, url: cmd.url, page: cmd.page,
                                ids: probe.jobs.map(item => item.encryptJobId),
                        });
                } catch {
                        return { id: cmd.id, ok: false, error: 'Visible loaded pages settled but checkpoint persistence failed; adoption stopped without a durable checkpoint' };
                }
                stage('result-ready');
                return {
                        id: cmd.id, ok: true,
                        data: {
                                code: 0,
                                zpData: { batches, currentPage: cmd.page, pageSize: probe.pageSize, totalCount },
                                adoption: {
                                        tabId: cmd.tabId, url: cmd.url, currentPage: cmd.page,
                                        jobListCount: totalCount, readOnly: true,
                                },
                        },
                };
        } finally {
                visibleCaptureInFlightTabs.delete(cmd.tabId);
        }
}

async function handleCaptureVisibleJobs(cmd, reportStage = null) {
	const stage = value => { if (typeof reportStage === 'function') reportStage(value); };
	stage('received');
	if (!cmd.url || !isExpectedBossSearchPage(cmd.url, cmd.url)) {
		return { id: cmd.id, ok: false, error: 'capture-visible-jobs requires an HTTPS BOSS city and query URL' };
	}
	if (!Number.isInteger(cmd.page) || cmd.page < 1 || cmd.page > 100) {
		return { id: cmd.id, ok: false, error: 'capture-visible-jobs requires a page between 1 and 100' };
	}
	if (cmd.startFresh !== undefined && typeof cmd.startFresh !== 'boolean') {
		return { id: cmd.id, ok: false, error: 'startFresh must be boolean when provided' };
	}
	if (cmd.startFresh === true && cmd.page !== 1) {
		return { id: cmd.id, ok: false, error: 'startFresh is only supported on page 1' };
	}
    const workspace = cmd.workspace || 'boss-radar';
    const freshDeadline = cmd.page === 1 ? Date.now() + VISIBLE_FRESH_TOTAL_TIMEOUT_MS : null;
    // Consume before resolving/creating a tab. A duplicate fresh command must
    // not create a replacement window or navigate a page.
    if (cmd.startFresh === true && !await consumeFreshCommandId(cmd.id)) {
        return { id: cmd.id, ok: false, error: 'Duplicate startFresh command refused before navigation' };
    }
    const resolvedTab = await resolveVisibleCaptureTab(cmd, workspace);
    const tabId = resolvedTab.tabId;
	if (visibleCaptureInFlightTabs.has(tabId)) {
		return { id: cmd.id, ok: false, error: 'Visible capture is already in progress for this tab; execution result unknown' };
	}
	visibleCaptureInFlightTabs.add(tabId);
    try {
           stage('before-tab-get');
           if (resolvedTab.createdTarget) {
                   const remaining = freshDeadline === null
                           ? VISIBLE_CREATED_TAB_COMMIT_TIMEOUT_MS
                           : Math.max(0, freshDeadline - Date.now());
                   const commit = await waitForCreatedRadarTargetCommit(
                           tabId,
                           cmd.url,
                           Math.min(VISIBLE_CREATED_TAB_COMMIT_TIMEOUT_MS, remaining),
                   );
                   if (!commit.ok) return { id: cmd.id, ok: false, error: commit.error };
           }
           let tab = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(tab.url || '')) return { id: cmd.id, ok: false, error: bossRiskError(tab.url || '') };

                if (cmd.page < 2) {
                        if (cmd.startFresh === true) {
                                const before = await readVisibleJobs(tabId, cmd.url, { allowDifferentSearch: true });
                                if (before.status === 'risk') return { id: cmd.id, ok: false, error: visibleCaptureError(before) };
                                // A newly-created dedicated tab already has the
                                // exact target URL as its one normal creation
                                // navigation. Never reload it. A pre-existing
                                // valid BOSS tab still gets exactly one fresh
                                // normal navigation.
                                if (!resolvedTab.createdTarget) {
                                        await navigateRadarTabNormally(tabId, cmd.url, { allowSameUrl: true });
                                }
			} else if (!isExpectedBossSearchPage(tab.url || '', cmd.url)) {
				// Before the one allowed city/query navigation, inspect an existing
				// BOSS results page for a visible risk/login signal. A mismatched city
				// alone is not risk, but risk always wins over a new navigation.
				if (isBossSearchPage(tab.url || '')) {
					const beforeNavigation = await readVisibleJobs(tabId, cmd.url, { allowDifferentSearch: true });
					if (beforeNavigation.status === 'risk') return { id: cmd.id, ok: false, error: visibleCaptureError(beforeNavigation) };
				}
				// City/query change gets exactly one ordinary Chrome navigation.
				await navigateRadarTabNormally(tabId, cmd.url);
                   }
                   stage('execute-script-dispatched');
                   const remaining = freshDeadline === null
                           ? VISIBLE_CAPTURE_TIMEOUT_MS
                           : Math.max(0, freshDeadline - Date.now());
                   const probe = await waitForVisibleJobs(tabId, cmd.url, 1, Math.min(VISIBLE_CAPTURE_TIMEOUT_MS, remaining));
			stage('execute-script-settled');
			if (probe.timedOut) {
				return { id: cmd.id, ok: false, error: `Visible first-page read did not settle; execution result unknown: ${visibleCaptureError(probe)}` };
			}
			if (probe.status !== 'ready' || probe.page !== 1) return { id: cmd.id, ok: false, error: visibleCaptureError(probe) };
			stage('after-tab-get');
                        try {
                                await saveVisibleCaptureState(workspace, { tabId, url: cmd.url, page: 1, ids: probe.jobs.map(item => item.encryptJobId) });
                        } catch {
                                return { id: cmd.id, ok: false, error: 'Visible first page settled but checkpoint persistence failed; capture stopped without a durable checkpoint' };
                        }
			stage('result-ready');
			return { id: cmd.id, ok: true, data: visibleCaptureEnvelope(probe, probe.jobs) };
		}

                const loadedCheckpoint = await loadVisibleCaptureState(workspace);
                let previous = loadedCheckpoint.state;
                let checkpointSource = loadedCheckpoint.source;
                // A service worker can restart while the visible Vue list remains alive.
		// Recover a requested earlier page only from a demonstrably cumulative list:
		// exact URL/form identity, native positive pageSize, and enough entries to
                // delimit the requested window. Never invent the common default of 15.
                if (!previous) {
                        if (checkpointSource !== 'missing') {
                                return { id: cmd.id, ok: false, error: visibleCaptureSequenceError(checkpointSource) };
                        }
                        stage('execute-script-dispatched');
                        const currentVisible = await readVisibleJobs(tabId, cmd.url);
                        stage('execute-script-settled');
                        if (currentVisible.status !== 'ready') {
                                return { id: cmd.id, ok: false, error: visibleCaptureError(currentVisible) };
                        }
                        const start = (cmd.page - 1) * (currentVisible?.pageSize || 0);
			const currentPageIsRequested = currentVisible?.status === 'ready'
				&& currentVisible.page === cmd.page
				&& Number.isInteger(currentVisible.pageSize)
				&& currentVisible.jobs.length >= start
				&& (currentVisible.hasMore === false || currentVisible.jobs.length >= start + currentVisible.pageSize);
			if (currentPageIsRequested) {
				const items = currentVisible.jobs.slice(start, start + currentVisible.pageSize);
				if (items.length || currentVisible.hasMore === false) {
					stage('after-tab-get');
                                        try {
                                                await saveVisibleCaptureState(workspace, {
                                                        tabId, url: cmd.url, page: cmd.page, ids: currentVisible.jobs.map(item => item.encryptJobId),
                                                });
                                        } catch {
                                                return { id: cmd.id, ok: false, error: 'Visible page settled but checkpoint persistence failed; capture stopped without a durable checkpoint' };
                                        }
					stage('result-ready');
					return { id: cmd.id, ok: true, data: visibleCaptureEnvelope(currentVisible, items) };
				}
			}
			// The visible page may instead be exactly the previous requested page.
			// Reconstruct that checkpoint only from a complete cumulative list, then
			// use the normal single-load path below.
			const currentPageIsPrevious = currentVisible?.status === 'ready'
				&& currentVisible.page === cmd.page - 1
				&& Number.isInteger(currentVisible.pageSize)
				&& currentVisible.hasMore === true
				&& currentVisible.jobs.length >= currentVisible.page * currentVisible.pageSize;
                        if (currentPageIsPrevious) {
                                previous = { tabId, url: cmd.url, page: currentVisible.page, ids: currentVisible.jobs.map(item => item.encryptJobId) };
                                checkpointSource = 'memory';
                                try {
                                        previous = await saveVisibleCaptureState(workspace, previous);
                                } catch {
                                        return { id: cmd.id, ok: false, error: 'Visible page checkpoint persistence failed; capture stopped without a durable checkpoint' };
                                }
                        }
                }
                if (!previous || previous.tabId !== tabId || previous.url !== cmd.url || previous.page + 1 !== cmd.page) {
                        return { id: cmd.id, ok: false, error: visibleCaptureSequenceError(checkpointSource) };
                }
                stage('execute-script-dispatched');
                const before = await readVisibleJobs(tabId, cmd.url);
                if (before.status !== 'ready' || before.loading || before.moreLoading) {
                        return { id: cmd.id, ok: false, error: visibleCaptureError(before) };
                }
                // The site's own normal loader can settle after a prior command
                // returns unknown.  When the live cumulative list proves the
                // exact old-ID prefix and this requested next page, capture that
                // suffix without issuing another site-side load.
                if (before.page === cmd.page) {
                        if (!visibleCaptureAdvancedStateMatchesCheckpoint(previous, before, tabId, cmd.url)) {
                                await discardVisibleCaptureState(workspace);
                                return { id: cmd.id, ok: false, error: visibleCaptureSequenceError('mismatch') };
                        }
                        const items = before.jobs.slice(previous.ids.length);
                        if (!items.length && before.hasMore !== false) {
                                return { id: cmd.id, ok: false, error: 'Visible page advanced without a new suffix or terminal evidence; refusing another load' };
                        }
                        stage('after-tab-get');
                        try {
                                await saveVisibleCaptureState(workspace, {
                                        tabId, url: cmd.url, page: cmd.page,
                                        ids: before.jobs.map(item => item.encryptJobId),
                                });
                        } catch {
                                return { id: cmd.id, ok: false, error: 'Visible already-loaded page settled but checkpoint persistence failed; capture stopped without a durable checkpoint' };
                        }
                        stage('result-ready');
                        return { id: cmd.id, ok: true, data: visibleCaptureEnvelope(before, items) };
                }
                if (before.page !== previous.page) {
                        return { id: cmd.id, ok: false, error: visibleCaptureError(before) };
                }
                if (!visibleCaptureStateMatchesProbe(previous, before, tabId, cmd.url)) {
                        await discardVisibleCaptureState(workspace);
                        return { id: cmd.id, ok: false, error: visibleCaptureSequenceError('mismatch') };
                }
                if (before.hasMore === false) {
                        return { id: cmd.id, ok: false, error: 'Visible page reports no more jobs; refusing a further load' };
                }
                let allowActiveClaimedBackground = false;
                if (before.visibilityState !== 'visible' || before.hidden !== false) {
                        // The old all-or-nothing visibility guard made the scan
                        // depend on Codex remaining in the background. Permit a
                        // narrow page-2+ continuation only after a fresh
                        // active-tab proof. Identity, risk,
                        // readiness, checkpoint, and one-action guards above
                        // remain mandatory.
                        let latestTab;
                        try {
                                latestTab = await chrome.tabs.get(tabId);
                        } catch {}
                        allowActiveClaimedBackground = latestTab?.active === true;
                        if (!allowActiveClaimedBackground) {
                                return { id: cmd.id, ok: false, error: 'Visible page is not currently visible and the claimed radar tab is not active; refusing any load-more action' };
                        }
                }
                const expected = new URL(cmd.url);
		let triggered;
		try {
			triggered = await executeVisibleMainWorld(tabId, triggerVisibleLoadMoreMainWorld, [
				String(expected.searchParams.get('city') || ''), String(expected.searchParams.get('query') || ''), previous.page,
                           allowActiveClaimedBackground,
			]);
		} catch (error) {
			return { id: cmd.id, ok: false, error: `Visible load-more dispatch failed; execution result unknown: ${error.message || String(error)}` };
		}
		const triggerState = triggered?.[0]?.result;
		stage('execute-script-settled');
		if (!triggerState?.ok) {
			if (triggerState?.risk) {
				return { id: cmd.id, ok: false, error: visibleCaptureError({ ...triggerState, status: 'risk' }) };
			}
			return { id: cmd.id, ok: false, error: `Visible load-more refused: ${triggerState?.reason || 'unknown state'}` };
		}
                // The one selected action can settle asynchronously. Observe it within the
                // existing bounded read-only wait; never add a scroll, repeat the loader,
                // or turn an unchanged page into a terminal result.
                const probe = await waitForVisibleJobs(tabId, cmd.url, cmd.page);
		if (probe.status !== 'ready' || probe.page !== cmd.page) {
                        const geometry = triggerState.geometry;
                        const actionDetail = geometry
                                ? `${triggerState.mode}; before scrollTop=${geometry.scrollTop}, clientHeight=${geometry.clientHeight}, scrollHeight=${geometry.scrollHeight}`
                                : String(triggerState.mode || 'unknown');
                        return { id: cmd.id, ok: false, error: `Visible load-more did not settle; execution result unknown (${actionDetail}): ${visibleCaptureError(probe)}` };
		}
		const items = visiblePageItems(probe, previous.ids);
		if (items === null) return { id: cmd.id, ok: false, error: 'Visible page sequence overlaps previous jobs and cannot prove new records' };
		stage('after-tab-get');
                try {
                        await saveVisibleCaptureState(workspace, { tabId, url: cmd.url, page: cmd.page, ids: probe.jobs.map(item => item.encryptJobId) });
                } catch {
                        return { id: cmd.id, ok: false, error: 'Visible page settled but checkpoint persistence failed; capture stopped without a durable checkpoint' };
                }
		stage('result-ready');
		return { id: cmd.id, ok: true, data: visibleCaptureEnvelope(probe, items) };
	} finally {
		visibleCaptureInFlightTabs.delete(tabId);
	}
}

async function handleExec(cmd) {
	if (!cmd.code) return { id: cmd.id, ok: false, error: 'Missing code' };
	const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
	const data = await evaluate(tabId, cmd.code);
	return { id: cmd.id, ok: true, data };
}

async function handleNavigate(cmd) {
	if (!cmd.url) return { id: cmd.id, ok: false, error: 'Missing url' };
	if (!isAllowedNavigation(cmd.url)) {
		return { id: cmd.id, ok: false, error: 'Only zhipin.com and the local diagnostic URL are allowed' };
	}

	const tabId = await resolveTabId(cmd.tabId, cmd.workspace);
	const current = await chrome.tabs.get(tabId);
	if (current.url === cmd.url) {
		return { id: cmd.id, ok: true, data: { title: current.title, url: current.url, tabId } };
	}
	await chrome.tabs.update(tabId, { url: cmd.url });

	// 等待导航完成
	await new Promise((resolve) => {
		let done = false;
		const finish = () => { if (!done) { done = true; chrome.tabs.onUpdated.removeListener(listener); resolve(); } };
		const listener = (id, info) => { if (id === tabId && info.status === 'complete') finish(); };
		chrome.tabs.onUpdated.addListener(listener);
		setTimeout(finish, 15000);
	});

	const tab = await chrome.tabs.get(tabId);
	return { id: cmd.id, ok: true, data: { title: tab.title, url: tab.url, tabId } };
}

async function handleCaptureJobList(cmd) {
	if (!cmd.url || !isAllowedNavigation(cmd.url)) {
		return { id: cmd.id, ok: false, error: 'Only a zhipin.com search page may be captured' };
	}
	if (!Number.isInteger(cmd.page) || cmd.page < 1 || cmd.page > 100) {
		return { id: cmd.id, ok: false, error: 'page must be an integer between 1 and 100' };
	}
	if (cmd.startFresh !== undefined && cmd.startFresh !== true && cmd.startFresh !== false) {
		return { id: cmd.id, ok: false, error: 'startFresh must be true or false when provided' };
	}
	if (cmd.startFresh === true && cmd.page !== 1) {
		return { id: cmd.id, ok: false, error: 'startFresh is only supported on page 1' };
	}

	const workspace = cmd.workspace || 'boss-radar';
	const tabId = await resolveTabId(
		cmd.tabId,
		workspace,
		cmd.startFresh === true ? DIAGNOSTIC_URL : (cmd.page === 1 ? cmd.url : DIAGNOSTIC_URL),
	);
	const previous = pageCaptureState.get(workspace);

	if (cmd.startFresh === true) {
		const freshDeadline = Date.now() + FRESH_START_TOTAL_TIMEOUT_MS;
		let current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '') };
		}
		// One read-only DOM status check happens before the single intentional
		// navigation. It never treats a blank body alone as a blocker.
		const beforeFreshNavigation = await captureTimeoutDiagnostic(tabId);
		if (isFreshStartUnreadableDiagnostic(beforeFreshNavigation)) {
			const diagnosticError = typeof beforeFreshNavigation?.diagnosticError === 'string'
				? beforeFreshNavigation.diagnosticError.slice(0, 240)
				: 'unknown diagnostic failure';
			return {
				id: cmd.id,
				ok: false,
				error: `BOSS page cannot be read before a fresh scan; refusing navigation; diagnosticError=${diagnosticError}`,
			};
		}
		if (isFreshStartBlockedDiagnostic(beforeFreshNavigation)) {
			return {
				id: cmd.id,
				ok: false,
				error: freshStartBlockerError(current.url || '', beforeFreshNavigation),
			};
		}
           if (!await consumeFreshCommandId(cmd.id)) {
			return { id: cmd.id, ok: false, error: 'Duplicate startFresh command refused before navigation' };
		}
		// Explicit startFresh is the only path allowed to reload the same URL.
		// Marking the command consumed above prevents a bridge replay from issuing
		// a second refresh while this normal Chrome navigation is in flight.
		const navigationBudgetMs = Math.min(30000, freshDeadline - Date.now());
		if (navigationBudgetMs <= 0) {
			return { id: cmd.id, ok: false, error: 'Fresh scan timed out before navigation' };
		}
		await navigateRadarTabNormally(tabId, cmd.url, { allowSameUrl: true, timeoutMs: navigationBudgetMs });
		current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '') };
		}
		const visibleBudgetMs = freshDeadline - Date.now();
		if (visibleBudgetMs <= 0) {
			return { id: cmd.id, ok: false, error: 'Fresh scan timed out after navigation' };
		}
		const visible = await waitForVisibleFirstSearchPage(tabId, cmd.url, visibleBudgetMs);
		if (visible?.risk) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', visible) };
		}
		if (visible?.ready && Array.isArray(visible.items)) {
			const data = {
				code: 0,
				zpData: { jobList: visible.items, hasMore: Boolean(visible.hasMore) },
			};
			pageCaptureState.set(workspace, { tabId, url: cmd.url, page: cmd.page });
			return { id: cmd.id, ok: true, data };
		}
		current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '') };
		}
		const diagnostic = await captureTimeoutDiagnostic(tabId);
		if (isBossAccountRiskDiagnostic(diagnostic)) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', diagnostic) };
		}
		return {
			id: cmd.id,
			ok: false,
			error: `Normal Chrome navigation did not yield a settled BOSS first page; pageState=${JSON.stringify(diagnostic)}`,
		};
	}

	if (cmd.page === 1) {
		let current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return {
				id: cmd.id,
				ok: false,
				error: bossRiskError(current.url || ''),
			};
		}

		let visible = await waitForVisibleFirstSearchPage(tabId, cmd.url);
		if (visible?.risk) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', visible) };
		}
		if (visible?.ready && Array.isArray(visible.items)) {
			const data = {
				code: 0,
				zpData: { jobList: visible.items, hasMore: Boolean(visible.hasMore) },
			};
			pageCaptureState.set(workspace, { tabId, url: cmd.url, page: cmd.page });
			return { id: cmd.id, ok: true, data };
		}

		current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return {
				id: cmd.id,
				ok: false,
				error: bossRiskError(current.url || ''),
			};
		}
		// This is one read-only DOM inspection before any normal navigation. A
		// non-empty explicit account restriction is terminal; a blank body alone
		// is not a risk signal and does not change the existing normal path.
		const beforeNavigation = await captureTimeoutDiagnostic(tabId);
		if (isBossAccountRiskDiagnostic(beforeNavigation)) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', beforeNavigation) };
		}
		if (isExpectedBossSearchPage(current.url || '', cmd.url)) {
			return {
				id: cmd.id,
				ok: false,
				error: `Bound radar tab is not a settled first page for this city and query: ${visible?.reason || 'unknown state'}`,
			};
		}

		await navigateRadarTabNormally(tabId, cmd.url);
		current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return {
				id: cmd.id,
				ok: false,
				error: bossRiskError(current.url || ''),
			};
		}
		visible = await waitForVisibleFirstSearchPage(tabId, cmd.url);
		if (visible?.risk) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', visible) };
		}
		if (visible?.ready && Array.isArray(visible.items)) {
			const data = {
				code: 0,
				zpData: { jobList: visible.items, hasMore: Boolean(visible.hasMore) },
			};
			pageCaptureState.set(workspace, { tabId, url: cmd.url, page: cmd.page });
			return { id: cmd.id, ok: true, data };
		}

		current = await chrome.tabs.get(tabId);
		if (isBossVerificationUrl(current.url || '')) {
			return {
				id: cmd.id,
				ok: false,
				error: bossRiskError(current.url || ''),
			};
		}
		const diagnostic = await captureTimeoutDiagnostic(tabId);
		if (isBossAccountRiskDiagnostic(diagnostic)) {
			return { id: cmd.id, ok: false, error: bossRiskError(current.url || '', diagnostic) };
		}
		return {
			id: cmd.id,
			ok: false,
			error: `Normal Chrome navigation did not yield a settled BOSS first page; pageState=${JSON.stringify(diagnostic)}`,
		};
	}

	if (!previous || previous.tabId !== tabId || previous.url !== cmd.url || previous.page + 1 !== cmd.page) {
		return { id: cmd.id, ok: false, error: 'Page capture must be sequential and start at page 1' };
	}
	const currentTab = await chrome.tabs.get(tabId);
	if (isBossVerificationUrl(currentTab.url || '')) {
		return { id: cmd.id, ok: false, error: bossRiskError(currentTab.url || '') };
	}
	if (!isExpectedBossSearchPage(currentTab.url || '', cmd.url)) {
		return { id: cmd.id, ok: false, error: 'PAGE_STATE_MISMATCH: radar tab changed city, query, or route' };
	}
	const trigger = async () => {
			await evaluate(
				tabId,
				'window.scrollTo(0, document.documentElement.scrollHeight); true',
			);
			await new Promise(resolve => setTimeout(resolve, 750));
			const state = await evaluate(tabId, `(() => {
				const component = document.querySelector('.loading-wait')?.__vue__?.$parent;
				if (!component || typeof component.loadMoreHandle !== 'function') {
					return { ok: false, reason: 'job list loader is unavailable' };
				}
				const expected = new URL(${JSON.stringify(cmd.url)});
				const form = component.formData || {};
				if (String(form.city || '') !== String(expected.searchParams.get('city') || '')
					|| String(form.query || '') !== String(expected.searchParams.get('query') || '')) {
					return { ok: false, reason: 'PAGE_STATE_MISMATCH: visible city or query changed' };
				}
				if (typeof component.hasMore !== 'boolean') {
					return { ok: false, reason: 'PAGE_STATE_LOST: hasMore is missing' };
				}
				const currentPage = Number(component.pageVo?.page || 0);
				if (currentPage !== ${cmd.page - 1}) {
					return { ok: false, reason: 'unexpected current page ' + currentPage };
				}
				if (component.hasMore === false) {
					return { ok: false, reason: 'the page reports no more jobs' };
				}
				component.loadMoreHandle();
				return { ok: true };
			})()`);
			if (!state?.ok) {
				throw new Error(`Cannot load BOSS result page ${cmd.page}: ${state?.reason || 'unknown error'}`);
			}
	};

	const data = await waitForJobList(tabId, cmd.url, cmd.page, trigger);
	// Propagate the platform response without advancing the successful-page
	// checkpoint. The caller maps risk/auth failures and stops the workflow.
	if (!data || data.code !== 0) {
		return { id: cmd.id, ok: true, data };
	}
	if (!data.zpData || !Array.isArray(data.zpData.jobList) || typeof data.zpData.hasMore !== 'boolean') {
		return { id: cmd.id, ok: false, error: 'INVALID_RESPONSE: incomplete joblist success envelope' };
	}
	const settledTab = await chrome.tabs.get(tabId);
	if (!isExpectedBossSearchPage(settledTab.url || '', cmd.url)) {
		return { id: cmd.id, ok: false, error: 'PAGE_STATE_MISMATCH: radar tab changed while loading' };
	}
	pageCaptureState.set(workspace, { tabId, url: cmd.url, page: cmd.page });
	return { id: cmd.id, ok: true, data };
}

async function handleCloseWindow(cmd) {
	let closedAutomationWindow = false;
	if (automationWindowId) {
		try { await chrome.windows.remove(automationWindowId); } catch {}
		automationWindowId = null;
		closedAutomationWindow = true;
	}
	for (const tabId of [...attached]) {
		try { await chrome.debugger.detach({ tabId }); } catch {}
		attached.delete(tabId);
	}
	pageCaptureState.clear();
	return {
		id: cmd.id,
		ok: true,
		data: {
			closedAutomationWindow,
			preservedRadarTab: Boolean(await getClaimedRadarTab()),
		},
	};
}

// ── Lifecycle ────────────────────────────────────────────────────────

let initialized = false;

function initialize() {
	if (initialized) return;
	initialized = true;
	chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });
	chrome.tabs.onRemoved.addListener(async (tabId) => {
		attached.delete(tabId);
		if (tabId === await loadClaimedRadarTabId()) await clearClaimedRadarTab();
	});
	chrome.debugger.onDetach.addListener((source) => { if (source.tabId) attached.delete(source.tabId); });
	connect();
	console.log('[boss-bridge] Extension initialized');
}

chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === 'keepalive') connect(); });

// Popup status API
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
	if (msg?.type === 'getStatus') {
		radarTabStatus().then(status => sendResponse({
			connected: ws?.readyState === WebSocket.OPEN,
			radarTab: status,
		}));
		return true;
	}
	if (msg?.type === 'claimRadarTab') {
		claimActiveRadarTab()
			.then(async () => sendResponse({
				ok: true,
				status: {
					connected: ws?.readyState === WebSocket.OPEN,
					radarTab: await radarTabStatus(),
				},
			}))
			.catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
		return true;
	}
	return false;
});
