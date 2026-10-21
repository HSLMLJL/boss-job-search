const dot = document.getElementById('dot');
const label = document.getElementById('label');
const hint = document.getElementById('hint');
const claim = document.getElementById('claim');
const tab = document.getElementById('tab');

function renderStatus(res) {
	dot.className = `dot ${res?.connected ? 'on' : 'off'}`;
	label.textContent = res?.connected ? '已连接 daemon' : '未连接 daemon';
	hint.textContent = res?.connected
		? '可直接运行；程序会创建专用页。也可手动绑定当前结果页。'
		: '先运行岗位雷达，让本地 daemon 启动。';
	tab.textContent = res?.radarTab?.prepared
		? `已绑定：${res.radarTab.title || res.radarTab.url}`
		: '未手动绑定；运行时将创建专用页';
}

chrome.runtime.sendMessage({ type: 'getStatus' }, renderStatus);

claim.addEventListener('click', () => {
	claim.disabled = true;
	chrome.runtime.sendMessage({ type: 'claimRadarTab' }, (res) => {
		claim.disabled = false;
		if (!res?.ok) {
			tab.textContent = res?.error || '绑定失败';
			return;
		}
		renderStatus(res.status);
	});
});
