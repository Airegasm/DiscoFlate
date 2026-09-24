// Headless proof of the Multiplayer PANEL.
//
// `node --check` only proves the file parses — it happily accepts markup
// spliced into the wrong function and every undefined runtime name. That is
// exactly how adding an overlay stayed broken for four releases. So this pulls
// the real multiplayer block out of web/index.html, evaluates it against a
// stub DOM, and drives it with the payloads /api/mp/status actually returns.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let P = 0; const F = [];
const ok = (c, label) => { c ? P++ : F.push(label); };

const html = fs.readFileSync(path.join(__dirname, 'web', 'index.html'), 'utf8');
const start = html.indexOf('// ---- multiplayer (the rail) ---');
const end = html.indexOf('async function saveAutomation()');
ok(start > 0 && end > start, 'the multiplayer block is still where the test expects it');
const src = html.slice(start, end);

// Every id the block touches must exist in the markup, or it is writing into
// nothing — the failure mode a syntax check cannot see.
// Any '#id' literal, not just $('#id') — ids reach the DOM through helpers
// (set(), cmdName()) too, and those are exactly the ones a typo hides in.
// Only COMPLETE arguments — a '#mp' followed by `+` is half of an id built at
// runtime ($('#mp'+which+'Guild')), and its finished forms are covered by the
// set() calls that hydrate them.
const ids = [...new Set([...src.matchAll(/'#([A-Za-z][A-Za-z0-9_]*)'\s*[,)]/g)].map(m => m[1]))];
const missing = ids.filter(id => !html.includes(`id="${id}"`));
ok(missing.length === 0, 'every id the panel writes to exists in the markup: ' + missing.join(', '));

// ---- stub DOM --------------------------------------------------------------
function El(id) {
  // A real <input>.value is ALWAYS a string — a stub that keeps numbers would
  // let a comparison pass here and fail in the browser.
  const el = { id, textContent: '', innerHTML: '', checked: false,
               disabled: false, title: '', className: '', style: {},
               options: [], _t: null, _v: '' };
  Object.defineProperty(el, 'value', {
    get() { return this._v; },
    set(v) { this._v = v == null ? '' : String(v); },
    enumerable: true,
  });
  return el;
}
const els = {};
ids.forEach(id => { els[id] = El(id); });
const $ = sel => els[String(sel).replace(/^#/, '')] || null;

let saved = null, posted = [], shown = [], aSaves = 0;
const sandbox = {
  $, console,
  esc: s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  guildsCache: [{ id: 'g1', name: 'The Den', channels: [{ id: '900', name: 'bot-net' },
                                                        { id: '901', name: 'gameshow' }] }],
  guildsLoaded: true,
  configRev: 3,
  lastState: {},
  saveCfg: async patch => { saved = patch; return {}; },
  api: async (p, b) => { posted.push([p, b]); return { ok: true }; },
  applyState: () => {},
  toastMsg: m => shown.push(m),
  loadGuilds: async () => {},
  scheduleSave: () => { aSaves++; },
  prompt: () => 'not right now',
  confirm: () => true,
  setTimeout, clearTimeout,
  // The modals really build DOM, so the stub has to let them. Everything they
  // append is kept, which is what lets the tests read what the invite actually
  // showed rather than trusting that it rendered.
  modals: [],
  document: { activeElement: null,
              querySelector: () => null,
              querySelectorAll: sel =>
                (sel === '.mpModal' ? sandbox.modals.filter(m => !m.removed) : []),
              createElement: () => {
                const node = { className: '', innerHTML: '', style: { cssText: '' },
                               children: [], remove() { node.removed = true; },
                               querySelector: () => ({ onclick: null, oninput: null,
                                                       value: '', checked: false,
                                                       textContent: '', innerHTML: '' }),
                               querySelectorAll: () => [] };
                return node;
              },
              body: { appendChild(n) { sandbox.modals.push(n); return n; } } },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(src, sandbox);
  ok(true, 'the panel block evaluates');
} catch (e) {
  ok(false, 'the panel block evaluates: ' + e.message);
  report();
}

const CFG = {
  bot_network: { guild_id: 'g1', channel_id: '900' },
  broadcast: { guild_id: 'g1', channel_id: '901' },
  peer_bot_name: 'Dave-bot',
  role_pref: 'host',
};

// ---- hydrating from /api/state --------------------------------------------
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false });
ok(els.mpNetChan.value === '900' && els.mpCastChan.value === '901',
   'both channel pickers select the saved channel');
ok(els.mpNetChan.innerHTML.includes('bot-net'), '…from the real server list');
ok(els.modeMulti.checked === true && els.modeMulti.disabled === false,
   'the header toggle follows the mode and is free while the session is down');

// ---- the SEAT: picked before going live, frozen after ----------------------
// Not negotiated. Two installs quietly agreeing a role between themselves is
// how you start a match in a seat you didn't mean to be in.
ok(els.seatTgl.style.display === '' && els.mpSeat.value === 'host',
   'the seat picker is offered in multiplayer, and shows the one you chose');
ok(els.mpSeat.disabled === false, '…and is free while the session is down');

sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: true });
ok(els.modeMulti.disabled === true, 'the mode toggle is LOCKED at go-live');
ok(/live/.test(els.modeTgl.title), '…and its tooltip says why');
ok(els.mpSeat.disabled === true,
   'the SEAT freezes with it — both decide what the other install is agreeing '
   + 'to, and neither may move under a running match');
ok(/host for this session/.test(els.seatTgl.title), '…and it says which seat you are in');

sandbox.mpApply({ mode: 'solo', multiplayer: CFG, listener_enabled: false });
ok(els.seatTgl.style.display === 'none', 'there is no seat to pick in solo');
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false });

// an input the operator is mid-edit is never overwritten underneath them
els.mpNetChan.value = 'half-typed';
sandbox.document.activeElement = els.mpNetChan;
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false });
ok(els.mpNetChan.value === 'half-typed', 'a field being typed in is not clobbered by a poll');
sandbox.document.activeElement = null;

// ---- the three panel states ------------------------------------------------
sandbox.mpRender({ mode: 'solo', state: 'idle', preflight: [] });
ok(els.mpPeerState.innerHTML.includes('◌ No peer seen yet'), '◌ with multiplayer off');

sandbox.mpRender({ mode: 'multi', state: 'advertised', peer_name: 'Dave-bot', preflight: [
  { ok: false, check: 'broadcast', why: 'missing in broadcast: Embed Links' }] });
ok(els.mpPeerState.innerHTML.includes('⚠') &&
   els.mpPeerState.innerHTML.includes('Embed Links'), '⚠ names the failing check');
ok(els.mpPreflight.innerHTML.includes('⚠'), 'the preflight list renders the fault');
ok(els.mpInviteBtn.disabled === true, 'you cannot invite while a preflight fails');

sandbox.mpRender({ mode: 'multi', state: 'advertised', peer_online: true,
                   peer_name: 'Dave-bot', peer_version: '3.90.2',
                   preflight: [{ ok: true, check: 'bot_network', why: '' }] });
ok(els.mpPeerState.innerHTML.includes('✓ Peer online: Dave-bot · v3.90.2'), '✓ names peer and version');
ok(els.mpInviteBtn.disabled === false, 'a clean preflight enables the invite');

// ---- an invite must show its cost BEFORE anyone agrees ---------------------
// The TERMS live in the Accept/Decline modal now (tested further down), so the
// card only points at it — repeating them would be two places to keep honest.
sandbox.modals.length = 0;
sandbox.mpRender({ mode: 'multi', state: 'invited', peer_online: true, peer_name: 'Curtis-bot',
                   invite: { host: 'Curtis', cost: 'race to 75%', paced: true }, preflight: [] });
