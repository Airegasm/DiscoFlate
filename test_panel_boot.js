// Does the panel script actually RUN?
//
// `node --check` only proves it parses. It cannot see a `let` read before its
// declaration — that is a TDZ ReferenceError at load, not `undefined` — and a
// single one kills the whole panel with a blank page. Exactly that happened
// when startup showTab() began asking applyModeVis() which mode we are in,
// while `lastState` was still declared 6000 lines further down.
//
// So: evaluate the real script against a DOM stub tolerant enough to get to
// the end. It is not a rendering test. It answers one question — does the file
// survive being loaded — which is the question a blank panel is asking.
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync('web/index.html','utf8');
const m=html.match(/<script>([\s\S]*)<\/script>/);
const src=m[1];

const el = () => new Proxy(function(){}, {
  get(t,k){
    if(k==='style') return {};
    if(k==='classList') return {add(){},remove(){},toggle(){},contains(){return false}};
    if(k==='children'||k==='options') return [];
    if(k==='dataset') return {};
    if(k==='value'||k==='textContent'||k==='innerHTML'||k==='id') return '';
    if(k==='checked'||k==='disabled'||k==='open') return false;
    if(k===Symbol.toPrimitive) return ()=> '';
    return el();
  },
  set(){ return true; },
  apply(){ return el(); },
});
const doc = {
  querySelector: () => el(), querySelectorAll: () => [], getElementById: () => el(),
  createElement: () => el(), addEventListener(){}, body: el(), documentElement: el(),
  activeElement: null, readyState: 'complete', cookie: '',
};
const box = {
  console: {log(){},warn(){},error(){}},
  document: doc, navigator: {userAgent:'x', clipboard:{writeText:()=>Promise.resolve()}},
  location: {search:'', href:'', hostname:'127.0.0.1', protocol:'http:'},
  localStorage: {getItem:()=>null, setItem(){}, removeItem(){}},
  fetch: () => Promise.resolve({ok:true, json:()=>Promise.resolve({}), text:()=>Promise.resolve('')}),
  setTimeout: () => 0, clearTimeout(){}, setInterval: () => 0, clearInterval(){},
  requestAnimationFrame: () => 0,
  URLSearchParams, URL, FormData: function(){}, Image: function(){ return el(); },
  WebSocket: function(){ return el(); }, EventSource: function(){ return el(); },
  alert(){}, confirm:()=>true, prompt:()=>'',
  performance:{now:()=>0}, matchMedia: () => ({matches:false, addEventListener(){}}),
};
box.addEventListener = () => {};
box.removeEventListener = () => {};
box.dispatchEvent = () => true;
box.window = box; box.self = box; box.globalThis = box;
vm.createContext(box);
try {
  vm.runInContext(src, box, {timeout: 8000});
  console.log('1 passed, 0 failed — the panel script evaluates end to end');
} catch (e) {
  console.log('0 passed, 1 failed');
  console.log('  FAIL: the panel does not load —', e.constructor.name + ':', e.message);
  process.exit(1);
}
