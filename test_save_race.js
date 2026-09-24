// An unsaved edit must survive a routine poll.
//
// The panel polls /api/state and re-hydrates fields from it. `free()` only
// skipped the field you were TYPING IN — so the moment you blurred (which is
// what fires the debounced save), a poll arriving in the next 400ms wrote the
// server's older value back over your edit, and the save then persisted the
// eaten value. Silent, and it looked exactly like "it doesn't save".

const fs = require('fs');
const path = require('path');

let P = 0; const F = [];
const ok = (c, label) => { c ? P++ : F.push(label); };

const html = fs.readFileSync(path.join(__dirname, 'web', 'index.html'), 'utf8');

// ---- the guard exists, and covers BOTH halves of the window ---------------
ok(/function savePending\(\)/.test(html), 'there is one test for "an edit is in flight"');
const sp = html.slice(html.indexOf('function savePending()'),
                      html.indexOf('async function doSave()'));
ok(/saveT/.test(sp),
   '…covering the DEBOUNCE: the 400ms before the POST even starts');
ok(/saveInFlight/.test(sp),
   '…and the POST itself, which is the longer half of the window');

// ---- doSave marks itself in flight, and always unmarks ---------------------
const ds = html.slice(html.indexOf('async function doSave()'),
                      html.indexOf('async function _doSave()'));
ok(/saveInFlight\+\+/.test(ds), 'doSave marks the flight');
ok(/finally\s*\{\s*saveInFlight--/.test(ds),
   '…and clears it in FINALLY — a save that throws must not wedge the panel '
   + 'into never hydrating again');

// ---- hydration honours it -------------------------------------------------
const std = html.slice(html.indexOf('function showToDom('),
                       html.indexOf('function showReload('));
ok(/const free = el =>/.test(std), 'showToDom still has its free() guard');
ok(/!savePending\(\)/.test(std),
   '…and it now refuses to write over a field whose edit has not landed yet');
ok(/force \|\|/.test(std),
   '…while an explicit reload (switching scene) still forces a rewrite, '
   + 'because that is the operator asking for it');

// the Go Live STAGES list is hydrated separately and needs the same guard —
// it is where the intro seconds live
const stages = std.slice(std.indexOf('glStages = Array.isArray') - 300,
                         std.indexOf('glStages = Array.isArray'));
ok(/savePending\(\)/.test(stages),
   'the intro stages are guarded too — the seconds live there, and they were '
   + 'being lost the same way');

// ---- the guard must not become a deadlock ---------------------------------
// A failed save retries every 3s forever, so a permanent `saveT` would freeze
// hydration permanently: one unreachable server and the whole panel silently
// goes stale, which is worse than the clobber the guard exists to prevent.
const sp2 = html.slice(html.indexOf('let savePendingSince'),
                       html.indexOf('async function doSave()'));
ok(/SAVE_PENDING_MS/.test(sp2), 'the pending state is bounded by a window');
ok(/Date\.now\(\) - savePendingSince\) < SAVE_PENDING_MS/.test(sp2),
   '…measured from when it STARTED, so a retry loop cannot extend it forever');
ok(/savePendingSince = 0/.test(sp2),
   '…and reset once nothing is in flight, so the window starts fresh each time');

console.log(`${P} passed, ${F.length} failed`);
F.forEach(f => console.log('  FAIL:', f));
process.exit(F.length ? 1 : 0);