ok(els.mpMatch.innerHTML.includes('has invited you'), 'the card says an invite is waiting');
ok(sandbox.modals.length === 1, '…and the modal carrying the terms opened');
ok(els.mpInviteBtn.style.display === 'none', 'you cannot invite while holding one');
sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [] });

// ---- the race, mid-flight --------------------------------------------------
const LIVE = { mode: 'multi', state: 'match', peer_online: true, peer_name: 'Dave-bot',
               me: '100', peer: '200', player: 'Curtis', peer_player: 'Dave',
               role: 'host', capacity: 63.4, peer_capacity: 41,
               preflight: [], notes: ['fired 10% ✓'],
               race: { game: 'Race to N%', targets: { '100': 150, '200': 75 } } };
sandbox.mpRender(LIVE);
ok(els.mpMatch.innerHTML.includes('Curtis') && els.mpMatch.innerHTML.includes('Dave'),
   'both racers are named by PLAYER, not by bot');
ok(els.mpMatch.innerHTML.includes('63% of 150%') && els.mpMatch.innerHTML.includes('41% of 75%'),
   'each meter reads against that racer’s own line');
ok(/width:42%/.test(els.mpMatch.innerHTML) && /width:55%/.test(els.mpMatch.innerHTML),
   'the bars are scaled to each line, so the compensated racer is not made to look behind');
ok(els.mpMatch.innerHTML.includes('watch it in Discord'), 'the panel says where the match actually is');
ok(els.mpAbortBtn.style.display === '' && els.mpInviteBtn.style.display === 'none',
   'abort is the only thing offered mid-match');
ok(els.mpNotes.innerHTML.includes('fired 10% ✓'), 'the link log renders');

sandbox.mpRender({ ...LIVE, paced: false });
ok(els.mpMatch.innerHTML.includes('not pace-compensated'),
   'an unpaced race is flagged while it runs, not only at the invite');

sandbox.mpRender({ ...LIVE, race: { ...LIVE.race, done: true, why: '2.10s vs 3.40s' } });
ok(els.mpMatch.innerHTML.includes('finished') && els.mpMatch.innerHTML.includes('2.10s vs 3.40s'),
   'the verdict and its margin stay on screen');

// the flag is the host’s, and only once both have armed
sandbox.mpRender({ mode: 'multi', state: 'ready', role: 'host', peer_online: true, preflight: [] });
ok(els.mpStartBtn.style.display === '', 'the host can drop the flag when both are armed');
sandbox.mpRender({ mode: 'multi', state: 'ready', role: 'guest', peer_online: true, preflight: [] });
ok(els.mpStartBtn.style.display === 'none', 'the guest never starts the race');
sandbox.mpRender({ mode: 'multi', state: 'linked', role: 'host', peer_online: true, preflight: [] });
ok(els.mpStartBtn.style.display === 'none', '…and not before the peer has armed');

// ---- resume is offered, never taken ----------------------------------------
sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [],
                   resume: { sid: 'm7k2', peer_name: 'Dave-bot', round: 4 } });
ok(els.mpResume.style.display === '' &&
   els.mpResumeText.textContent === 'Rejoin match with Dave-bot, round 4?',
   'an unfinished match asks before rejoining');
sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [], resume: {} });
ok(els.mpResume.style.display === 'none', '…and hides when there is nothing to rejoin');

// ---- the invite MODAL: everything they're agreeing to ----------------------
const INV = {
  mode: 'multi', state: 'invited', peer_online: true, preflight: [], calibration: 90,
  peer_name: 'Curtis-bot', peer_player: 'Curtis',
  invite: {
    host: 'Curtis', bot: 'Curtis-bot', game: 'Race to N%', input: 'audience',
    scene: 'Roulette Night', cost: 'Race to N% · race to 75%', cal: 60,
    cap: 300, paced: true,
    venue: { cast: { channel: 'gameshow', guild: 'The Den', kind: 'voice' },
             net: { channel: 'bot-net', guild: 'Backstage', kind: 'text' } },
  },
};
// idle first, so the previous modal closes and this one really re-opens
const openInvite = inv => {
  sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [] });
  sandbox.modals.length = 0;
  sandbox.mpRender(inv);
  return sandbox.modals[0].innerHTML;
};
let card = openInvite(INV);
ok(/Curtis wants to play/.test(card), 'the popup names the PLAYER asking, not their bot alone');
ok(/Curtis-bot/.test(card), '…and names the bot too');
ok(/Race to N%/.test(card) && /Gameshow/.test(card), 'the game and who plays it');
ok(/#gameshow in The Den/.test(card) && /🔊/.test(card),
   'the venue as something a person can settle — a name and a server, not an id');
ok(/#bot-net in Backstage/.test(card), '…and the protocol channel too');
ok(/you lose at <b>300%<\/b>/.test(card),
   'the CAP as a number — a yes/no on "past 100%" never tells the guest how far '
   + 'this can go, and the cap is what defines their lose condition');
// Both pump speeds, side by side, and YOURS is editable: every target is paced
// from it, and this is the last moment a stale figure can be corrected.
ok(/Their pump/.test(card) && /value="60" disabled/.test(card),
   'the invite shows the host\u2019s pump speed, which is why the lines differ');
ok(/id="mpiMyCal"/.test(card), '\u2026and your own, as a field you can fix');
ok(!/id="mpiTakeSplit"/.test(card), 'no split box when it was not offered');
card = openInvite({ ...INV, invite: { ...INV.invite, split: true } });
ok(/id="mpiTakeSplit"/.test(card), 'offered → the guest gets the box');
ok(/Split the difference/.test(card) && /go back to their own/.test(card),
   '\u2026and it says the change lasts only for the match');

const respJs = html.slice(html.indexOf('function mpInviteeModal('),
                          html.indexOf('async function setSeat('));
ok(/cal:/.test(respJs) && /split:/.test(respJs),
   'accepting sends the corrected pump speed and the split answer');
ok(/video:\s*true/.test(respJs),
   '\u2026and always the camera: a match requires it, so there is nothing to ask');
card = openInvite(INV);
ok(/race to 75%/.test(card), 'the compensated cost estimate');
ok(!/per fire/.test(card),
   'no ceilings are quoted: there is no ceiling to set any more, and a limit '
   + 'nobody can see would be worse than none');
// Camera is no longer a QUESTION — it is a requirement of a match, and the
// server refuses an accept without one. The card states it rather than asking.
ok(!/id="mpVideo"/.test(card), 'the camera is not offered as a box to untick');
ok(/on camera/.test(card) && /requires it/.test(card),
   '…the invite says accepting puts you on camera, and that both players must');
ok(/Accept/.test(card) && /Decline/.test(card) && /Block/.test(card),
   'three answers: accept, decline, block');

card = openInvite({ ...INV, invite: { ...INV.invite, cap: 0, paced: false,
                                      why: 'uncalibrated: 200' } });
ok(/you lose at <b>100%<\/b>/.test(card),
   'a cap that did not travel falls back to 100, never to blank');
ok(/unpaced/.test(card), 'an unpaced race is still flagged');
card = openInvite({ ...INV, invite: { ...INV.invite, venue: {} } });
ok(/not set/.test(card),
   'a venue that did not travel says so rather than rendering an empty line');
card = openInvite(INV);

// the ready check is the host's, and only once linked
sandbox.mpRender({ mode: 'multi', state: 'linked', role: 'host', peer_online: true, preflight: [] });
ok(els.mpReadyBtn.style.display === '', 'the host can post the ready check once linked');
sandbox.mpRender({ mode: 'multi', state: 'linked', role: 'guest', peer_online: true, preflight: [] });
ok(els.mpReadyBtn.style.display === 'none', 'the guest never posts it');
sandbox.mpRender({ mode: 'multi', state: 'advertised', role: '', peer_online: true, preflight: [] });
ok(els.mpReadyBtn.style.display === 'none', '…and not before there is a match to be ready for');

// ---- blocked list ----------------------------------------------------------
sandbox.renderMpBlocked([{ bot_id: '600', bot_name: 'Dave-bot', player: 'Dave' }]);
ok(/Dave/.test(els.mpBlockList.innerHTML) && /Unblock/.test(els.mpBlockList.innerHTML),
   'a blocked bot is listed by player, with a way back');
sandbox.renderMpBlocked([]);
ok(/Nobody is blocked/.test(els.mpBlockList.innerHTML), 'an empty list says so plainly');

// ---- there is no built-in game to configure --------------------------------
// "Race to N%" was an EXAMPLE, never a feature. A game is its Rounds, so there
// is nothing here to set: no target, no game picker, no command names.
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, prefix: '!', listener_enabled: false });
ok(!html.includes('id="mpTarget"') && !html.includes('id="mpPush"')
   && !html.includes('id="mpCmdPump"'),
   'the fallback game\u2019s controls are gone from the markup');
