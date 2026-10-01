import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { DecisionCache } from '../addon/scripts/decision-cache.js';

const saved = new Map();
const newCache = () => new DecisionCache('policy', pid => saved.get(pid), (pid,v) => saved.set(pid,v));
let cache = newCache();
assert.equal(cache.get('p1').decision, 'unknown');
cache.accept({pid:'p1', policy:'policy', decision:'allowed', revision:1});
assert.equal(newCache().get('p1').decision, 'allowed');
cache.accept({pid:'p1', policy:'policy', decision:'denied', revision:2});
cache.accept({pid:'p1', policy:'policy', decision:'allowed', revision:1});
assert.equal(newCache().get('p1').decision, 'denied');
assert.equal(new DecisionCache('other', p => saved.get(p), () => {}).get('p1').decision, 'unknown');
assert.throws(() => cache.accept({pid:'p1', policy:'wrong', decision:'allowed', revision:3}));
console.log('PASS: persistent authorization cache, revocation, ordering, policy changes');

async function fixture(properties = new Map(), endpoint = 'http://127.0.0.1:18080') {
  let loaded = false;
  const callbacks = {};
  const replies = [];
  const kicked = [];
  const players = [];
  const world = {
    getDynamicProperty: p => properties.get(p),
    setDynamicProperty: (p,v) => properties.set(p,v),
    getAllPlayers: () => players,
    afterEvents:{worldLoad:{subscribe: fn => callbacks.load = fn}}
  };
  const system = {runInterval: (fn,ticks) => {assert.equal(ticks,100); callbacks.interval=fn;}};
  const admin = {
    beforeEvents:{asyncPlayerJoin:{subscribe: fn => {assert.ok(loaded, 'subscribe only after world load'); callbacks.join=fn;}}},
    kickPlayer:(p,reason)=>kicked.push(p.persistentId),
    variables:{get:key=>key==='policy_hash'?'policy':endpoint},
    secrets:{get:()=>({secret:true})}
  };
  class HttpRequest {constructor(url){this.url=url;} setTimeout(value){assert.equal(value,3);}}
  class HttpHeader {constructor(name,value){this.name=name;this.value=value;}}
  const net = {HttpRequest,HttpHeader,HttpRequestMethod:{Post:'POST'},http:{request:async req=>{
    assert.ok(req.url.startsWith(endpoint + '/'));
    if (req.url.endsWith('/online') && JSON.parse(req.body).players.length===0) return {status:200,body:JSON.stringify({policy:'policy',players:[]})};
    const next = replies.shift();
    if (next instanceof Error) throw next;
    if (!next) throw new Error('offline');
    return {status:200,body:JSON.stringify(next)};
  }}};
  const context=vm.createContext({console,Map,JSON,Number,Error});
  const modules=new Map();
  for (const [name,exports] of [['@minecraft/server',{world,system}],['@minecraft/server-admin',admin],['@minecraft/server-net',net]]) {
    modules.set(name,new vm.SyntheticModule(Object.keys(exports),function(){for(const [k,v] of Object.entries(exports))this.setExport(k,v);},{context}));
  }
  const cacheModule=new vm.SourceTextModule(fs.readFileSync(fileURLToPath(new URL('../addon/scripts/decision-cache.js',import.meta.url)),'utf8'),{context});
  modules.set('./decision-cache.js',cacheModule);
  const main=new vm.SourceTextModule(fs.readFileSync(fileURLToPath(new URL('../addon/scripts/main.js',import.meta.url)),'utf8'),{context});
  await main.link(name=>modules.get(name));
  await main.evaluate();
  assert.equal(callbacks.join,undefined);
  loaded=true;
  callbacks.load();
  await Promise.resolve();
  function event(pid='p1') {
    return {persistentId:pid,name:'Player',isValid:()=>true,
      allowJoin(){this.allowed=true;},disallowJoin(reason){this.denied=reason;}};
  }
  return {callbacks,replies,event,players,kicked,properties};
}

const f=await fixture();
let e=f.event();
f.replies.push({pid:'p1',policy:'policy',decision:'unlinked',revision:0,message:'/mc link 0123'});
await f.callbacks.join(e);
assert.equal(e.denied,'/mc link 0123');
assert.equal(e.allowed,undefined);
e=f.event();
f.replies.push({pid:'p1',policy:'policy',decision:'allowed',revision:1});
await f.callbacks.join(e);
assert.equal(e.allowed,true);
const restarted=await fixture(f.properties);
e=restarted.event();
restarted.replies.push(new Error('Bot stopped'));
await restarted.callbacks.join(e);
assert.equal(e.allowed,true);
e=restarted.event('never-linked');
await restarted.callbacks.join(e);
assert.ok(e.denied);
f.players.push({persistentId:'p1',name:'Player',isValid:true});
f.replies.push({policy:'policy',players:[{pid:'p1',policy:'policy',decision:'denied',revision:2}]});
await f.callbacks.interval();
assert.deepEqual(f.kicked,['p1']);
e=f.event();
f.replies.push(new Error('offline after revocation'));
await f.callbacks.join(e);
assert.ok(e.denied);
assert.equal(e.allowed,undefined);
console.log('PASS: actual addon lifecycle, code display, admission, restart during outage, kick, revoked offline entry');

await fixture(new Map(), 'http://127.0.0.1:28080');
for (const endpoint of ['http://0.0.0.0:18080', 'http://example.invalid:18080',
                        'http://127.0.0.1:0', 'http://127.0.0.1:65536',
                        'http://127.0.0.1:18080/extra', 'http://127.0.0.1:18080?x=1']) {
  await assert.rejects(() => fixture(new Map(), endpoint), /Invalid Discord authorization configuration/);
}
console.log('PASS: configurable loopback API port; external hosts and invalid URLs rejected');
