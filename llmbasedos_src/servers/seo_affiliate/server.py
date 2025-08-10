import os, asyncio, httpx, json, re, hashlib, time, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List
from llmbasedos_src.mcp_server_framework import MCPServer

SEO = MCPServer("seo_affiliate", __file__.replace("server.py","caps.json"))

# --- Config via ENV ---
LLM_URL   = os.getenv("LLM_ROUTER_URL", "http://gateway:8000/llm/chat")   # ton router LLM
BASE_DIR  = Path(os.getenv("SEO_AFF_BASE", "/data/sites")).resolve()      # où écrire le site
SITE_HOST = os.getenv("SEO_AFF_HOST", "https://example.github.io")        # pour sitemap (change-le quand tu pushes)

# --- Utils ---
def slugify(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-zA-Z0-9\s-]', '', text).strip().lower()
    text = re.sub(r'\s+', '-', text)
    return text[:80] or hashlib.sha1(text.encode()).hexdigest()[:12]

def html_escape(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
             .replace('"',"&quot;").replace("'","&#39;"))

async def llm_json(system: str, prompt: str, temperature=0.5) -> Any:
    # On construit un payload standard, compatible OpenAI, que notre gateway comprend
    payload = {
        "messages": [{"role":"system","content":system},{"role":"user","content":prompt}],
        "temperature": temperature,
        "response_format": "json", # On indique notre intention
        "meta": {"purpose": "copy"}
    }
    
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(LLM_URL, json=payload)
        r.raise_for_status()
        openai_response = r.json()

        # --- LOGIQUE DE PARSING CORRIGÉE ET ROBUSTE ---
        try:
            # 1. On va chercher le contenu textuel dans la structure OpenAI
            content_str = openai_response["choices"][0]["message"]["content"]
            
            # 2. On nettoie les "tics" des LLMs (comme les ```json)
            if content_str.strip().startswith("```json"):
                content_str = content_str.strip()[7:-3]
            elif content_str.strip().startswith("```"):
                 content_str = content_str.strip()[3:-3]
            
            # 3. On parse la chaîne nettoyée
            return json.loads(content_str)
            
        except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
            SEO.logger.error(f"Could not parse LLM JSON response: {e}. Full response: {openai_response}")
            # En cas d'échec, on retourne un dictionnaire vide pour éviter de planter
            return {}

