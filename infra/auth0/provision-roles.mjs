#!/usr/bin/env node
// Create embry0's Auth0 roles on the tenant whose Management API credentials
// are in the file named by AUTH0_MGMT_ENV (RAV-1069). Idempotent: existing
// roles and assignments are detected and skipped. Default-deny model:
// embry0-operator = dashboard access, embry0-admin = env-secret reveals +
// agent/config mutations (admin implies operator in embry0's tier folding,
// no separate assignment needed).
//
// Usage:
//   AUTH0_MGMT_ENV=/path/to/mgmt.env node provision-roles.mjs
//   AUTH0_MGMT_ENV=/path/to/mgmt.env node provision-roles.mjs --assign <role> <email>
//
// Assignment happens only with --assign. There is no built-in member list.
// Never prints secrets. Users who have never logged into the tenant cannot be
// resolved by email — reported for manual dashboard assignment.
import fs from 'fs';

const MGMT_ENV = process.env.AUTH0_MGMT_ENV;
if (!MGMT_ENV) {
  console.error('AUTH0_MGMT_ENV is required (path to a file with AUTH0_MGMT_*)');
  process.exit(1);
}
const ROLES = [
  { name: 'embry0-operator', description: 'embry0 dashboard access' },
  { name: 'embry0-admin', description: 'embry0 admin: env-secret reveals, agent/config mutations' },
];

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
const H = { authorization: `Bearer ${tok.access_token}`, 'content-type': 'application/json' };
const api = (p, init) => fetch(`https://${DOMAIN}/api/v2${p}`, { headers: H, ...init });

async function ensureRole(def) {
  const found = await (await api(`/roles?name_filter=${encodeURIComponent(def.name)}`)).json();
  if (Array.isArray(found)) {
    const hit = found.find(r => r.name === def.name);
    if (hit) { console.log(`role ${def.name}: exists (${hit.id})`); return hit.id; }
  } else if (found.statusCode) {
    console.error(`role ${def.name}: LIST FAILED —`, found.message || found.error, '(needs read:roles)');
    return null;
  }
  const r = await api('/roles', { method: 'POST', body: JSON.stringify(def) });
  const res = await r.json();
  if (res.statusCode) { console.error(`role ${def.name}: CREATE FAILED —`, res.message || res.error, '(needs create:roles)'); return null; }
  console.log(`role ${def.name}: created (${res.id})`);
  return res.id;
}

async function userIdByEmail(email) {
  const r = await api(`/users-by-email?email=${encodeURIComponent(email.toLowerCase())}`);
  const res = await r.json();
  if (!Array.isArray(res)) { console.error(`user ${email}: LOOKUP FAILED —`, res.message || res.error, '(needs read:users)'); return null; }
  if (!res.length) { console.log(`user ${email}: NOT FOUND in tenant (never logged in?) — assign manually in dashboard later`); return null; }
  return res[0].user_id;
}

async function assign(roleId, roleName, email) {
  const uid = await userIdByEmail(email);
  if (!uid || !roleId) return false;
  const existing = await (await api(`/users/${encodeURIComponent(uid)}/roles`)).json();
  if (Array.isArray(existing) && existing.some(r => r.id === roleId)) {
    console.log(`assign ${roleName} -> ${email}: already assigned`); return true;
  }
  const r = await api(`/users/${encodeURIComponent(uid)}/roles`, { method: 'POST', body: JSON.stringify({ roles: [roleId] }) });
  if (r.status === 204) { console.log(`assign ${roleName} -> ${email}: OK`); return true; }
  const res = await r.json().catch(() => ({}));
  console.error(`assign ${roleName} -> ${email}: FAILED ${r.status}`, res.message || '', '(needs create:role_members)');
  return false;
}

const roleIds = {};
for (const def of ROLES) roleIds[def.name] = await ensureRole(def);

const missing = Object.entries(roleIds).filter(([, id]) => !id).map(([name]) => name);
if (missing.length) {
  console.error(`roles not ensured: ${missing.join(', ')}`);
  process.exit(1);
}

const extra = process.argv.indexOf('--assign');
if (extra === -1) {
  console.log('done — roles ensured; pass --assign <role> <email> to add a member');
  process.exit(0);
}
const [roleName, email] = process.argv.slice(extra + 1);
if (!roleName || !email) { console.error('usage: --assign <role> <email>'); process.exit(1); }
if (!roleIds[roleName]) { console.error(`unknown role ${roleName}`); process.exit(1); }
process.exit((await assign(roleIds[roleName], roleName, email)) ? 0 : 1);