const mpJs = html.slice(html.indexOf('async function mpSave()'),
                        html.indexOf('async function setMode('));
ok(!/race:/.test(mpJs) && !/commands:/.test(mpJs),
   '\u2026and the panel writes neither race terms nor command names');
const offJs = html.slice(html.indexOf('function mpInviteModal()'),
                         html.indexOf('function mpInviteeModal('));
// The invite carries exactly ONE thing the panel decides — whether the host is
// OFFERING to split calibration. Everything else (game, Versus/Gameshow, the
// finish line) is read on the server from the scene and its Rounds, so the two
// cannot disagree about what was agreed to.
ok(/api\('\/api\/mp\/offer', \{split: [^}]+\}\)/.test(offJs),
   'an invite passes only the split OFFER: the scene names the game, carries Versus/Gameshow, '
   + 'and the finish line is the top of the last Round — all read on the server, '
   + 'so they cannot disagree');

// ---- one save payload ------------------------------------------------------
// The multiplayer editors reuse actionRow(), which calls scheduleSave() on
// every field. If gatherConfig() doesn't carry mp_actions, a row edit saves
// SOLO config, loses the edit, and bumps the rev underneath the multiplayer
// save already in flight — which is exactly "config changed elsewhere".
const gStart2 = html.indexOf('function gatherConfig(){');
const gSrc = html.slice(gStart2, html.indexOf('\nfunction ', gStart2 + 10));
ok(/mpPayload\(\)/.test(gSrc),
   'gatherConfig carries the multiplayer keys, so there is only ONE writer');
