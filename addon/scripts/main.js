import { system, world } from '@minecraft/server';
import { beforeEvents, kickPlayer, variables, secrets } from '@minecraft/server-admin';
import { http, HttpRequest, HttpRequestMethod, HttpHeader } from '@minecraft/server-net';
import { DecisionCache } from './decision-cache.js';

const PREFIX = 'mcbe-connect:';
const policy = variables.get('policy_hash');
const endpoint = variables.get('api_url');
function validEndpoint(value) {
  if (typeof value !== 'string') return false;
  const match = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/.exec(value);
  return !!match && Number(match[1]) <= 65535;
}
const cache = new DecisionCache(policy,
  pid => { const raw = world.getDynamicProperty(PREFIX + pid); return raw ? JSON.parse(raw) : null; },
  (pid, value) => world.setDynamicProperty(PREFIX + pid, JSON.stringify(value)));
let ready = false;
let checking = false;
let reportedOffline = false;

async function request(path, body) {
  const req = new HttpRequest(endpoint + path);
  req.method = HttpRequestMethod.Post;
  req.body = JSON.stringify(body);
  req.headers = [new HttpHeader('Content-Type', 'application/json'),
    new HttpHeader('Authorization', secrets.get('auth_header'))];
  req.setTimeout(3);
  const response = await http.request(req);
  if (response.status !== 200) throw new Error('Local authorization API unavailable');
  return JSON.parse(response.body);
}

async function onJoin(event) {
  if (!ready || !event.persistentId) {
    event.disallowJoin('参加制御を準備中です。しばらくして再接続してください。');
    return;
  }
  let result;
  try {
    result = await request('/v1/authorize', {pid: event.persistentId, name: event.name});
    if (result.pid !== event.persistentId) throw new Error('Identity mismatch');
    const accepted = cache.accept(result);
    if (accepted.decision === 'allowed' && result.decision === 'allowed') {
      if (event.isValid()) event.allowJoin();
      return;
    }
  } catch (_) {
    if (cache.get(event.persistentId).decision === 'allowed') {
      if (event.isValid()) event.allowJoin();
      return;
    }
    result = {message: '参加資格を確認できません。Discordまたは連携Botの復旧後に再接続してください。'};
  }
  if (event.isValid()) event.disallowJoin(result.message || 'サブスクロールまたはDiscord連携を確認してください。');
}

world.afterEvents.worldLoad.subscribe(() => {
  if (typeof policy !== 'string' || !policy || !validEndpoint(endpoint) || !secrets.get('auth_header')) {
    throw new Error('Invalid Discord authorization configuration');
  }
  beforeEvents.asyncPlayerJoin.subscribe(onJoin);
  ready = true;
  // The supervisor requires this marker before reporting service readiness.
  console.warn('[mcbe-connect:ready] version=1');
  request('/v1/online', {players: []}).then(response => {
    if (response.policy !== policy) throw new Error('Policy mismatch');
    console.warn('[mcbe-connect:api] reachable');
  }).catch(error => console.warn('[mcbe-connect:api] offline (' + error.name + '); using saved decisions'));
});

system.runInterval(async () => {
  if (!ready || checking) return;
  const players = world.getAllPlayers();
  if (!players.length) return;
  checking = true;
  try {
    const response = await request('/v1/online', {players: players.map(p => ({pid: p.persistentId, name: p.name}))});
    if (!Array.isArray(response.players) || response.policy !== policy) throw new Error('Invalid response');
    const statuses = new Map();
    for (const result of response.players) statuses.set(result.pid, cache.accept(result));
    for (const player of players) {
      const status = statuses.get(player.persistentId);
      if (status && status.decision !== 'allowed' && player.isValid) {
        kickPlayer(player, 'サブスクロールまたはDiscord連携が解除されたため退出しました。');
      }
    }
    if (reportedOffline) console.warn('[mcbe-connect] Local API recovered');
    reportedOffline = false;
  } catch (_) {
    if (!reportedOffline) console.warn('[mcbe-connect] Local API unavailable; retaining cached decisions');
    reportedOffline = true;
  } finally {
    checking = false;
  }
}, 100);
