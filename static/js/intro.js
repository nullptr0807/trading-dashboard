// intro.js — render README.md fetched from /api/intro (bilingual via ?lang=)
async function renderIntroPage() {
  const app = document.getElementById('app');
  app.innerHTML = `
    <section class="section intro-section">
      <div class="glass-card intro-card">
        <div id="intro-body" class="intro-body">
          <p style="color:var(--text-secondary);">${(typeof t === 'function' ? t('intro_loading') : 'Loading…')}</p>
        </div>
      </div>
    </section>
  `;
  try {
    const lang = (typeof getLang === 'function') ? getLang() : 'en';
    const res = await fetch(`/api/intro?lang=${encodeURIComponent(lang)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const md = await res.text();
    const body = document.getElementById('intro-body');
    if (window.marked) {
      marked.setOptions({ breaks: false, gfm: true });
      body.innerHTML = marked.parse(md);
    } else {
      body.innerHTML = '<pre>' + md.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</pre>';
    }
  } catch (e) {
    document.getElementById('intro-body').innerHTML =
      `<p style="color:var(--negative);">${(typeof t === 'function' ? t('intro_error') : 'Failed to load')} — ${e.message}</p>`;
  }
}
window.renderIntroPage = renderIntroPage;
