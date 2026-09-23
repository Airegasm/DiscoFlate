// Go Live can only offer groups the RUNTIME will actually play.
//
// A scene group resolves inside the loaded scene (plus the globals) — the
// server deliberately does not hunt other scenes, because two scenes reuse a
// name like "Intro" for completely different looks. The picker used to offer
// every scene's groups anyway, so choosing one from another scene saved fine,
// showed fine, and then played nothing at all. Nothing surfaced the mismatch:
// a missing group is a quiet no-op by design.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let P = 0; const F = [];
const ok = (c, label) => { c ? P++ : F.push(label); };

const html = fs.readFileSync(path.join(__dirname, 'web', 'index.html'), 'utf8');
const start = html.indexOf('function glGroupOptions(');
ok(start > 0, 'glGroupOptions is still where the test expects it');
const src = html.slice(start, html.indexOf('\n}', start) + 2);

const VERSUS = {
  name: 'DiscoFlate Versus',
  intro_groups: ['Intro', 'Intro Video'],
  groups: ['Intro', 'Intro Video', 'Main', 'Result', 'Round Video'],
  overlays: [{ id: 'a', group: 'Main' }, { id: 'b', group: 'Result' },
             { id: 'c', group: 'Intro' }, { id: 'd', group: 'Intro Video' },
             { id: 'e', group: 'Round Video' }],
};
const SOLO = {
  name: 'DiscoFlate Default',
  intro_groups: ['Intro'],
  groups: ['Main', 'Intro', 'Pause'],
  overlays: [{ id: 'z', group: 'Main' }, { id: 'y', group: 'Pause' },
             { id: 'x', group: 'Intro' }],
};

const box = {
  esc: s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
  scenesData: [SOLO, VERSUS],
  sceneGlobals: [{ id: 'g1', group: 'Watermark' }],
  chatSceneName: 'DiscoFlate Versus',
  stgStage: () => VERSUS,
};
vm.createContext(box);
vm.runInContext(src, box);

// the leading "— none —" is always there; it is not a group
const opts = h => [...h.matchAll(/<option[^>]*>([^<]*)<\/option>/g)]
  .map(m => m[1]).filter(t => t && !t.includes('none'));

// ---- editing the versus scene ---------------------------------------------
let h = box.glGroupOptions('', false);
ok(opts(h).includes('Main'), "this scene's own groups are offered");
ok(opts(h).includes('Result') && opts(h).includes('Round Video'),
   '…all of its normal ones');
ok(!opts(h).includes('Pause'),
   "another scene's group is NOT offered — picking it would play nothing");
ok(opts(h).includes('Watermark'),
   'global overlay groups ARE offered: those really do resolve anywhere');
ok(!opts(h).includes('Intro'),
   '"Then switch to" never offers an intro group — switching to another intro '
   + 'is never what you want');
ok(h.includes('DiscoFlate Versus') && !h.includes('DiscoFlate Default'),
   'the list is labelled with this scene, and names no other');

// ---- the intro stage picker ------------------------------------------------
h = box.glGroupOptions('', true);
ok(opts(h).includes('Intro') && opts(h).includes('Intro Video'),
   'the stage picker offers THIS scene’s flagged intro groups');
ok(!opts(h).includes('Main'), '…and only those');
ok(!h.includes('Global overlays'),
   '…with no globals: an intro group is a scene’s own pre-show');

// A scene whose intro groups are unflagged has NOTHING to offer a stage. That
// is the trap the versus scene was in, and why it now flags both.
box.stgStage = () => ({ ...VERSUS, intro_groups: [] });
ok(opts(box.glGroupOptions('', true)).length === 0,
   'unflagged intro groups leave the stage picker empty — flag them');
box.stgStage = () => VERSUS;

// ---- a value already saved that this scene cannot play ---------------------
h = box.glGroupOptions('Pause', false);
ok(opts(h).includes('Pause'), "a saved value is kept, not silently dropped");
ok(/won.t play/.test(h), "…and the picker SAYS it won't play");

// ---- global-overlay editing mode -------------------------------------------
// Neither an edited scene NOR a live one to fall back to.
box.stgStage = () => null;
box.chatSceneName = '';
h = box.glGroupOptions('', false);
ok(opts(h).includes('Watermark') && !opts(h).includes('Main'),
   'with no scene in hand it offers globals only, never a guess');

console.log(`${P} passed, ${F.length} failed`);
F.forEach(f => console.log('  FAIL:', f));
process.exit(F.length ? 1 : 0);