ok(!/saveCfg\(\{mp_actions/.test(html) && !/saveCfg\(\{mp_rounds/.test(html),
   'no second writer races it');

const pStart = html.indexOf('function mpPayload(){');
const pbox = { console, mpLoaded: false, mpCfg: null,
               cleanMpActions: () => [{ name: 'A' }],
               cleanMpRounds: () => [{ name: 'R' }],
               cleanActionRows: rows => rows || [],
               mpEnd: { max_capacity: 9999, actions: [{ type: 'message' }] },
               mpSudden: { actions: [{ type: 'mp_action' }] } };
pbox.window = pbox;
vm.createContext(pbox);
vm.runInContext(html.slice(pStart, html.indexOf('async function mpSave()', pStart)), pbox);
ok(Object.keys(pbox.mpPayload()).length === 0,
   'before the panel is hydrated it contributes NOTHING — a save from a solo '
   + 'tab must not write empty lists over real ones');
vm.runInContext('mpLoaded = true; mpCfg = {peer_bot_name:"D", blocked:[{bot_id:"9"}]};', pbox);
const pay = pbox.mpPayload();
ok(pay.mp_actions.length === 1 && pay.mp_rounds.length === 1,
   'once hydrated it carries actions and rounds');
ok(pay.multiplayer.peer_bot_name === 'D', '…and the multiplayer block');
// The End Condition is always last and always sent — it is how a match ends.
ok(pay.mp_end && pay.mp_end.actions.length === 1, '…and the End Condition');
ok(pay.mp_sudden && Array.isArray(pay.mp_sudden.actions),
   '…and Sudden Death, which is pinned beside it');
ok(pay.mp_end.max_capacity === 999,
   '…with the lose-at capacity clamped to 999, so a typo cannot set a target '
   + 'nobody could ever reach and a match that could never end');
ok(!('blocked' in pay.multiplayer),
   'but never `blocked` — that is the server’s, and can change without the '
   + 'panel, so the deep-merge must keep theirs');

// ---- the mode toggle must not race its own autosave ------------------------
// A checkbox raises BOTH `input` and `change`, and both are wired to the
// generic autosave. Without an exclusion the Multi toggle schedules a full
// config save that races its own POST and 409s it, flipping the switch back.
const noAuto = html.slice(html.indexOf('const NO_AUTOSAVE_IDS'), html.indexOf('function maybeSave'));
ok(/'modeMulti'/.test(noAuto),
   'the Multi toggle is excluded from the generic autosave, like mock and listener');
const smSrc = html.slice(html.indexOf('async function setMode(on){'),
                         html.indexOf('function mpApply(st){'));
const smCode = smSrc.split('\n').filter(l => !/^\s*(\/\/|\*)/.test(l)).join('\n');
ok(!/config_rev/.test(smCode),
   'setMode sends no config_rev — a one-key patch has nothing to clobber, so '
   + 'guarding it only creates a way for the toggle to fail');
ok(/flushSave/.test(smSrc), '…and it lets anything already pending land first');

// ---- what multiplayer strips out of the shared tabs ------------------------
const chatCard = html.slice(html.indexOf('<div class="card span" data-tab="chat">'),
                            html.indexOf('<!-- Triggers'));
ok(/<div class="chatside" data-mode="solo">/.test(chatCard),
   'the operator Controls are solo-only — a match drives itself');
ok(/data-mode="solo"[^>]*>\s*<input type="checkbox" id="chatIso"/.test(chatCard),
   'Isolate is solo-only: a match has ONE channel, so there is nothing to narrow');
ok(/data-mode="solo">Channel <select id="chatChan"/.test(chatCard),
   'the channel PICKER is solo-only');
ok(/data-mode="multi">Channel[\s\S]{0,140}id="chatChanFixed"/.test(chatCard),
   '…replaced in multiplayer by a label of the channel set on the Discord tab');
ok(/id="cooldownExemptNames"/.test(chatCard),
   'but "Your Discord name" stays in BOTH — it is the owner identity that '
   + 'scopes the Ready buttons');

const disc = [...html.matchAll(/data-tab="discord"(?: data-mode="(\w+)")?/g)].map(m => m[1]);
ok(disc.includes('solo') && disc.includes('multi'),
   'the Discord tab swaps the solo listen list for the two match channels');
const mdisc = html.slice(html.indexOf('data-tab="discord" data-mode="multi"'),
                         html.indexOf('<!-- Triggers'));
ok(/id="mpNetChan"/.test(mdisc) && /id="mpCastChan"/.test(mdisc),
   '…and that is where bot_network and broadcast are picked now');
ok(/where the game listens and plays/.test(mdisc)
   && /where the two bots talk/.test(mdisc),
   '…described in one line each: broadcast is the game, bot_network is the bots');
const mptab = html.slice(html.indexOf('<div class="card span" data-tab="multi">'),
                         html.indexOf('<!-- devices -->'));
ok(!/id="mpNetChan"/.test(mptab),
   '…so the Multiplayer tab no longer carries them twice');
ok(!/Fallback game/.test(mptab) && !/Who referees/.test(mptab),
   'and the fallback game and the referee picker are gone entirely');

// ---- `Who` says which MACHINE, and says so --------------------------------
const whoAt = html.indexOf('const WHO_GROUPS = [');
ok(whoAt > 0, 'the Who options are grouped');
const whoSrc = html.slice(whoAt, html.indexOf('let fields=cond', whoAt));
ok(!/nobody \(runs here\)/.test(whoSrc),
   'the blank option no longer says two opposite things at once');
ok(/just this machine/.test(whoSrc), '…it says the row stays here');
ok(/which machine it runs on/.test(whoSrc),
   'and the row spells out what the control IS, beside it');
ok(/Which MACHINE this row runs on/.test(whoSrc),
   '…with the long version in the tooltip');
ok(/the racer the spin picked/.test(whoSrc),
   'the targets are described by what they mean, not by their keyword');
// the duplicates are gone: only the host runs a block, so `host` was always
// this machine and `guest` always the other
['host', 'guest', 'me'].forEach(v =>
  ok(!new RegExp(`\\['${v}',`).test(whoSrc),
     `'${v}' is no longer offered — it duplicated another option`));
ok(/optgroup/.test(whoSrc),
   'and what is left is grouped: what the round decided, by position, always');

// it only appears where it can mean something
const wrAt = html.indexOf('const WHO_ROWS = [');
const wr = html.slice(wrAt, html.indexOf(']', wrAt));
['fire', 'camera', 'overlay'].forEach(t =>
  ok(wr.includes(`'${t}'`), `Who appears on a ${t} row — it touches a pump or a picture`));
['mp_action', 'wait', 'message', 'scene_group'].forEach(t =>
  ok(!wr.includes(`'${t}'`), `…and not on a ${t} row, where there is nothing to aim`));

// the reference prose moved out of the editor tab
const trigMulti = [...html.matchAll(/data-tab="triggers" data-mode="multi">\s*<details class="help"><summary><h2>([^<]*)/g)]
  .map(m => m[1]);
ok(trigMulti.length === 2 && /Actions/.test(trigMulti[0]) && /Rounds/.test(trigMulti[1]),
   'Triggers in multiplayer is two EDITORS and nothing else');
const helpMulti = html.slice(html.indexOf('data-tab="help" data-mode="multi"'),
                             html.indexOf('<div class="card span" data-tab="help" data-mode="solo"'));
ok(/The rows multiplayer adds/.test(helpMulti),
   '…the row reference is prose, so it lives in Help');
ok(/Who does a row act on\?/.test(helpMulti), '…and so does the explanation of Who');

// ---- a guest in a match is locked to Chat ----------------------------------
// It takes instructions: its commands are off and every editor would be editing
// something that isn't driving anything.
const lkAt = html.indexOf('function applyMpLock(){');
const lbox = { console, esc: sandbox.esc, lastState: {}, shown: [] };
lbox.tabs = [{ getAttribute: () => 'chat', style: {} },
             { getAttribute: () => 'triggers', style: {} },
             { getAttribute: () => 'stages', style: {} }];
lbox.concede = { style: { display: 'none' } };
lbox.note = { style: { display: 'none' }, innerHTML: '' };
lbox.document = {
  querySelectorAll: sel => (sel === '.tab-btn' ? lbox.tabs : []),
  querySelector: sel => (sel === '#mpConcede' ? lbox.concede
                        : sel === '#mpLockNote' ? lbox.note : null),
};
lbox.showTab = t => lbox.shown.push(t);
lbox.api = async () => ({ ok: true });
lbox.confirm = () => true;
lbox.mpPoll = () => {};
lbox.window = lbox;
vm.createContext(lbox);
vm.runInContext(html.slice(lkAt, html.indexOf('function mpAvailable(){', lkAt)), lbox);

lbox.lastState = {};
lbox.applyMpLock();
ok(lbox.tabs.every(t => t.style.display === ''), 'no match: every tab is there');
ok(lbox.concede.style.display === 'none', '…and nothing to concede');

lbox.lastState = { mp_in_match: true, mp_locked: false, mp_peer: 'Dave' };
lbox.applyMpLock();
ok(lbox.tabs.every(t => t.style.display === ''),
   'the HOST keeps its panel — it is running the show');
ok(lbox.concede.style.display === '', '…but can concede');

lbox.shown = [];
lbox.lastState = { mp_in_match: true, mp_locked: true, mp_peer: 'Dave' };
lbox.applyMpLock();
ok(lbox.tabs[0].style.display === '' && lbox.tabs[1].style.display === 'none'
   && lbox.tabs[2].style.display === 'none',
   'the GUEST is locked to Chat — every other tab is gone');
ok(lbox.shown[0] === 'chat', '…and is moved there');
ok(lbox.note.style.display === '' && /Dave/.test(lbox.note.innerHTML),
   '…and told why, naming who it is playing');
ok(/not a fault/.test(lbox.note.innerHTML),
   '…in words that read as the deal, not as an error');

lbox.shown = [];
lbox.applyMpLock();
ok(lbox.shown.length === 0,
   'a second poll does not yank them back to Chat — they are only moved once');

lbox.lastState = { mp_in_match: false, mp_locked: false };
lbox.applyMpLock();
ok(lbox.tabs.every(t => t.style.display === ''),
   'the lock RELEASES when the match ends — it is driven by state, not set once');
ok(lbox.note.style.display === 'none' && lbox.concede.style.display === 'none',
   '…and the banner and Concede go with it');

const conAt = html.indexOf('async function mpConcede()');
const con = html.slice(conAt, html.indexOf('function mpAvailable(){', conAt));
ok(/confirm\(/.test(con), 'Concede asks first — it stops both pumps');
// Concede is NOT an abort. It tells the other bot, runs the End Condition's
// action block — the outro, the result card, whatever the author wrote — and
// only then drops the handshake. Aborting first would tear the match down with
// the block still to run and nothing left to run it on.
ok(/\/api\/mp\/concede/.test(con) && /conceded/.test(con),
   '…then CONCEDES, so the End Condition still gets to play');
ok(!/\/api\/mp\/abort/.test(con), '…rather than tearing the match down first');

// ---- the server list reaches BOTH sets of pickers --------------------------
// guildsCache arrives asynchronously, after the applyState that drew the
// pickers. Whoever loads it has to refresh them, or the multiplayer ones come
// up empty while the solo ones fill.
const lgAt = html.indexOf('async function loadGuilds(){');
const lg = html.slice(lgAt, html.indexOf('\nfunction ', lgAt));
ok(/mpApply\(/.test(lg),
   'loadGuilds refreshes the multiplayer pickers too, not just the solo ones');
const teAt = html.indexOf('function tabEnter(name){');
const te = html.slice(teAt, html.indexOf('\nfunction ', teAt));
ok(/name==='discord'/.test(te),
   'and the Discord tab loads the server list on entry — it owns the two match '
   + 'channels now, so the hook cannot stay on the Multiplayer tab');
ok(/name==='system'/.test(te),
   '…and so does System, which carries the preflight that checks them');
ok(!/mpStopPoll/.test(te),
   'entering a tab no longer stops the status poll: the match is run from the '
   + 'header and an invite opens a modal by itself, so it must keep running '
   + 'wherever you are');

// ---- mode-aware pages ------------------------------------------------------
const cards = [...html.matchAll(/<div class="card span" data-tab="(\w+)"(?: data-mode="(\w+)")?>/g)]
  .map(m => ({ tab: m[1], mode: m[2] || null }));
const trig = cards.filter(c => c.tab === 'triggers');
ok(trig.length > 1 && trig.every(c => c.mode),
   'every Triggers card declares which mode it belongs to');
ok(trig.some(c => c.mode === 'multi') && trig.some(c => c.mode === 'solo'),
   'Triggers has both a solo and a multiplayer face');
const help = cards.filter(c => c.tab === 'help');
ok(help.some(c => c.mode === 'multi') && help.some(c => c.mode === 'solo')
   && help.some(c => c.mode === null),
   'Help has multiplayer-only, solo-only AND global cards — so the multiplayer '
   + 'page is not a duplicate of everything');

const vStart = html.indexOf('function mpAvailable(){');
const vEnd = html.indexOf('\nfunction ', html.indexOf('function applyModeVis(){'));
ok(vStart > 0 && vEnd > vStart, 'one visibility switch rather than one per page');
const clx = () => ({ add() {}, remove() {}, toggle() {}, contains: () => false });
// the banner lives outside this slice; record what it was asked for so the
// mode switch can be checked without pulling the whole panel in
const vbox = { console, lastState: { mode: 'solo' }, warned: [] };
vbox.mpWarnBanner = on => vbox.warned.push(!!on);
vbox.els = [{ getAttribute: () => 'solo', style: {}, classList: clx() },
            { getAttribute: () => 'multi', style: {}, classList: clx() }];
vbox.document = { querySelectorAll: sel => (sel === '[data-mode]' ? vbox.els : []),
                  querySelector: () => null };
vbox.window = vbox;
vm.createContext(vbox);
vm.runInContext(html.slice(vStart, vEnd), vbox);
vbox.applyModeVis();
ok(vbox.warned[vbox.warned.length - 1] === false,
   'solo shows no multiplayer warning');
ok(vbox.els[0].style.display === '' && vbox.els[1].style.display === 'none',
   'in solo, multiplayer cards are hidden');
vbox.lastState = { mode: 'multi' };
vbox.applyModeVis();
ok(vbox.els[0].style.display === 'none' && vbox.els[1].style.display === '',
   '…and the other way round in multiplayer');
ok(vbox.warned[vbox.warned.length - 1] === true,
   '…and switching INTO multiplayer raises the "not tested yet" warning');
vbox.lastState = { mode: 'multi', multiplayer_enabled: false };
vbox.applyModeVis();
ok(vbox.els[0].style.display === '' && vbox.els[1].style.display === 'none',
   'a build without multiplayer shows the solo half even when the stored mode '
   + 'says multi — the toggle that would get you out is hidden');
ok(vbox.warned[vbox.warned.length - 1] === false,
   '…and a build WITHOUT multiplayer shows no warning about it either');
ok(vbox.mpAvailable() === false, 'the gate reads the flag');
vbox.lastState = { mode: 'solo' };
ok(vbox.mpAvailable() === true,
   '…and an ABSENT flag means available, so an older server still works');

// ---- the Scenes tab layout -------------------------------------------------
// the whole section, so nesting can be checked as well as contents
const stgAt = html.indexOf('🎭 Scenes');
const stgSec = html.slice(stgAt, html.indexOf('</details>', stgAt));
// The canvas column owns the intro line and the picker, so the lists beside it
// start at the TOP of the section instead of below them.
ok(stgSec.indexOf('<div class="stgstage">') < stgSec.indexOf('<select id="scnSel"'),
   'the two-column split opens BEFORE the scene picker');
ok(stgSec.indexOf('class="stgmain"') < stgSec.indexOf('<select id="scnSel"'),
   '…and the picker sits inside the canvas column');
ok(stgSec.indexOf('class="stgmain"') < stgSec.indexOf('scene is the whole show'),
   '…as does the intro line, so neither pushes the lists down');
ok(stgSec.indexOf('<select id="scnSel"') < stgSec.indexOf('class="stgside"'),
   '…and the lists column comes after it, spanning the whole height');
// nesting must actually balance, or the card swallows what follows it
let depth = 0;
for (const m of stgSec.matchAll(/<(\/?)div\b/g)) depth += m[1] ? -1 : 1;
ok(depth === 0, `the section's divs balance (got ${depth})`);

const hdr = html.slice(html.indexOf('<select id="scnSel"'), html.indexOf('id="stgEditor"'));
ok(!/id="stgModeWrap"/.test(hdr),
   'the Mode selector is NOT in the header row above both columns');
ok(/stgExport\(\)/.test(hdr) && /stgImport\(/.test(hdr),
   'Export/Import moved up here, with copy and delete');
ok(/\.stgside[\s\S]{0,120}style\.display = s \? '' : 'none'/.test(
     html.slice(html.indexOf('function renderStages(){'),
                html.indexOf('function renderStages(){') + 1200)),
   'the lists hide with the editor — they left #stgEditor, which used to do '
   + 'that for free, and an empty palette beside a missing canvas reads as broken');

const under = html.slice(html.indexOf('class="stgunder"'), html.indexOf('id="stgProps"'));
ok(/id="stgModeWrap"/.test(under),
   'the Mode selector sits with Resolution under the canvas — its height costs '
   + 'the lists nothing');
ok(!/stgExport\(\)/.test(under),
   'Export/Import are gone from under the canvas — their min-content width was '
   + 'what stopped that column shrinking, which capped the lists');
const cssAt = html.indexOf('.stgstage .stgside{');
const css = html.slice(cssAt, html.indexOf('@media', cssAt));
ok(/clamp\(320px, 40%, 560px\)/.test(css), 'the lists column got the freed width');
// Height is BORROWED: align-items:stretch makes this column as tall as the
// canvas column, and the canvas is capped at 900px wide — so a small scene or
// an empty properties panel used to leave the lists a squat strip.
ok(/min-height:min\(72vh/.test(css),
   'the lists have a height floor of their own, so they never depend on how '
   + 'tall the canvas happens to be');
const paneAt = html.indexOf('.stgstage .stgside .stgpane{');
const pane = html.slice(paneAt, html.indexOf('}', paneAt) + 1);
ok(/flex:1 1 0/.test(pane), 'both panes divide that height from a zero basis…');
ok(/min-height:0/.test(pane),
   '…with no floor on each, or they cannot shrink together and one ends up '
   + 'taller than the other');
// anchor on the rule itself — there is more than one @media(max-width:820px)
const narrowAt = html.indexOf('.stglists{display:grid');
const narrow = html.slice(narrowAt, html.indexOf('}', html.indexOf('.stglist{max-height', narrowAt)) + 1);
ok(/max-height:min\(44vh/.test(narrow),
   'and stacked on a narrow screen they get a usable height, not six rows');

// ---- the scene Mode control is Versus/Gameshow, never solo/multi -----------
const sStart = html.indexOf('function sceneMode(s){');
const sEnd = html.indexOf('function stgSetGlobal(on){');
const sEls = {};
['stgMode', 'stgModeWrap', 'stgModeHint', 'scnSel'].forEach(id => { sEls[id] = El(id); });
let toasts = [], saves = 0;
const scenes = [{ name: 'Tuesday Show' },
                { name: 'Race Night', mode: 'multi' },
                { name: 'Built In', mode: 'multi', builtin: true }];
const sbox = {
  console, esc: sandbox.esc, scenesData: scenes, stgCur: 0, stgGlobalMode: false,
  scnSelId: null, stgGroupFilter: '', stgPending: new Set(),
  lastState: { mode: 'multi' },
  $: sel => sEls[String(sel).replace(/^#/, '')] || null,
  document: { activeElement: null },
  toastMsg: m => toasts.push(m), scheduleSave: () => { saves++; },
  renderStages: () => {}, isBuiltinScene: sc => !!(sc && sc.builtin),
  globalScene: () => ({ name: 'globals' }), showReload: () => {},
  sceneSwitchSave: () => {}, stgSyncLive: () => {}, stgRemember: () => {},
};
sbox.window = sbox;
vm.createContext(sbox);
vm.runInContext(html.slice(sStart, sEnd), sbox);
sbox.stgStage = () => (sbox.stgGlobalMode ? sbox.globalScene() : (scenes[sbox.stgCur] || null));

ok(sbox.sceneMode({ name: 'x' }) === 'solo',
   'an untagged scene is solo — nothing written before multiplayer disappears');
sbox.stgCur = 1;
sbox.stgModeUi();
ok(sEls.stgModeWrap.style.display === '' && sEls.stgMode.value === 'operators',
   'a multi scene offers Versus / Gameshow, defaulting to Versus');
ok(/Rounds/.test(sEls.stgModeHint.innerHTML),
   '…and says the ROUNDS are the game, not a setting here');
sbox.stgSetMode('audience');
ok(scenes[1].input === 'audience' && saves === 1, 'switching to Gameshow saves it');
ok(!('mode' in scenes[1]) || scenes[1].mode === 'multi',
   'and it NEVER writes `mode` — a scene does not move between solo and multi');
sbox.stgCur = 0;
sbox.stgModeUi();
ok(sEls.stgModeWrap.style.display === 'none',
   'Versus / Gameshow is hidden in solo, where it means nothing');
sbox.stgCur = 2;
toasts = [];
sbox.stgSetMode('audience');
ok(!scenes[2].input && /read-only/.test(toasts[0] || ''),
   'a shipped scene refuses the change with a reason');

// ---- the action serializer -------------------------------------------------
// cleanActionRows is THE serializer: a field added to actionRow() that isn't
// added here is silently stripped on save.
const cStart = html.indexOf('function cleanActionRows(arr){');
const cbox = { console, s: v => String(v == null ? '' : v).trim() };
cbox.window = cbox;
vm.createContext(cbox);
vm.runInContext(html.slice(cStart, html.indexOf('function cleanPolls(){')), cbox);

const ROUND = [
  { type: 'mp_spin', message: '🎡 picks [multi_chosen_name]', announce_odds: false },
  { type: 'mp_roll', dice: 1, sides: 8, luck: 5 },
  { type: 'mp_action', action: 'MultiRoulette' },
  { type: 'fire', fire_mode: 'add', fill_pct: '[multi_roll]', multi_who: 'chosen' },
  { type: 'wait', seconds: 15 },
  { type: 'mp_duel', seconds: 999, title: 'RPS',
    options: [{ label: '🪨 Rock', beats: ['scissors'] },
              { label: '✂️ Scissors', value: 'scissors', beats: ['paper'] }] },
  { type: 'mp_choice', multi_who: 'chosen', seconds: 1, default: 'take',
    options: [{ label: 'Double', style: 'danger',
                actions: [{ type: 'fire', fire_mode: 'add', fill_pct: 5, multi_who: 'loser' }] },
              { label: 'Take it', value: 'take', actions: [] }] },
];
const out = cbox.cleanActionRows(ROUND);
ok(out[0].type === 'mp_spin' && out[0].announce_odds === false, 'a spin round-trips');
ok(out[1].dice === 1 && out[1].sides === 8 && out[1].luck === 5, 'the dice survive');
ok(out[2].action === 'MultiRoulette', 'an mp_action keeps which Action it runs');
ok(out[3].multi_who === 'chosen' && out[3].fill_pct === '[multi_roll]',
   'multi_who and a placeholder amount both survive');
ok(!('multi_who' in out[4]), 'a row with no target does not gain an empty one');
const duel = out[5];
ok(duel.seconds === 180, 'an absurd duel deadline is clamped');
ok(duel.options[0].value === '🪨 rock' || duel.options[0].value,
   'a move with no explicit value gets one from its label');
ok(duel.options[0].beats.includes('scissors'), '…and keeps what it beats');
const ch = out[6];
ok(ch.seconds === 5, 'too short a choice is clamped up, not left to fire instantly');
ok(ch.options[0].actions[0].multi_who === 'loser',
   'a button’s nested block survives, targets and all');

const gStart = html.indexOf('function actGist(a){');
const gbox = { console, s: cbox.s, esc: sandbox.esc };
gbox.window = gbox;
vm.createContext(gbox);
vm.runInContext(html.slice(gStart, html.indexOf('\nfunction ', gStart + 10)), gbox);
ok(/spin the wheel/.test(gbox.actGist(ROUND[0])), 'a collapsed spin says what it is');
ok(gbox.actGist(ROUND[1]) === 'roll 1d8 → [multi_roll]', 'a collapsed roll names its dice');
ok(/MultiRoulette/.test(gbox.actGist(ROUND[2])), 'a collapsed mp_action names it');
ok(/both pick/.test(gbox.actGist(ROUND[5])), 'a collapsed duel says both pick');

// ---- the Rounds editor -----------------------------------------------------
const rStart = html.indexOf('// ---- Multiplayer Rounds ---');
const rEnd = html.indexOf('// ---- multiplayer (the rail) ---', rStart);
ok(rStart > 0 && rEnd > rStart, 'the Rounds editor is where the test expects it');
const rEls = { mpRndList: El('mpRndList'), mpRndMsg: El('mpRndMsg') };
let rSaves = 0;
const rbox = {
  console, esc: sandbox.esc,
  mpActions: [{ name: 'MultiRoulette' }, { name: 'MultiRPS' }],
  $: sel => rEls[String(sel).replace(/^#/, '')] || null,
  // marks each block so the test can count them per round
  actionsSection: label => `[[BLOCK:${label}]]`,
  cleanActionRows: a => a || [],
  shiftSet: set => set, confirm: () => true,
  scheduleSave: () => { rSaves++; }, api: async () => ({ ok: true }),
  setTimeout, clearTimeout,
};
rbox.window = rbox;
vm.createContext(rbox);
vm.runInContext(html.slice(rStart, rEnd), rbox);
const R = () => vm.runInContext('mpRounds', rbox);
const setR = v => { rbox._in = v; vm.runInContext('mpRounds = _in;', rbox); };

rbox.mpRndAdd();
ok(R().length === 1 && R()[0].until === 'count' && R()[0].count === 1,
   'a new round defaults to Count \u00d7 1 — the plain case');
ok((R()[0].actions || []).length === 1,
   '\u2026and starts with a call to an Action, not an empty box');

setR([{ name: 'Four', until: 'count', count: 4, actions: [{ type: 'mp_action' }] },
      { name: 'Final', until: 'leader', max: 100, actions: [{ type: 'mp_action' }] }]);
rbox.renderMpRounds();
const rHtml = rEls.mpRndList.innerHTML;
const [r1, r2] = rHtml.split('</details>');
ok(/1\. Four<\/b> <span class="muted">\u00d74/.test(rHtml),
   'a Count round says how many times, right in the summary');
ok(/2\. Final<\/b> <span class="muted">first to 100%/.test(rHtml),
   '\u2026and a target round says what it is waiting for');
ok(/>Times\s*</.test(r1) && !/>Target %\s*</.test(r1),
   'Count asks for a number of times\u2026');
ok(/>Target %\s*</.test(r2) && !/>Times\s*</.test(r2), '\u2026and % asks for a percentage');
ok(!/Give up after/.test(r1),
   'a Count round needs no give-up limit — the count IS the limit');
ok(/Give up after/.test(r2),
   '\u2026while a target round does: one nobody can reach still has to end');
ok((r1.match(/\[\[BLOCK:/g) || []).length === 2,
   'each round has its own action block AND its round card, both collapsible '
   + 'rows like a custom command\u2019s');
ok(/mpRndMove\(0,1\)/.test(r1) && /disabled>\u25b2/.test(r1),
   'rounds reorder with arrows, and the first cannot move up');

// what actually saves
setR([{ name: 'A', until: 'count', count: 9999, actions: [{ type: 'mp_action' }] }]);
ok(rbox.cleanMpRounds()[0].count === 200, 'an absurd count is clamped');
ok(!('max' in rbox.cleanMpRounds()[0]),
   '\u2026and a Count round stores no target, so there is nothing to disagree with');
setR([{ name: 'B', until: 'leader', max: 30, max_passes: 9999,
        actions: [{ type: 'mp_action' }] }]);
const cb2 = rbox.cleanMpRounds()[0];
ok(cb2.max === 30 && cb2.max_passes === 200 && !('count' in cb2),
   'a target round stores its target and a clamped give-up, and no count');
setR([{ name: 'C', until: 'count', count: 2, actions: [] }]);
ok(rbox.cleanMpRounds().length === 0,
   'a round with no actions is dropped \u2014 there is nothing to run');

// ---- the spread bet, per round ---------------------------------------------
setR([{ name: 'D', until: 'count', count: 1, actions: [{ type: 'mp_action' }] }]);
ok(!('spread_bet' in rbox.cleanMpRounds()[0]),
   'a round with no bet on it stores nothing — the tickbox is opt-in');
setR([{ name: 'E', until: 'count', count: 1, actions: [{ type: 'mp_action' }],
        spread_bet: { enabled: true, stake: 9999, seconds: 1 } }]);
const sb = rbox.cleanMpRounds()[0].spread_bet;
ok(sb && sb.enabled === true, 'a ticked round carries its bet');
ok(sb.stake === 999, '…with the stake clamped, so a typo cannot end a match instantly');
ok(sb.seconds === 10,
   '…and a floor on the deadline: two seconds to type a number is not a decision');
vm.runInContext('mpRndOpen = new Set([0]);', rbox);
rbox.renderMpRounds();
const rD = rEls.mpRndList.innerHTML;
ok(/Spread bet/.test(rD) && /without going over/.test(rD),
   'the editor states the rule rather than assuming you remember it');
ok(/winner pays half/.test(rD),
   '…and that the winner still pays half, which is the whole balance of it');
setR([{ name: 'D', until: 'leader', max: 0, actions: [{ type: 'mp_action' }] }]);
ok(rbox.cleanMpRounds().length === 0,
   '\u2026and so is a % round with no target: it could never end');

setR([{ name: 'first', until: 'count', count: 1, actions: [{ type: 'mp_action' }] },
      { name: 'second', until: 'count', count: 1, actions: [{ type: 'mp_action' }] }]);
rbox.mpRndMove(1, -1);
ok(R()[0].name === 'second', 'the arrows actually reorder the run order');

// ---- saving ----------------------------------------------------------------
els.mpNetGuild.value = 'g1'; els.mpNetChan.value = '900';
els.mpCastGuild.value = 'g1'; els.mpCastChan.value = '901';
const MC = () => vm.runInContext('mpCfg', sandbox);
(async () => {
  aSaves = 0;
  await sandbox.mpSave();
  ok(aSaves === 1, 'editing a multiplayer setting goes through the ONE save path');
  const m = MC() || {};
  ok(m.bot_network.channel_id === '900' && m.broadcast.channel_id === '901',
     'saving writes both channels');
  // Names are chosen in the invite modal — the only place they are ever set —
  // so a save carries whatever is already held rather than reading a field
  // that no longer exists.
  ok(m.video && m.video.required === true,
     'saving records that a match requires both players on camera');
  ok(!('race' in m) && !('commands' in m),
     'saving writes no race terms and no command names — neither exists any more');
  ok(!('input' in m),
     '\u2026and no `input`: whose hands are on a match is the SCENE\u2019s '
     + 'Versus/Gameshow, and a second answer written here could disagree with it');

  // Inviting and answering are the MODALS' job now — there is no second path.
  ok(!/async function mpOffer\(/.test(html) && !/async function mpRespond\(/.test(html),
     'the old tab-scoped invite/answer handlers are gone, not left wired to '
     + 'fields that no longer exist');
  report();
})();

function report() {
  // ---- polling follows the MODE, not a tab ----------------------------------
// The Multiplayer tab is gone: the header runs the match and an invite opens a
// modal by itself. Polling that stopped when you left a tab would mean an
// invite arriving while you were on Scenes was never seen at all.
{
  const pollJs = html.slice(html.indexOf('async function mpPoll('),
                            html.indexOf('function mpStopPoll('));
  ok(/mode === 'multi'/.test(pollJs),
     'the poll keeps running on MODE, wherever you are in the panel');
  ok(!/data-tab="multi"/.test(pollJs),
     '…and not on a tab that no longer exists');
}
ok(!/data-tab="multi"/.test(html) && !/data-go="multi"/.test(html),
   'nothing in the panel still points at the removed Multiplayer tab');
ok(!/id="mpLimFire"/.test(html) && !/id="mpOnExceed"/.test(html),
   'the ceiling editors are gone with it');

// ---- Max volume: the lose-at capacity, and the unit stakes are priced in ---
// On the Chat page because it is a dial you reach for while setting up. It is
// FROZEN during a match: it is agreed at invite time and carried in the
// envelope, so letting it move afterwards would re-price a deal both players
// had already agreed to.
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false,
                  mp_end: { max_capacity: 100 } });
ok(els.mpMaxVol.value === '100', 'the dial shows the configured ceiling');
ok(els.mpMaxVol.disabled === false, '…and turns freely before a match');
ok(els.mpMaxVolNote.textContent === '%', 'at 100 there is no multiplier to mention');

sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false,
                  mp_end: { max_capacity: 200 } });
ok(/×2/.test(els.mpMaxVolNote.textContent),
   'a bigger ceiling SAYS it doubles every stake — the scaling must not be '
   + 'invisible, or a row that reads 5 and fires 10 is inexplicable');
sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false,
                  mp_end: { max_capacity: 50 } });
ok(/×0\.5/.test(els.mpMaxVolNote.textContent), '…and that a smaller one halves them');

sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: false,
                  mp_end: { max_capacity: 0 } });
ok(/concede only/.test(els.mpMaxVolNote.textContent),
   'zero says what it means: no capacity ending, conceding is the only way out');

sandbox.mpApply({ mode: 'multi', multiplayer: CFG, listener_enabled: true,
                  mp_in_match: true, mp_end: { max_capacity: 100 } });
ok(els.mpMaxVol.disabled === true, 'it LOCKS once a match is running');
ok(/locked/.test(els.mpMaxVolNote.textContent), '…and says why');

const mvJs = html.slice(html.indexOf('async function mpMaxVolSet('),
                        html.indexOf('function mpMaxVolUi('));
ok(/Math\.min\(999/.test(mvJs) && /Math\.max\(0/.test(mvJs),
   'the dial clamps to 0-999, so a typo cannot set a ceiling nobody reaches');

// ---- the two match modals --------------------------------------------------
// ✉ Invite lives in the header and only appears when it could do anything.
// The invitee's Accept/Decline opens by STATE, never by a button: one that sat
// unnoticed behind a tab is one that times out.
sandbox.modals.length = 0;
const seatAs = r => vm.runInContext(
  `mpCfg = {role_pref:'${r}'}`, sandbox);
seatAs('host');
const inviteBtn = els.mpInviteBtn;

sandbox.mpRender({ mode: 'multi', listener_enabled: true, state: 'advertised', preflight: [] });
ok(inviteBtn.style.display === '', 'host + live + no match yet → Invite is offered');
sandbox.mpRender({ mode: 'multi', listener_enabled: false, state: 'idle', preflight: [] });
ok(inviteBtn.style.display === 'none', '…not before you go live: nothing is armed yet');
seatAs('guest');
sandbox.mpRender({ mode: 'multi', listener_enabled: true, state: 'advertised', preflight: [] });
ok(inviteBtn.style.display === 'none', '…and never to the GUEST — they wait to be asked');
seatAs('host');
sandbox.mpRender({ mode: 'solo', listener_enabled: true, state: 'idle', preflight: [] });
ok(inviteBtn.style.display === 'none', '…nor in solo');
sandbox.mpRender({ mode: 'multi', listener_enabled: true, state: 'advertised',
                   preflight: [{ ok: false, check: 'broadcast', why: 'no channel picked' }] });
ok(inviteBtn.disabled === true && /no channel picked/.test(inviteBtn.title),
   '…and a failed preflight disables it WITH the reason, not silently');

// the invitee's modal, opened by state
sandbox.modals.length = 0;
sandbox.mpRender({ mode: 'multi', state: 'invited', calibration: 90, preflight: [],
  invite: { host: 'Curtis', bot: 'C-bot', game: 'Race', cal: 60, cap: 150, split: true,
            cost: 'race to 75%', input: 'operators',
            venue: { cast: { channel: 'stage', kind: 'voice' }, net: { channel: 'wire' } } } });
ok(sandbox.modals.length === 1, 'an invite opens a modal on the invitee, by itself');
const im = sandbox.modals[0].innerHTML;
ok(/Curtis wants to play/.test(im), '…naming who is asking');
ok(/race to 75%/.test(im), '…what it will cost');
ok(/you lose at <b>150%<\/b>/.test(im), '…and what losing looks like, as a number');
ok(/#stage/.test(im) && /🔊/.test(im), '…where it is played, and that it is a voice channel');
ok(/id="mpiMyCal"/.test(im) && /value="90"/.test(im),
   '…your own pump speed, editable, because every target is paced from it');
ok(/value="60" disabled/.test(im), '…theirs beside it, read-only');
ok(/id="mpiTakeSplit"/.test(im), '…the split offer, because they made one');
ok(/on camera/.test(im), '…and that accepting puts you on camera');
ok(/id="mpiYes"/.test(im) && /id="mpiNo"/.test(im) && /id="mpiBlock"/.test(im),
   '…with Accept, Decline and Block');

// it does NOT reopen while it is already up, and it closes when the invite goes
sandbox.mpRender({ mode: 'multi', state: 'invited', calibration: 90, preflight: [], invite: {} });
ok(sandbox.modals.length === 1, 'a second poll does not stack another copy on top');
sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [] });
ok(sandbox.modals[0].removed === true,
   'and it closes itself when the invite is withdrawn or times out');

// no split offered → no box to tick
sandbox.modals.length = 0;
sandbox.mpRender({ mode: 'multi', state: 'invited', calibration: 90, preflight: [],
  invite: { host: 'C', cal: 60, cap: 100, venue: {} } });
ok(!/id="mpiTakeSplit"/.test(sandbox.modals[0].innerHTML),
   'no split box unless the host offered one — you cannot re-tune their rig');
sandbox.mpRender({ mode: 'multi', state: 'idle', preflight: [] });

// ---- Help describes the game that actually ships ---------------------------
// A help page describing a game that was removed is worse than no help page:
// it sends somebody looking for a screen that does not exist.
{
  const mp = html.slice(html.indexOf('two installs, one game'),
                        html.indexOf('Custom commands — how to set them up'));
  ok(!/Race to N%/.test(mp),
     'the removed fallback game is gone from Help, not left describing a '
     + 'screen nobody can find');
  ok(!/!boost/.test(mp) && !/!stall/.test(mp),
     '…and so are its commands');
  for (const row of ['mp_spin', 'mp_roll', 'mp_choice', 'mp_duel',
                     'mp_cards', 'mp_simon', 'mp_ttt', 'mp_tell']) {
    ok(mp.includes('<code>' + row + '</code>'), `Help lists ${row}`);
  }
  ok(/Round 4 · Simon/.test(mp) && /Round 3 · Blackjack/.test(mp),
     'the shipped four rounds are described');
  ok(/who cracks first/.test(mp),
     'Sudden Death explains why a solved game is the RIGHT decider');
  ok(/winner pays half/.test(mp), 'the spread bet explains its own balance');
  ok(/none of them\s*\n?\s*fires a pump on its own|fires a pump on its own/.test(mp),
     'and that a game decides who lost while a separate fire spends it — which '
     + 'is what lets a stake change without touching the game');
}

// ---- the "not tested yet" banner -------------------------------------------
// Multiplayer is tested only against test doubles. Somebody switching into it
// needs telling BEFORE they build a show on it.
{
  const b = html.slice(html.indexOf('function mpWarnBanner('),
                       html.indexOf('function offlineBanner('));
  ok(/position:fixed/.test(b) && /top:0/.test(b),
     'the warning sits across the top, not buried in a help page');
  ok(/NOT PERSON-TO-PERSON TESTED/.test(b),
     '…and says the specific thing that is true: no two real installs have '
     + 'played a match');
  ok(/Solo is unaffected/.test(b),
     '…and that solo is fine, so it does not read as "the app is broken"');
  ok(/Multi<\/b> back off/.test(b), '…and how to get out of it');
  ok(!/dismiss|close|✕/i.test(b),
     'it is NOT dismissible: it describes a standing state, and it goes away '
     + 'by leaving multiplayer');
  ok(/paddingTop/.test(b),
     '…and it makes room for itself rather than covering the header');

  const vis = html.slice(html.indexOf('function applyModeVis()'),
                         html.indexOf('function pushGameplay('));
  ok(/mpWarnBanner\(m === 'multi'\)/.test(vis),
     'it is driven by the MODE, so switching in shows it and switching out '
     + 'hides it');
}

// ---- the red dot on System, and clearing it --------------------------------
{
  const dotJs = html.slice(html.indexOf('function renderBlocked('),
                           html.indexOf('async function clearLog('));
  ok(/data-go="system"/.test(dotJs), 'the dot goes on the System tab');
  ok(/tabdot/.test(dotJs) && /dot.remove\(\)/.test(dotJs),
     '…and is REMOVED when the problems go, not just added');
  ok(/missing:/.test(dotJs) && /server admin/.test(dotJs),
     'the banner names the permission and who can grant it — the operator '
     + 'usually cannot fix this themselves');
  ok(/Edit Channel/.test(dotJs),
     '…and where to click, so it is a remedy rather than a complaint');

  const clr = html.slice(html.indexOf('async function clearLog('),
                         html.indexOf('function renderLog('));
  ok(/api\('\/api\/log\/clear'/.test(clr),
     'Clear goes to the server, which owns both the log and the problems');
  ok(/applyState/.test(clr),
     '…and re-renders from the response, so the dot goes with the log rather '
     + 'than lingering until the next poll');
  ok(/>Clear</.test(html), 'and there is a Clear button to press');
}

// ---- the permission help says WHY, not just WHAT ---------------------------
// All four are granted to @everyone by default, so an operator has probably
// never had to set one and will not recognise the failure when one is revoked.
{
  const two = html.slice(html.indexOf('The two channels'),
                         html.indexOf('Fair races between unequal pumps'));
  ok(/never had to grant/.test(two),
     'it says these are on by default, so nobody hunts for a setting they '
     + 'already have');
  ok(/worst-named/.test(two) && /nothing to do with URLs/.test(two),
     'Embed Links is explained: it governs rich CARDS, not links');
  ok(/do not post at all/.test(two),
     '…and that losing it means the cards silently never appear');
  ok(/protocol.s log/.test(two) && /catches up/.test(two),
     'Read Message History is explained: bot_network IS the log, which is what '
     + 'makes reconnecting free');
}

// ---- the placeholder list is duplicated ON PURPOSE, so guard the copy -----
// It appears in "Placeholders & examples" and again in the Multiplayer help
// section, so you don't have to leave the page you're reading. Two copies of a
// list is exactly where rot starts, so they must be identical.
{
  const ph = [...html.matchAll(/<code>\[(multi_[a-z_]+)\]<\/code>/g)].map(m => m[1]);
  const counts = {};
  ph.forEach(n => { counts[n] = (counts[n] || 0) + 1; });
  const once = Object.keys(counts).filter(n => counts[n] < 2);
  ok(once.length === 0,
     'every [multi_…] documented in the table is ALSO in the Multiplayer help '
     + 'section: ' + once.join(', '));
  ok(Object.keys(counts).length >= 40,
     'and the list is the whole set, not a handful: ' + Object.keys(counts).length);
}

console.log(`${P} passed, ${F.length} failed`);
  F.forEach(f => console.log('  FAIL:', f));
  process.exit(F.length ? 1 : 0);
}
