import test from 'node:test';
import assert from 'node:assert/strict';
import { handleRequest } from '../src/worker.js';

const origin = 'https://alessandro-veneri.github.io';

function request(body, extraHeaders = {}) {
  return new Request('https://worker.example/v1/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: origin, ...extraHeaders },
    body: JSON.stringify(body)
  });
}

function env(overrides = {}) {
  const manifest = {
    papers: [{
      id: 'digital-advertising-auctions',
      status: 'published',
      approved: true,
      context: 'digital-advertising-auctions/paper-context.json'
    }]
  };
  const context = {
    schemaVersion: 1,
    blocks: [{ anchor: 'result-1', text: 'Greater data access softens competition and raises both bidders expected payoff.' }]
  };
  return {
    ALLOWED_ORIGIN: origin,
    PAPER_SITE_BASE: origin,
    GEMINI_API_KEY: 'test-key',
    ENVIRONMENT: 'test',
    QUOTA: {
      idFromName: (value) => value,
      get: () => ({ fetch: async () => Response.json({ allowed: true, dailyRemaining: 9 }) })
    },
    TEST_FETCH: async (url) => {
      const value = String(url);
      if (value.endsWith('/papers/manifest.json')) return Response.json(manifest);
      if (value.endsWith('/paper-context.json')) return Response.json(context);
      if (value.includes('generativelanguage.googleapis.com')) {
        return Response.json({
          candidates: [{
            finishReason: 'STOP',
            content: { parts: [{ text: JSON.stringify({
              answer: 'It softens competition.',
              sourceQuote: 'Greater data access softens competition',
              sourceAnchor: 'wrong-anchor'
            }) }] }
          }],
          usageMetadata: { promptTokenCount: 100, candidatesTokenCount: 20 }
        });
      }
      return new Response('not found', { status: 404 });
    },
    ...overrides
  };
}

test('returns a grounded answer and corrects the source anchor', async () => {
  const response = await handleRequest(request({
    paperId: 'digital-advertising-auctions',
    question: 'What happens when data access increases?',
    history: []
  }), env());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), origin);
  const data = await response.json();
  assert.equal(data.answer, 'It softens competition.');
  assert.equal(data.sourceAnchor, 'result-1');
});

test('rejects an untrusted origin', async () => {
  const response = await handleRequest(request({ paperId: 'x', question: 'x', history: [] }, { Origin: 'https://evil.example' }), env());
  assert.equal(response.status, 403);
});

test('rejects caller-controlled model or paper fields', async () => {
  const response = await handleRequest(request({
    paperId: 'digital-advertising-auctions', question: 'Question', history: [], model: 'paid-model'
  }), env());
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /unknown/);
});

test('rejects invalid and oversized history', async () => {
  const history = Array.from({ length: 7 }, () => ({ role: 'user', text: 'x' }));
  const response = await handleRequest(request({ paperId: 'digital-advertising-auctions', question: 'Question', history }), env());
  assert.equal(response.status, 400);
});

test('production rejects draft papers', async () => {
  const base = env();
  base.TEST_FETCH = async (url) => {
    if (String(url).endsWith('/papers/manifest.json')) {
      return Response.json({ papers: [{ id: 'digital-advertising-auctions', status: 'draft', approved: false, context: 'digital-advertising-auctions/paper-context.json' }] });
    }
    throw new Error('context should not be fetched');
  };
  base.ENVIRONMENT = 'production';
  const response = await handleRequest(request({ paperId: 'digital-advertising-auctions', question: 'Question', history: [] }), base);
  assert.equal(response.status, 404);
});

test('enforces quota rejection', async () => {
  const limited = env({
    QUOTA: {
      idFromName: (value) => value,
      get: () => ({ fetch: async () => Response.json({ allowed: false, reason: 'daily', dailyRemaining: 0 }) })
    }
  });
  const response = await handleRequest(request({ paperId: 'digital-advertising-auctions', question: 'Question', history: [] }), limited);
  assert.equal(response.status, 429);
});

test('maps Gemini free-tier exhaustion to temporary unavailability', async () => {
  const exhausted = env();
  const regularFetch = exhausted.TEST_FETCH;
  exhausted.TEST_FETCH = async (url, init) => {
    if (String(url).includes('generativelanguage.googleapis.com')) {
      return new Response(JSON.stringify({ error: { status: 'RESOURCE_EXHAUSTED' } }), { status: 429 });
    }
    return regularFetch(url, init);
  };
  const response = await handleRequest(request({ paperId: 'digital-advertising-auctions', question: 'Question', history: [] }), exhausted);
  assert.equal(response.status, 503);
  assert.match((await response.json()).error, /quota/);
});
