// Every action row type must SERIALISE. If one throws, gatherConfig() dies
// before it builds a payload — so the save vanishes with no error, the badge
// retries forever, and nothing in the app says why.
//
// That is exactly what happened: the overlay and timer clauses call s() to
// coerce a field to a string, and s was only ever defined inside the gist
// helper, never here. Any block containing an `overlay`, `update_overlay_text`,
// `overlay_kill`, `start_timer` or `stop_timer` row was unsaveable — silently,
// and for a long time, because nothing had put one in a saved block yet.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let P = 0; const F = [];
const ok = (c, label) => { c ? P++ : F.push(label); };

const html = fs.readFileSync(path.join(__dirname, 'web', 'index.html'), 'utf8');
const src = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

const el = () => new Proxy({ style:{}, dataset:{}, children:[], options:[], value:'',
  checked:false, textContent:'', innerHTML:'', disabled:false, title:'',
  classList:{add(){},remove(){},toggle(){},contains:()=>false},
  appendChild(){}, remove(){}, addEventListener(){}, getAttribute:()=>null,
  setAttribute(){}, insertAdjacentHTML(){}, focus(){}, blur(){}, click(){},
  closest:()=>null, querySelector:()=>el(), querySelectorAll:()=>[] },
  { get(t,k){ return (k in t) ? t[k] : () => el(); }, set(t,k,v){ t[k]=v; return true; } });
const doc = { activeElement:null, body:el(), cookie:'', querySelector:()=>el(),
  querySelectorAll:()=>[], getElementById:()=>el(), createElement:()=>el(), addEventListener(){} };
const box = { console, document: doc, location:{href:'http://x/',search:'',hash:''},
  navigator:{clipboard:{}}, localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  setTimeout:()=>0, clearTimeout(){}, setInterval:()=>0, clearInterval(){},
  addEventListener(){}, removeEventListener(){}, fetch:()=>new Promise(()=>{}),
  requestAnimationFrame:()=>0, alert(){}, confirm:()=>false, prompt:()=>'',
  matchMedia:()=>({matches:false,addEventListener(){}}), URL:{createObjectURL:()=>''},
  Blob:function(){}, FormData:function(){},
  EventSource:function(){ return {addEventListener(){},close(){}}; } };
box.window = box; box.self = box; box.globalThis = box;
vm.createContext(box);
vm.runInContext(src, box);

// Every type the row editor offers. A new one that throws here is a save that
// disappears, so this list is the guard.
const TYPES = vm.runInContext(
  "(function(){const m=/\\['message','notify'[^\\]]*\\]/.exec(" +
  "cleanActionRows.toString()+'');return null;})()", box) || [
  'message','notify','broadcast','command','fire','roll','chance','minigame','var','if',
  'repeat','random','group','label','goto','goto_if','cancel','capacity','wait','poll',
  'competition','bonus_round','award','command_gate','overlay','overlay_kill','scene_group',
  'scene_group_kill','camera','snapshot','update_overlay_text','start_timer','stop_timer',
  'stop_devices','end_session','mp_action','mp_tell','mp_spin','mp_roll','mp_choice',
  'mp_duel','mp_cards','mp_simon','mp_ttt'];

const bad = [];
for (const t of TYPES) {
  try { box.cleanActionRows([{ type: t, overlay: 'x', text: 'y', seconds: 5, group: 'g' }]); }
  catch (e) { bad.push(`${t} (${e.message})`); }
}
ok(bad.length === 0, 'every action row type serialises without throwing: ' + bad.join(', '));

// the five that were actually broken, checked by name so the regression is named
for (const t of ['overlay','update_overlay_text','overlay_kill','start_timer','stop_timer']) {
  let threw = null;
  try { box.cleanActionRows([{ type: t, overlay: 'bvOutcome', text: 'hi', seconds: 8 }]); }
  catch (e) { threw = e.message; }
  ok(!threw, `${t} serialises — it used to throw "${threw}" and take the whole save with it`);
}

