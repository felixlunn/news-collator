# Search Worker

A small [Cloudflare Worker](https://developers.cloudflare.com/workers/) that
lets personal tags on the Channels page search UK outlets live. The website is
static, so it has no server of its own; this Worker is that server.

- `GET /search?q=term,term&x=excluded` searches Google News (UK edition) for
  the last three days and returns only results from UK outlets (any `.uk`
  domain, plus the outlets in `config/uk_outlets.json`).
- It only answers the Despatch website (`ALLOWED_ORIGINS` in `wrangler.toml`),
  so other sites can't use up its quota.
- Cloudflare's free plan allows 100,000 requests a day; each channel view
  makes at most one request per personal tag (five at most).

## One-off setup

1. **Create a Cloudflare account** at <https://dash.cloudflare.com/sign-up>
   (free; no card needed for Workers).
2. **Open Workers & Pages** in the dashboard once, and choose your
   `workers.dev` subdomain if asked. The Worker's address will be
   `https://despatch-search.<your-subdomain>.workers.dev`.
3. **Copy your Account ID**, shown on the Workers & Pages overview page
   (or see Cloudflare's guide *Find account and zone IDs*).
4. **Create an API token**: Manage account → *Account API Tokens* →
   *Create Token*, use the **Edit Cloudflare Workers** template, and limit it
   to your account. Copy the token; it's shown only once.
5. **Add both to GitHub**: in this repo, *Settings → Secrets and variables →
   Actions → New repository secret*:
   - `CLOUDFLARE_API_TOKEN` — the token
   - `CLOUDFLARE_ACCOUNT_ID` — the account ID
6. **Deploy**: *Actions → Deploy search worker → Run workflow*. The run's
   summary shows the Worker's address.
7. **Switch it on for the site**: put that address in `config/tags.toml`
   under `[search]` as `live_search_url`, and commit. The next site build
   turns on live search for personal tags.

After that, any change under `worker/` redeploys automatically.

## Changing it

- Allow another site to use it: add its origin to `ALLOWED_ORIGINS` in
  `wrangler.toml` (comma-separated, no trailing slash).
- Tests: `cd worker && npm test` (Node 22+, no dependencies). The workflow
  runs them, and bundles the Worker, before every deploy.
- To switch live search off, empty `live_search_url` in `config/tags.toml`.
  You can also delete the Worker under *Workers & Pages* in Cloudflare.
