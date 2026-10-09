#!/usr/bin/env node
// Additively register embry0's OAuth callback, logout, and web-origin URLs
// on an Auth0 confidential client. Management API credentials come from the
// file named by AUTH0_MGMT_ENV. The client id and public origin come from
// AUTH0_CLIENT_ID and SSO_PUBLIC_ORIGIN. GET -> append-only -> PATCH;
// refuses any non-additive change. Safe to re-run (idempotent). Prints URL
// lists only, never secrets. (RAV-1069)
import fs from 'fs';

const MGMT_ENV = process.env.AUTH0_MGMT_ENV;
if (!MGMT_ENV) {
  console.error('AUTH0_MGMT_ENV is required (path to a file with AUTH0_MGMT_*)');
  process.exit(1);
}
const CLIENT = (process.env.AUTH0_CLIENT_ID || '').trim();
if (!CLIENT) { console.error('AUTH0_CLIENT_ID is required'); process.exit(1); }
const ORIGIN = (process.env.SSO_PUBLIC_ORIGIN || '').trim().replace(/\/+$/, '');
if (!ORIGIN) { console.error('SSO_PUBLIC_ORIGIN is required'); process.exit(1); }
const ADD_CB = `${ORIGIN}/api/v1/auth/callback`;
const ADD_LO = [ORIGIN, `${ORIGIN}/`];
const ADD_WO = ORIGIN;

const env = Object.fromEntries(
  fs.readFileSync(MGMT_ENV, 'utf8').split('\n')
    .filter(l => l.includes('=') && !l.trim().startsWith('#'))
    .map(l => { const i = l.indexOf('='); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; })
);
const { AUTH0_MGMT_DOMAIN: DOMAIN, AUTH0_MGMT_CLIENT_ID: CID, AUTH0_MGMT_CLIENT_SECRET: SEC } = env;
if (!DOMAIN || !CID || !SEC) { console.error('missing AUTH0_MGMT_* creds in', MGMT_ENV); process.exit(1); }

const tok = await (await fetch(`https://${DOMAIN}/oauth/token`, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ client_id: CID, client_secret: SEC, audience: `https://${DOMAIN}/api/v2/`, grant_type: 'client_credentials' })
})).json();
if (!tok.access_token) { console.error('mgmt token mint failed:', tok.error || tok); process.exit(1); }
console.log('mgmt token scopes:', tok.scope || '(none reported)');
const H = { authorization: `Bearer ${tok.access_token}` };

const cur = await (await fetch(`https://${DOMAIN}/api/v2/clients/${CLIENT}?fields=callbacks,allowed_logout_urls,web_origins,grant_types,name`, { headers: H })).json();
if (cur.statusCode) { console.error('GET client failed:', cur); process.exit(1); }
console.log('CLIENT:', cur.name, '| grant_types:', JSON.stringify(cur.grant_types));

const uniq = a => [...new Set(a)];
const cb = uniq([...(cur.callbacks || []), ADD_CB]);
const lo = uniq([...(cur.allowed_logout_urls || []), ...ADD_LO]);
const wo = uniq([...(cur.web_origins || []), ADD_WO]);
console.log('callbacks  : +', ADD_CB, (cur.callbacks || []).includes(ADD_CB) ? '(already present)' : '(new)');
console.log('logout_urls: +', JSON.stringify(ADD_LO));
console.log('web_origins: +', ADD_WO);

const subset = (o, n) => (o || []).every(x => n.includes(x));
if (!subset(cur.callbacks, cb) || !subset(cur.allowed_logout_urls, lo) || !subset(cur.web_origins, wo)) {
  console.error('refusing: change is not purely additive'); process.exit(1);
}
if ((cur.callbacks || []).includes(ADD_CB) && (cur.web_origins || []).includes(ADD_WO) && ADD_LO.every(u => (cur.allowed_logout_urls || []).includes(u))) {
  console.log('>> already registered; nothing to do'); process.exit(0);
}
const r = await fetch(`https://${DOMAIN}/api/v2/clients/${CLIENT}`, {
  method: 'PATCH', headers: { ...H, 'content-type': 'application/json' },
  body: JSON.stringify({ callbacks: cb, allowed_logout_urls: lo, web_origins: wo })
});
const res = await r.json();
console.log('PATCH', r.status, res.statusCode ? ('ERROR ' + JSON.stringify(res)) : 'OK — registered additively');
process.exit(res.statusCode ? 1 : 0);
