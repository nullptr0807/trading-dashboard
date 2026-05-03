// explore.js — research articles section
// Routes:
//   #/explore           list of posts
//   #/explore/<slug>    single post (markdown rendered)

async function renderExplorePage() {
  const app = document.getElementById('app');
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  app.innerHTML = `
    <section class="section explore-section">
      <div class="explore-header">
        <h1 class="explore-title">${lang === 'zh' ? '探索' : 'Explore'}</h1>
        <p class="explore-tagline">${lang === 'zh'
          ? '关于量化、风控与系统设计的实验、探讨与总结。'
          : 'Experiments, discussions, and write-ups on quant, risk, and system design.'}</p>
      </div>
      <div id="explore-list" class="explore-list">
        <p style="color:var(--text-secondary);">${lang === 'zh' ? '加载中…' : 'Loading…'}</p>
      </div>
    </section>
  `;
  try {
    const res = await fetch('/api/explore');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const posts = (data.posts || []);
    const list = document.getElementById('explore-list');
    if (!posts.length) {
      list.innerHTML = `<p style="color:var(--text-secondary);">${lang === 'zh' ? '暂无文章' : 'No posts yet'}</p>`;
      return;
    }
    list.innerHTML = posts.map(p => {
      const title = lang === 'zh' ? (p.title_zh || p.title_en) : (p.title_en || p.title_zh);
      const summary = lang === 'zh' ? (p.summary_zh || p.summary_en) : (p.summary_en || p.summary_zh);
      const cat = lang === 'zh' ? (p.category_zh || p.category) : (p.category || p.category_zh);
      const tags = (p.tags || []).map(t => `<span class="explore-tag">#${escapeHtml(t)}</span>`).join('');
      const img = p.image
        ? `<div class="explore-card-img"><img src="/static/explore/${encodeURIComponent(p.slug)}/${encodeURIComponent(p.image)}" alt=""></div>`
        : '';
      return `
        <a class="glass-card explore-card" href="#/explore/${encodeURIComponent(p.slug)}">
          ${img}
          <div class="explore-card-body">
            <div class="explore-card-meta">
              <span class="explore-card-cat">${escapeHtml(cat || '')}</span>
              <span class="explore-card-date">${escapeHtml(p.date || '')}</span>
            </div>
            <h2 class="explore-card-title">${escapeHtml(title || '')}</h2>
            <p class="explore-card-summary">${escapeHtml(summary || '')}</p>
            <div class="explore-card-tags">${tags}</div>
          </div>
        </a>
      `;
    }).join('');
  } catch (e) {
    document.getElementById('explore-list').innerHTML =
      `<p style="color:var(--negative);">${lang === 'zh' ? '加载失败' : 'Failed to load'} — ${escapeHtml(e.message)}</p>`;
  }
}

async function renderExplorePost(slug) {
  const app = document.getElementById('app');
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  app.innerHTML = `
    <section class="section explore-post-section">
      <a href="#/explore" class="explore-back">← ${lang === 'zh' ? '返回 Explore' : 'Back to Explore'}</a>
      <div class="glass-card explore-post-card">
        <div id="explore-post-body" class="intro-body explore-post-body">
          <p style="color:var(--text-secondary);">${lang === 'zh' ? '加载中…' : 'Loading…'}</p>
        </div>
      </div>
    </section>
  `;
  try {
    const res = await fetch(`/api/explore/${encodeURIComponent(slug)}?lang=${encodeURIComponent(lang)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const md = await res.text();
    const body = document.getElementById('explore-post-body');
    if (window.marked) {
      marked.setOptions({ breaks: false, gfm: true });
      // Rewrite relative image paths (e.g. "sizing_comparison.png") to /static/explore/<slug>/...
      const html = marked.parse(md).replace(
        /<img\s+([^>]*?)src="(?!https?:|\/)([^"]+)"/g,
        (_, pre, src) => `<img ${pre}src="/static/explore/${encodeURIComponent(slug)}/${src}"`
      );
      body.innerHTML = html;
    } else {
      body.innerHTML = '<pre>' + md.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</pre>';
    }
  } catch (e) {
    document.getElementById('explore-post-body').innerHTML =
      `<p style="color:var(--negative);">${lang === 'zh' ? '加载失败' : 'Failed to load'} — ${escapeHtml(e.message)}</p>`;
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

window.renderExplorePage = renderExplorePage;
window.renderExplorePost = renderExplorePost;
