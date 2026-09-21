(function () {
  if (window.__kunigamiVoiceCallWatcher) return;
  window.__kunigamiVoiceCallWatcher = true;
  const path = window.location.pathname;
  if (/^\/(?:login|register|forgot_password)(?:\/|$)/.test(path)) return;

  let checking = false;
  async function checkVoiceCall() {
    if (checking || document.visibilityState === 'hidden' || window.location.pathname.startsWith('/call/')) return;
    checking = true;
    try {
      const response = await fetch('/api/calls/pending', {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      const call = data && data.call;
      if (!call || !call.call_id) return;
      if (call.status === 'active' || (call.status === 'ringing' && call.initiator === 'assistant')) {
        window.location.assign('/call/' + encodeURIComponent(call.call_id));
      }
    } catch (_) {
      // Ordinary page activity must not be interrupted by a failed background poll.
    } finally {
      checking = false;
    }
  }
  window.addEventListener('focus', checkVoiceCall);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkVoiceCall();
  });
  setTimeout(checkVoiceCall, 500);
  setInterval(checkVoiceCall, 3000);
})();
