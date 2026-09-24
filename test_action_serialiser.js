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

// and it must keep the values, not just survive
const [ov] = box.cleanActionRows([{ type:'overlay', overlay:'bvOutcome', mode:'timed',
                                    seconds:8, fade_in:0.3, fade_out:0.6 }]);
ok(ov.overlay === 'bvOutcome', 'the overlay id survives serialisation');
ok(ov.mode === 'timed' && ov.seconds === 8, '…with its mode and duration');
ok(ov.fade_in === 0.3 && ov.fade_out === 0.6, '…and its fades');
const [ut] = box.cleanActionRows([{ type:'update_overlay_text', overlay:'bvDuel', text:'+5%' }]);
ok(ut.overlay === 'bvDuel' && ut.text === '+5%', 'update_overlay_text keeps both fields');

console.log(`${P} passed, ${F.length} failed`);
F.forEach(f => console.log('  FAIL:', f));
process.exit(F.length ? 1 : 0);
