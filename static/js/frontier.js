// frontier.js — daily arXiv quant-research digests
// Routes:
//   #/frontier              list of paper cards
//   #/frontier/<arxiv_id>   single paper digest (markdown rendered)

async function renderFrontierPage() {
  const app = document.getElementById('app');
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  app.innerHTML = `
    <section class="section explore-section">
      <div class="explore-header">
        <h1 class="explore-title">${lang === 'zh' ? '前沿' : 'Frontier'}</h1>
        <p class="explore-tagline">${lang === 'zh'
          ? '每日自动追踪 arXiv q-fin / cs.LG 量化相关论文，并评估其在我们系统中的应用价值。'
          : 'Daily auto-tracked arXiv q-fin / cs.LG quant research, with relevance assessments for our system.'}</p>
      </div>
      <div id="frontier-list" class="explore-list">
        <p style="color:var(--text-secondary);">${lang === 'zh' ? '加载中…' : 'Loading…'}</p>
      </div>
    </section>
  `;
  try {
    const res = await fetch('/api/frontier');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const papers = (data.papers || []);
    const list = document.getElementById('frontier-list');
    if (!papers.length) {
      list.innerHTML = `<p style="color:var(--text-secondary);">${lang === 'zh' ? '暂无论文' : 'No papers yet'}</p>`;
      return;
    }
    list.innerHTML = papers.map(p => {
      const title = lang === 'zh' ? (p.title_zh || p.title_en) : (p.title_en || p.title_zh);
      const summary = lang === 'zh' ? (p.summary_zh || p.summary_en) : (p.summary_en || p.summary_zh);
      const cat = (p.categories || []).slice(0, 2).join(' · ');
      const tags = (p.tags || []).map(t => `<span class="explore-tag">#${escapeHtml(t)}</span>`).join('');
      const verdict = p.relevance_score
        ? `<span class="explore-card-cat" style="background:rgba(80,200,120,0.15);">${escapeHtml(p.relevance_score)}</span>`
        : '';
      const img = p.image
        ? `<div class="explore-card-img"><img src="${window.BASE}/static/frontier/${encodeURIComponent(p.arxiv_id)}/${encodeURIComponent(p.image)}" alt=""></div>`
        : '';
      return `
        <a class="glass-card explore-card" href="#/frontier/${encodeURIComponent(p.arxiv_id)}">
          ${img}
          <div class="explore-card-body">
            <div class="explore-card-meta">
              <span class="explore-card-cat">${escapeHtml(cat || '')}</span>
              ${verdict}
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
    document.getElementById('frontier-list').innerHTML =
      `<p style="color:var(--negative);">${lang === 'zh' ? '加载失败' : 'Failed to load'} — ${escapeHtml(e.message)}</p>`;
  }
}

async function renderFrontierPost(arxivId) {
  const app = document.getElementById('app');
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  app.innerHTML = `
    <section class="section explore-post-section">
      <a href="#/frontier" class="explore-back">← ${lang === 'zh' ? '返回前沿' : 'Back to Frontier'}</a>
      <div class="glass-card explore-post-card">
        <div id="frontier-post-body" class="intro-body explore-post-body">
          <p style="color:var(--text-secondary);">${lang === 'zh' ? '加载中…' : 'Loading…'}</p>
        </div>
      </div>
    </section>
  `;
  try {
    const res = await fetch(`/api/frontier/${encodeURIComponent(arxivId)}?lang=${encodeURIComponent(lang)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const md = await res.text();
    const body = document.getElementById('frontier-post-body');
    if (window.marked) {
      marked.setOptions({ breaks: false, gfm: true });
      const html = marked.parse(md).replace(
        /<img\s+([^>]*?)src="(?!https?:|\/)([^"]+)"/g,
        (_, pre, src) => `<img ${pre}src="${window.BASE}/static/frontier/${encodeURIComponent(arxivId)}/${src}"`
      );
      body.innerHTML = html;
      if (window.renderMathInElement) {
        try {
          renderMathInElement(body, {
            delimiters: [
              { left: '$$', right: '$$', display: true },
              { left: '\\[', right: '\\]', display: true },
              { left: '$',  right: '$',  display: false },
              { left: '\\(', right: '\\)', display: false },
            ],
            throwOnError: false,
            ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
          });
        } catch (err) { console.warn('KaTeX render failed:', err); }
      }
    } else {
      body.innerHTML = '<pre>' + md.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</pre>';
    }
  } catch (e) {
    document.getElementById('frontier-post-body').innerHTML =
      `<p style="color:var(--negative);">${lang === 'zh' ? '加载失败' : 'Failed to load'} — ${escapeHtml(e.message)}</p>`;
  }
}

window.renderFrontierPage = renderFrontierPage;
window.renderFrontierPost = renderFrontierPost;
