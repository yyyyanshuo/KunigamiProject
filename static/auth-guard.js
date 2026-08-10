(function () {
  'use strict';

  let redirecting = false;

  window.handleApiUnauthorized = function handleApiUnauthorized(response) {
    if (!response || response.status !== 401) return false;
    if (redirecting) return true;
    redirecting = true;
    try {
      const returnTo = window.location.pathname + window.location.search + window.location.hash;
      if (returnTo && returnTo.startsWith('/') && !returnTo.startsWith('//')) {
        sessionStorage.setItem('post_login_return_to', returnTo);
      }
    } catch (e) {}
    window.location.replace('/login?reason=session_expired');
    return true;
  };

  window.fetchWithAuthGuard = async function fetchWithAuthGuard(input, init) {
    const response = await window.fetch(input, init);
    window.handleApiUnauthorized(response);
    return response;
  };
})();
