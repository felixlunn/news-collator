/**
 * Despatch search Worker (Cloudflare Workers).
 *
 * Lets personal tags on the Channels page search UK outlets live. The site is
 * static, so the browser calls this Worker, which queries Google News (UK
 * edition) and returns only results from UK outlets.
 *
 *   GET /search?q=term one,term two&x=excluded term
 *   -> { "query": "...", "items": [{ "u", "t", "o", "p", "k": "search" }] }
 *
 * Safeguards:
 *  - Answers only pages from ALLOWED_ORIGINS (wrangler.toml), so other sites
 *    can't spend this Worker's free daily quota. Direct visits (no Origin
 *    header) are allowed for testing.
 *  - Caps the number and length of terms.
 *  - UK-only: the same rule and outlet list (config/uk_outlets.json) as the
 *    Python side's tags.is_uk_source.
 */

import OUTLET_DATA from "../../config/uk_outlets.json" with { type: "json" };

const UK_OUTLETS = OUTLET_DATA.outlets;
const EXCLUDED = OUTLET_DATA.excluded || [];  // never returned, even on .uk domains

const GOOGLE_NEWS_RSS = "https://news.google.com/rss/search";
const MAX_TERMS = 10;
const MAX_TERM_LENGTH = 60;
const WINDOW_DAYS = 3;          // matches the site's 72-hour window
const BROWSER_CACHE_SECONDS = 600;

// ------------------------------------------------------------------ queries

/** Clean user input into search terms: trimmed, no wildcards/case markers, capped. */
export function cleanTerms(raw) {
  return String(raw || "")
    .split(",")
    .map((t) => t.replace(/[\u0000-\u001f"]/g, " ").trim().replace(/^=+/, "").replace(/\*+$/, "").trim())
    .filter((t) => t.length >= 3)
    .slice(0, MAX_TERMS)
    .map((t) => t.slice(0, MAX_TERM_LENGTH));
}

/** Google News syntax, as tags.google_news_query: (a OR "b c") -x when:3d */
export function buildQuery(terms, exclude) {
  const quote = (t) => (t.includes(" ") ? `"${t}"` : t);
  const joined = terms.length === 1 ? quote(terms[0]) : `(${terms.map(quote).join(" OR ")})`;
  return [joined, ...exclude.map((t) => `-${quote(t)}`), `when:${WINDOW_DAYS}d`].join(" ");
}

// ------------------------------------------------------------------ UK outlets

function domainOf(urlOrDomain) {
  let host = String(urlOrDomain || "");
  try {
    if (host.includes("//")) host = new URL(host).hostname;
  } catch {
    return "";
  }
  return host.toLowerCase().split(":")[0].replace(/^www\./, "");
}

/** A .uk domain, or a known UK outlet on another domain. */
export function isUkSource(urlOrDomain) {
  const d = domainOf(urlOrDomain);
  if (!d || EXCLUDED.some((x) => d === x || d.endsWith("." + x))) return false;
  if (d.endsWith(".uk")) return true;
  return Object.keys(UK_OUTLETS).some((k) => d === k || d.endsWith("." + k));
}

function outletName(urlOrDomain) {
  const d = domainOf(urlOrDomain);
  for (const [k, name] of Object.entries(UK_OUTLETS)) {
    if (d === k || d.endsWith("." + k)) return name;
  }
  return d || "Web";
}

// ------------------------------------------------------------------ RSS parsing
// Workers have no DOM parser; Google News RSS is regular enough for this.

function decode(s) {
  return s
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
    .trim();
}

function tag(block, name) {
  const m = new RegExp(`<${name}\\b[^>]*>([\\s\\S]*?)</${name}>`).exec(block);
  return m ? decode(m[1]) : "";
}

/** Google News RSS -> items from UK outlets only. */
export function parseGoogleNews(xml) {
  const items = [];
  for (const [, block] of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)) {
    const sourceUrl = (/<source\s+url="([^"]+)"/.exec(block) || [])[1] || "";
    if (!isUkSource(sourceUrl)) continue;
    const link = tag(block, "link");
    let title = tag(block, "title");
    const cut = title.lastIndexOf(" - ");  // titles end " - Outlet"
    if (cut > 0) title = title.slice(0, cut).trim();
    if (!/^https?:\/\//.test(link) || !title) continue;
    const date = new Date(tag(block, "pubDate"));
    items.push({
      u: link, t: title, o: outletName(sourceUrl),
      p: isNaN(date) ? null : date.toISOString(), k: "search",
    });
  }
  return items;
}

// ------------------------------------------------------------------ HTTP

function allowedOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return { ok: true, header: null };  // direct visit, e.g. testing in a browser tab
  const allowed = String(env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return { ok: allowed.includes(origin), header: origin };
}

function json(body, status, origin, extra = {}) {
  const headers = { "Content-Type": "application/json; charset=utf-8", ...extra };
  if (origin) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Vary"] = "Origin";
  }
  return new Response(JSON.stringify(body), { status, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = allowedOrigin(request, env);

    if (!origin.ok) return json({ error: "This search service only answers the Despatch website." }, 403, null);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: {
        "Access-Control-Allow-Origin": origin.header || "*", "Access-Control-Allow-Methods": "GET",
        "Access-Control-Max-Age": "86400", "Vary": "Origin" } });
    }
    if (request.method !== "GET" || url.pathname !== "/search") {
      return json({ error: "Use GET /search?q=term,term" }, 404, origin.header);
    }

    const terms = cleanTerms(url.searchParams.get("q"));
    const exclude = cleanTerms(url.searchParams.get("x"));
    if (!terms.length) return json({ error: "Give at least one search term of 3 or more characters." }, 400, origin.header);

    const query = buildQuery(terms, exclude);
    const params = new URLSearchParams({ q: query, hl: "en-GB", gl: "GB", ceid: "GB:en" });
    let upstream;
    try {
      upstream = await fetch(`${GOOGLE_NEWS_RSS}?${params}`, {
        headers: { "User-Agent": "DespatchSearch/1.0", Accept: "application/rss+xml, application/xml" },
      });
    } catch (e) {
      return json({ query, items: [], error: "The news search could not be reached." }, 502, origin.header);
    }
    if (!upstream.ok) {
      return json({ query, items: [], error: `The news search answered ${upstream.status}. Try again shortly.` },
                  502, origin.header);
    }
    const items = parseGoogleNews(await upstream.text());
    return json({ query, items }, 200, origin.header, { "Cache-Control": `public, max-age=${BROWSER_CACHE_SECONDS}` });
  },
};
