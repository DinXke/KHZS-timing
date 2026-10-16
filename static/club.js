/* HZS Timing – "Club": de zwemmers van één gekozen club overal laten opvallen (live timing, jury, oproepkamer,
   startblok, vorige reeksen). Keuze per toestel (localStorage). Ook: kleurenpalet volgen (instelling "palette"). */
(function(){
'use strict';
var KEY='lt_club',cur='';
try{cur=localStorage.getItem(KEY)||''}catch(e){}
var css=''+
'.myclub,tr.myclub>td{background:color-mix(in srgb,var(--warning,#fbbf24) 17%,transparent)!important}'+
'tr.myclub>td:first-child,.myclub:not(tr){box-shadow:inset .25em 0 0 var(--warning,#fbbf24)}'+
'tr.myclub[style*="grid"],tbody tr.myclub{box-shadow:inset .25em 0 0 var(--warning,#fbbf24)}'+
'@media (max-width:699px),(max-height:499px) and (max-width:1100px){tbody tr.myclub>td{background:transparent!important}tr.myclub>td:first-child{box-shadow:none}}'+
'#clubbtn.on{background:var(--warning,#fbbf24);color:var(--onwarn,#111);border-color:var(--warning,#fbbf24)}'+
'#clubpop{position:fixed;z-index:400;min-width:12em;max-width:min(92vw,22em);max-height:min(70vh,30em);overflow:auto;background:var(--card,#0e2233);color:var(--text,#e8f1f8);'+
'border:1px solid var(--border,#23445e);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);padding:.35em;font:inherit;font-size:15px}'+
'#clubpop[hidden]{display:none}'+
'#clubpop button{display:flex;width:100%;align-items:center;gap:.6em;text-align:left;background:none;border:0;color:inherit;font:inherit;padding:.55em .7em;border-radius:8px;cursor:pointer;min-height:44px}'+
'#clubpop button:hover,#clubpop button:focus-visible{background:var(--card2,#12293d);outline:none}'+
'#clubpop button.sel{background:color-mix(in srgb,var(--warning,#fbbf24) 22%,transparent);font-weight:700}'+
'#clubpop .hint{font-size:.8em;color:var(--muted,#9bb2c4);padding:.4em .7em .2em}';

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}

/* ---- markeren: elk tekstje dat exact de clubcode is (of "(CODE)") -> de rij/kaart eromheen */
var ROW='tr, .jrow, .hrow, .mem, #bl2 > span, .bl2 > span, li';
function mark(){
  var old=document.querySelectorAll('.myclub');for(var i=0;i<old.length;i++)old[i].classList.remove('myclub');
  if(!cur)return;
  var want=cur,want2='('+cur+')';
  var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode:function(n){
    var t=n.nodeValue.trim();return (t===want||t===want2)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_SKIP}});
  var n;while((n=w.nextNode())){
    var el=n.parentElement;if(!el||el.closest('#clubpop,#clubbtn,select,option'))continue;
    var row=el.closest(ROW);(row||el).classList.add('myclub');
  }
}
var tmr=null;function later(){clearTimeout(tmr);tmr=setTimeout(mark,120)}
new MutationObserver(function(ms){for(var i=0;i<ms.length;i++)if(ms[i].type==='childList'||ms[i].type==='characterData'){later();return}})
  .observe(document.documentElement,{childList:true,subtree:true,characterData:true});

/* ---- knop + keuzelijst */
var btn,pop,clubs=[];
function label(){if(!btn)return;btn.classList.toggle('on',!!cur);btn.setAttribute('aria-pressed',cur?'true':'false');
  btn.innerHTML='<span aria-hidden="true">&#9873;</span> '+(cur?esc(cur):'Club')}
function loadClubs(cb){fetch('/api/clubs',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){clubs=(d&&d.clubs)||[];cb&&cb()}).catch(function(){cb&&cb()})}
function open(){
  loadClubs(function(){
    var h='<div class="hint">Zwemmers van deze club laten opvallen</div><button type="button" data-c=""'+(cur?'':' class="sel"')+'>Geen</button>';
    clubs.forEach(function(c){h+='<button type="button" data-c="'+esc(c)+'"'+(c===cur?' class="sel"':'')+'>'+esc(c)+'</button>'});
    if(!clubs.length)h+='<div class="hint">Nog geen clubs bekend (geen programma of reeks).</div>';
    pop.innerHTML=h;pop.hidden=false;
    var r=btn.getBoundingClientRect(),pw=Math.min(window.innerWidth*0.92,pop.offsetWidth||220);
    pop.style.top=Math.round(r.bottom+6)+'px';pop.style.left=Math.round(Math.max(8,Math.min(window.innerWidth-pw-8,r.right-pw)))+'px';
    var s=pop.querySelector('.sel');if(s)s.focus();
  });
}
function close(){if(pop)pop.hidden=true}
function set(c){cur=c||'';try{if(cur)localStorage.setItem(KEY,cur);else localStorage.removeItem(KEY)}catch(e){}label();mark();close()}
function init(){
  if(!document.getElementById('clubcss')){var st=document.createElement('style');st.id='clubcss';st.textContent=css;document.head.appendChild(st)}
  var anchor=document.getElementById('themebtn');
  if(anchor&&!document.getElementById('clubbtn')){
    btn=document.createElement('button');btn.type='button';btn.className='tbtn';btn.id='clubbtn';btn.title='Club kiezen om te laten opvallen';
    anchor.parentNode.insertBefore(btn,anchor);label();
    pop=document.createElement('div');pop.id='clubpop';pop.hidden=true;pop.setAttribute('role','menu');document.body.appendChild(pop);
    btn.addEventListener('click',function(e){e.stopPropagation();if(pop.hidden)open();else close()});
    pop.addEventListener('click',function(e){var b=e.target.closest('button[data-c]');if(b)set(b.getAttribute('data-c'))});
    document.addEventListener('click',function(e){if(pop&&!pop.hidden&&!pop.contains(e.target))close()});
    document.addEventListener('keydown',function(e){if(e.key==='Escape')close()});
  }
  mark();
}
/* ---- kleurenpalet volgen (instelling van de server, ook live gewijzigd) */
function palette(){var g=window.__ltRace&&window.__ltRace.getState,S=g&&g(),p=S&&S.settings&&S.settings.palette;
  if(p){var h=document.documentElement,v=/^(scorebord|omega|zwembad)$/.test(p)?p:'';if((h.getAttribute('data-palette')||'')!==v){if(v)h.setAttribute('data-palette',v);else h.removeAttribute('data-palette')}}}
setInterval(palette,2000);
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
window.ClubHL={set:set,refresh:mark};
})();
