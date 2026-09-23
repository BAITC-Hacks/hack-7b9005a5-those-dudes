const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'dist/app.js'),'utf8');
const navigation=source.slice(source.indexOf('  function setupNavigation()'),source.indexOf('  function renderHeader()'));
function harness(hash='#network') {
  const classes=()=>({values:new Set(),toggle(key,on){on?this.values.add(key):this.values.delete(key);}});
  const buttons=['network','transactions','ai'].map(name=>({dataset:{view:name},classList:classes(),attributes:{'aria-label':name},handlers:{},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},getAttribute(k){return this.attributes[k];},addEventListener(k,v){this.handlers[k]=v;}}));
  const views=buttons.map(b=>({id:'view-'+b.dataset.view,classList:classes()}));
  const location={pathname:'/dashboard',search:'?run=abcdef0123456789abcdef0123456789',hash};
  const handlers={},urls=[],document={body:{dataset:{}},title:''};let resizes=0;
  const window={location,history:{pushState(_state,_title,url){urls.push(url);location.hash='#'+url.split('#')[1];}},addEventListener(name,fn){handlers[name]=fn;}};
  vm.runInNewContext(navigation+'\nsetupNavigation();',{window,document,$$:selector=>selector==='.nav-item'?buttons:views,network:{resize(){resizes++;}}});
  return {buttons,views,document,location,handlers,urls,get resizes(){return resizes;}};
}
test('a direct section link opens the requested view after reload without replacing its run',()=>{
  const h=harness('#transactions');assert.equal(h.document.body.dataset.view,'transactions');
  assert.equal(h.buttons[1].attributes['aria-current'],'page');assert.equal(h.views[1].classList.values.has('active'),true);assert.equal(h.urls.length,0);
});
test('section navigation keeps run in the URL, with no duplicate entries on repeat clicks',()=>{
  const h=harness();h.buttons[2].handlers.click();h.buttons[2].handlers.click();
  assert.deepEqual(h.urls,['/dashboard?run=abcdef0123456789abcdef0123456789#ai']);assert.equal(h.buttons[0].attributes['aria-current'],undefined);
});
test('Back/Forward and manual hash changes restore exactly one visible section',()=>{
  const h=harness('#ai');h.location.hash='#network';h.handlers.popstate();
  assert.equal(h.document.body.dataset.view,'network');assert.equal(h.resizes,1);
  h.location.hash='#transactions';h.handlers.hashchange();
  assert.equal(h.views.filter(v=>v.classList.values.has('active')).length,1);assert.equal(h.document.title,'transactions — Граф денег');
});
test('unknown fragments fall back to the network without a redirect or changing run',()=>{
  const h=harness('#<script>');assert.equal(h.document.body.dataset.view,'network');assert.equal(h.urls.length,0);
});
