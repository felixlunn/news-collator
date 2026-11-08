// Run with: cd worker && npm test   (Node 22+; no dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker, { buildQuery, cleanTerms, isUkSource, parseGoogleNews } from "../src/index.js";

const RSS = readFileSync(new URL("../../tests/fixtures/google_news.xml", import.meta.url), "utf8");
const ENV = { ALLOWED_ORIGINS: "https://felixlunn.github.io" };

test("terms are cleaned and capped", () => {
  assert.deepEqual(cleanTerms(' sanction*, =OFSI, "asset freeze", ab, '), ["sanction", "OFSI", "asset freeze"]);
  assert.equal(cleanTerms(Array(20).fill("word").join(",")).length, 10);
});

test("query matches the Python google_news_query format", () => {
  assert.equal(buildQuery(["sanction", "asset freeze"], ["sport"]), '(sanction OR "asset freeze") -sport when:3d');
  assert.equal(buildQuery(["Iran"], []), "Iran when:3d");
});

test("UK outlets only", () => {
  for (const ok of ["https://www.bbc.co.uk", "theguardian.com", "news.sky.com", "https://www.walesonline.co.uk"])
    assert.ok(isUkSource(ok), ok);
  for (const no of ["https://www.cnn.com", "reuters.com", "theguardian.com.evil.io", ""])
    assert.ok(!isUkSource(no), no);
});

test("Google News RSS is parsed and non-UK results dropped", () => {
  const items = parseGoogleNews(RSS);
  assert.deepEqual(items.map((i) => [i.t, i.o]), [
    ["Iran warns UK over sanctions on Revolutionary Guard", "BBC News"],
    ["UK sanctions Iranian officials over crackdown", "The Times"],
    ["Ministers respond to Iran sanctions report", "manchestereveningnews.co.uk"],
  ]);
  assert.equal(items[0].p, "2026-11-08T16:00:00.000Z");
  assert.equal(items[0].k, "search");
});

test("other websites are refused; the site and direct visits are served", async () => {
  const req = (origin) => new Request("https://w.example/search?q=", { headers: origin ? { Origin: origin } : {} });
  assert.equal((await worker.fetch(req("https://evil.example"), ENV)).status, 403);
  const own = await worker.fetch(req("https://felixlunn.github.io"), ENV);
  assert.equal(own.status, 400);  // allowed through, but no terms given
  assert.equal(own.headers.get("Access-Control-Allow-Origin"), "https://felixlunn.github.io");
  assert.equal((await worker.fetch(req(null), ENV)).status, 400);
});

test("a search calls Google News UK and returns UK items", async (t) => {
  let called;
  t.mock.method(globalThis, "fetch", async (url) => { called = url; return new Response(RSS, { status: 200 }); });
  const res = await worker.fetch(new Request("https://w.example/search?q=Iran,Tehran&x=football",
    { headers: { Origin: "https://felixlunn.github.io" } }), ENV);
  const body = await res.json();
  assert.equal(res.status, 200);
  assert.match(called, /news\.google\.com\/rss\/search\?q=%28Iran\+OR\+Tehran%29\+-football\+when%3A3d&hl=en-GB&gl=GB/);
  assert.equal(body.items.length, 3);
});

test("upstream failures are reported, not thrown", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("busy", { status: 503 }));
  const res = await worker.fetch(new Request("https://w.example/search?q=Iran"), ENV);
  assert.equal(res.status, 502);
  assert.deepEqual((await res.json()).items, []);
});
