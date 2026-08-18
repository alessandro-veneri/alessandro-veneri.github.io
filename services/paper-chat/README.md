# Paper chat Worker

This Worker is the only component allowed to access the Gemini key. It accepts
a paper id, question, and at most three prior exchanges; fetches canonical
context for an approved paper; uses the fixed free-tier
`gemini-3.5-flash` model; verifies any returned quotation; and stores no
conversation content.

## Local development

Install Wrangler, configure a local secret, and start the Worker:

```sh
npm install
npx wrangler secret put GEMINI_API_KEY
npm run dev
```

For draft testing, use a local Wrangler environment with `ALLOW_DRAFTS=true`,
`PAPER_MANIFEST_URL=http://localhost:8000/manifest.json`, and
`PAPER_SITE_BASE=http://localhost:8000`. Never enable drafts in production.

## Production setup

1. Create a Gemini API key in a project with no billing account attached.
2. Create a narrowly scoped Cloudflare API token for this Worker.
3. Add `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as GitHub secrets.
4. Set `GEMINI_API_KEY` with `wrangler secret put GEMINI_API_KEY`.
5. Deploy the Worker and place its `/v1/ask` URL in `papers/manifest.json`.

The Durable Object applies 2 questions/minute and 10/day per hashed IP, plus a
50-question global daily ceiling. Gemini quota exhaustion returns a temporary
unavailability response and never falls back to a paid model.