// ---- the ONE field that defines a row must survive --------------------------
// A row whose defining field is dropped still exists, still runs, and does
// NOTHING. That is what stopped the minigames: !agsimon kept its minigame row,
// the row pointed at no game, and every save re-wrote it that way.
for (const [t, f, v, want] of [
    ['minigame', 'minigame', 'mg:abc123', 'mg:abc123'],
    ['camera',   'op',       'freeze',    'freeze'],
    ['notify',   'message',  'hello',     'hello'],
    ['snapshot', 'caption',  'a caption', 'a caption'],
    ['award',    'amount',   12,          12]]) {
  const [o] = box.cleanActionRows([{ type: t, [f]: v }]);
  ok(o[f] === want,
     `a ${t} row keeps its ${f} — without it the row runs nothing, silently`);
}
// a minigame row written by an older build used `game`; both must be accepted
{
  const [o] = box.cleanActionRows([{ type: 'minigame', game: 'mg:old' }]);
  ok(o.minigame === 'mg:old', 'an older `game` field is migrated, not dropped');
}

// and it must keep the values, not just survive
const [ov] = box.cleanActionRows([{ type:'overlay', overlay:'bvOutcome', mode:'timed',
                                    seconds:8, fade_in:0.3, fade_out:0.6 }]);
ok(ov.overlay === 'bvOutcome', 'the overlay id survives serialisation');
ok(ov.mode === 'timed' && ov.seconds === 8, '…with its mode and duration');
ok(ov.fade_in === 0.3 && ov.fade_out === 0.6, '…and its fades');
const [ut] = box.cleanActionRows([{ type:'update_overlay_text', overlay:'bvDuel', text:'+5%' }]);
ok(ut.overlay === 'bvDuel' && ut.text === '+5%', 'update_overlay_text keeps both fields');

// ---- SELF-MAINTAINING: every field the editor writes must survive ----------
// The two bugs above were both "the editor writes a field, the serialiser
// doesn't know about it". Rather than list the types by hand — which is how
// they were missed — read the editor itself: for each `typ==='X'` block,
// collect the T('field') writes, then prove each one round-trips.
//
// Add a row type with a new field and forget the serialiser, and this fails.
{
  const map = {};
  const re = /if\(typ==='([a-z_]+)'\)\{([\s\S]{0,2600}?)\n  \}/g;
  let m;
  while ((m = re.exec(html))) {
    const fields = [...new Set([...m[2].matchAll(/T\('([a-z_]+)'\)/g)].map(x => x[1]))];
    if (fields.length) map[m[1]] = [...new Set((map[m[1]] || []).concat(fields))];
  }
  ok(Object.keys(map).length > 10,
     'the editor-field map is readable: ' + Object.keys(map).length + ' types');

  // values chosen to survive clamping, so a clamp is never mistaken for a drop
  const val = { announce_odds:false, dice:2, sides:6, luck:5, seconds:60, length:4,
    grow:2, block_during:false, charges:3, amount:7, iterations:3, max_iterations:9,
    scope:'user', operation:'set', mode:'fixed', style:'embed', as:'owner',
    fire_mode:'add', award_type:'command', capacity_op:'add' };
  const skip = new Set(['freeze','deadline','label','target','default']);  // stored elsewhere
  const dropped = [];
  for (const [t, fields] of Object.entries(map)) {
    const row = { type: t };
    fields.forEach(f => { row[f] = (f in val) ? val[f] : 'X_' + f; });
    let out;
    try { [out] = box.cleanActionRows([row]); }
    catch (e) { dropped.push(`${t}: THREW ${e.message}`); continue; }
    for (const f of fields) {
      if (skip.has(f)) continue;
      const v = out[f];
      if (v === undefined || v === null || v === '') dropped.push(`${t}.${f}`);
    }
  }
  ok(dropped.length === 0,
     'every field the row editor writes survives serialisation — a dropped one '
     + 'is a row that runs nothing, silently: ' + dropped.join(', '));
}

console.log(`${P} passed, ${F.length} failed`);
F.forEach(f => console.log('  FAIL:', f));
process.exit(F.length ? 1 : 0);
