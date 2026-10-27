/* Pagina di accesso: invio della password (solo se WEB_PASSWORD è impostata) */
(() => {
  'use strict';
  const form = document.getElementById('loginform');
  if (!form) return;
  const err = document.getElementById('loginerr');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    try {
      const r = await fetch('/api/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'edith' },
        body: JSON.stringify({ password: document.getElementById('pw').value }),
      });
      const j = await r.json();
      if (r.ok) { location.href = '/'; return; }
      err.textContent = j.error || 'Accesso non riuscito.'; err.hidden = false;
    } catch (x) { err.textContent = 'Server non raggiungibile.'; err.hidden = false; }
  });
})();