def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# --- Minimal theme (no deps) ---
HTML_LAYOUT = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{meta_desc}">
<link rel="canonical" href="{canonical}">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:780px;margin:30px auto;padding:0 16px;line-height:1.6}}
h1{{font-size:1.9rem;margin-bottom:.2rem}}
small, time{{color:#666}}
a.btn{{display:inline-block;margin:.6rem 0;padding:.55rem .9rem;border:1px solid #333;border-radius:8px;text-decoration:none}}
hr{{border:none;border-top:1px solid #eee;margin:1.2rem 0}}
.code{{background:#f7f7f7;padding:.2rem .35rem;border-radius:4px}}
.faq h3{{margin:.9rem 0 .2rem}}
footer{{margin:3rem 0 2rem;color:#777;font-size:.9rem}}
</style>
<script type="application/ld+json">
{schema_org}
</script>
</head>
<body>
<header>
  <h1>{h1}</h1>
  <small>{subtitle}</small>
</header>

<article>
{content_html}
<p><a class="btn" href="{affiliate}" rel="sponsored nofollow">Tester l’app recommandée →</a></p>
</article>

<section class="faq">
  <h2>FAQ</h2>
  {faq_html}
</section>

<hr/>
<nav><a href="{root}/index.html">Accueil</a></nav>
<footer>© {year} • Généré par une Sentinelle adoptan.ai</footer>
</body>
</html>
"""

INDEX_HTML = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{site_title}</title><meta name="description" content="{site_desc}">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:900px;margin:30px auto;padding:0 16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
.card{{padding:14px;border:1px solid #eee;border-radius:10px}}
.card a{{text-decoration:none;font-weight:600}}
small{{color:#666}}
</style>
</head>
<body>
  <h1>{site_title}</h1>
  <p>{site_desc}</p>
  <div class="grid">
  {cards}
  </div>
  <hr/>
  <small>MAJ: {updated}</small>
</body>
</html>
"""

# --- LLM prompts ---
SYSTEM = "Tu es un rédacteur SEO français. Tu écris clair, utile, sans promesses ni garanties. Tu renvoies STRICTEMENT du JSON valide."
PROMPT_ARTICLE = """Sujet principal: "{keyword}"

Crée un article SEO court et efficace pour un mini-site d'affiliation.
Retourne strictement ce JSON:
{{
 "title": "...",
 "meta_description": "... (<= 160 chars)",
 "h1": "...",
 "subtitle": "...",
 "sections": [
   {{"h2":"", "html":"<p>... paragraphe(s) courts en HTML ...</p>"}}
 ],
 "faq": [
   {{"q":"Question ?", "a_html":"<p>Réponse courte en HTML.</p>"}}
 ],
 "schema_org": {{
   "@context": "https://schema.org",
   "@type": "Article",
   "headline": "...",
   "datePublished": "{now}",
   "inLanguage": "{lang}"
 }}
}}
Contraintes:
- 600–900 mots
- HTML simple uniquement pour le corps et FAQ (p, ul/li, strong, code)
- aucun langage marketing agressif; pas de promesse de gains garantis
- n'intègre pas le lien d'affiliation dans le texte (il sera sous l'article)
"""

@SEO.register_method("mcp.seo_aff.generate_site")
async def generate_site(_server: MCPServer, _id, params: List[Any]):
    p = params[0]
    site_id  = p["site_id"]
    keyword  = p["keyword"]
    aff_link = p["affiliate_link"]
    n_articles = int(p.get("n_articles", 3))
    lang = p.get("lang","fr")
    dry  = bool(p.get("dry", True))

    site_root = BASE_DIR / site_id
    posts_dir = site_root / "posts"
    out_index = site_root / "index.html"
    out_map   = site_root / "sitemap.xml"
    out_rss   = site_root / "feed.xml"

    topics_prompt = f"""Donne {n_articles} variantes de sujets spécifiques autour de: "{keyword}".
Réponds en JSON array de chaînes courtes, informatives, sans emojis."""
    topics = await llm_json(SYSTEM, topics_prompt, temperature=0.2)
    if not isinstance(topics, list):
        topics = [str(topics)]
    topics = [t.strip() for t in topics if isinstance(t, str) and t.strip()][:n_articles]
    if not topics:
        return {"ok": False, "error": "No topics produced by LLM."}

    pages = []
    now_iso = iso_now()

    # Generate each article
    for t in topics:
        art = await llm_json(SYSTEM, PROMPT_ARTICLE.format(keyword=t, now=now_iso, lang=lang), temperature=0.5)
        if not isinstance(art, dict):
            # fallback minimal
            art = {
                "title": t,
                "meta_description": t,
                "h1": t,
                "subtitle": "",
                "sections": [{"h2":"", "html":"<p>Contenu indisponible.</p>"}],
                "faq": [],
                "schema_org": {"@context":"https://schema.org","@type":"Article","headline":t,"datePublished":now_iso,"inLanguage":lang}
            }
        slug = slugify(art.get("title") or t)
        url  = f"{SITE_HOST}/{site_id}/posts/{slug}.html"

        content_html = ""
        for sec in art.get("sections", []):
            h2 = sec.get("h2","").strip()
            if h2:
                content_html += f"<h2>{html_escape(h2)}</h2>\n"
            content_html += sec.get("html","")

        faq_html = ""
        for qa in art.get("faq", []):
            faq_html += f"<h3>{html_escape(qa.get('q',''))}</h3>\n{qa.get('a_html','')}"

        html = HTML_LAYOUT.format(
            lang=lang,
            title=html_escape(art.get("title","")),
            meta_desc=html_escape(art.get("meta_description","")),
            canonical=url,
            schema_org=json.dumps(art.get("schema_org",{}), ensure_ascii=False),
            h1=html_escape(art.get("h1","")),
            subtitle=html_escape(art.get("subtitle","")),
            content_html=content_html,
            faq_html=faq_html,
            affiliate=aff_link,
            root=f"{SITE_HOST}/{site_id}",
            year=datetime.now().year
        )

        out_file = posts_dir / f"{slug}.html"
        if not dry:
            write_text(out_file, html)

        pages.append({"title": art.get("h1", t), "slug": slug, "url": url, "ts": now_iso})

    # Build index
    cards = "\n".join([
        f'<div class="card"><a href="posts/{p["slug"]}.html">{html_escape(p["title"])}</a><br/><small>publié {p["ts"][:10]}</small></div>'
        for p in pages
    ])
    idx_html = INDEX_HTML.format(
        lang=lang, site_title=f"{keyword} · Guide & Actu",
        site_desc=f"Mini-site d’affiliation sur {keyword}.",
        cards=cards, updated=iso_now()
    )
    if not dry:
        write_text(out_index, idx_html)

    # Build sitemap
    sitemap = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
               "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">",
               f"<url><loc>{SITE_HOST}/{site_id}/index.html</loc></url>"]
    for p in pages:
        sitemap.append(f"<url><loc>{html_escape(p['url'])}</loc></url>")
    sitemap.append("</urlset>")
    if not dry:
        write_text(out_map, "\n".join(sitemap))

    # Tiny RSS (optional)
    rss_items = []
    for p in pages:
        rss_items.append(f"<item><title>{html_escape(p['title'])}</title><link>{html_escape(p['url'])}</link><pubDate>{p['ts']}</pubDate></item>")
    rss = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel>
<title>{html_escape(keyword)} · Flux</title>
<link>{SITE_HOST}/{site_id}/index.html</link>
<description>Flux {html_escape(keyword)}</description>
{''.join(rss_items)}
</channel></rss>"""
    if not dry:
        write_text(out_rss, rss)

    return {
        "ok": True,
        "dry": dry,
        "site_id": site_id,
        "out_dir": str(site_root),
        "pages": pages,
        "next": [
            "Commit/push this folder to a GitHub Pages or Cloudflare Pages project.",
            "Add your custom domain or use default pages domain.",
            "Submit sitemap.xml to Google Search Console for faster indexing."
        ]
    }

if __name__ == "__main__":
    asyncio.run(SEO.start())
