/* lichte modus voor trage toestellen (Raspberry Pi): golven één keer tekenen en als geheel verschuiven
   (de grafische chip doet dat), geen overvloeimodus, minder belletjes. ?lite=0/1 om te forceren. */
(function(){
  var q=location.search.match(/[?&]lite=([01])/),ua=navigator.userAgent||'';
  var da=document.documentElement.getAttribute('data-anim');   // gezet door het HDMI-scherm van een kastje
  var lite=q?q[1]==='1':(da==='light'||(navigator.hardwareConcurrency||8)<=2);
  if(!lite)return;
  document.documentElement.classList.add('lite');
  var st=document.createElement('style');
  st.textContent=
   'html.lite #splash .wave,html.lite #infoscreen .is-wave{animation:none!important}'+
   'html.lite #splash svg.wv,html.lite #infoscreen svg.is-wv{width:200%!important;will-change:transform;animation:ltWv 7s linear infinite}'+
   'html.lite #splash svg.wv+svg.wv{animation-duration:5s}'+
   '@keyframes ltWv{from{transform:translateX(0)}to{transform:translateX(-50%)}}'+
   'html.lite #splash .spn{mix-blend-mode:normal!important;opacity:.6}'+
   'html.lite #infoscreen .is-tick{mix-blend-mode:normal!important;opacity:.75}'+
   'html.lite #splash .swm{filter:none!important}'+
   'html.lite #splash .bub:nth-child(n+5),html.lite #infoscreen .is-bub:nth-of-type(n+8){display:none!important}'+
   'html.lite #infoscreen .is-bub{will-change:transform}';
  document.head.appendChild(st);
  // golven zijn periodiek (150 eenheden): over 2400 eenheden tekenen, dan sluit een verschuiving van 50% naadloos aan
  function fixViewBox(){document.querySelectorAll('#splash svg.wv,#infoscreen svg.is-wv').forEach(function(v){
    if(v.getAttribute('viewBox')!=='0 0 2400 600')v.setAttribute('viewBox','0 0 2400 600')})}
  fixViewBox();new MutationObserver(fixViewBox).observe(document.documentElement,{childList:true,subtree:true});
})();
/* HZS Timing – infoscherm (geen wedstrijd, volgende wedstrijd, pauze, …) in de stijl van de splash screens.
   Gebruikt door /info (altijd zichtbaar), de publieke pagina en de oproepkamer (als overlay).
   InfoScreen.init({standalone:true|false, page:'publiek'|'callroom'}) · InfoScreen.apply(info) · InfoScreen.setLive(bool) */
(function(){
'use strict';
var MODES={
  geen:{k:'HZS Timing',t:'Er is nu geen wedstrijd bezig',ic:'🌙'},
  volgende:{k:'Volgende wedstrijd',t:'',ic:''},   // geen kalender-emoji: Android tekent daar een vaste datum (17 juli) in
  welkom:{k:'Fijn dat je er bent',t:'Welkom!',ic:'👋'},
  inzwemmen:{k:'Inzwemmen',t:'Straks begint de wedstrijd',ic:'🏊'},
  pauze:{k:'Pauze',t:'We zijn zo terug',ic:'🥤🧇'},
  prijsuitreiking:{k:'Prijsuitreiking',t:'Applaus voor de winnaars!',ic:'🏅'},
  einde:{k:'Einde van de wedstrijd',t:'Bedankt en tot de volgende keer!',ic:'🏁'},
  bericht:{k:'Mededeling',t:'',ic:'📣'},
  presentatie:{k:'Presentatie',t:'',ic:'🖼'},
  idle:{k:'Live uitslagen',t:'HZS Timing',ic:''}
};
var css=''+
'#infoscreen{position:fixed;inset:0;z-index:250;display:none;overflow:hidden;color:var(--text,#e8f1f8);'+
'background:linear-gradient(180deg,var(--bg,#07131f) 0%,var(--card2,#12293d) 100%);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}'+
'#infoscreen.on{display:block;animation:isIn .5s ease both}@keyframes isIn{from{opacity:0}to{opacity:1}}'+
'#infoscreen .is-top{position:absolute;left:4vw;right:4vw;top:3.5vh;display:flex;align-items:center;justify-content:space-between;gap:3vw;z-index:3}'+
'#infoscreen .is-logo{height:clamp(34px,8vh,96px);width:auto;max-width:62vw;object-fit:contain;object-position:left center;display:block}'+
'#infoscreen .is-clock{font-weight:800;font-size:clamp(20px,5vh,64px);font-variant-numeric:tabular-nums;letter-spacing:.02em;opacity:.92}'+
'#infoscreen .is-main{position:absolute;left:5vw;right:5vw;top:17vh;bottom:36vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;z-index:3}'+
'#infoscreen .is-k{font-weight:800;text-transform:uppercase;letter-spacing:.22em;color:var(--accent,#38bdf8);font-size:clamp(13px,2.6vh,30px)}'+
'#infoscreen .is-ic{font-size:clamp(28px,7vh,90px);line-height:1;margin-bottom:1.2vh;animation:isBob 3s ease-in-out infinite}@keyframes isBob{50%{transform:translateY(-.12em)}}'+
'#infoscreen .is-t{font-weight:850;line-height:1.1;margin:.25em 0 .15em;width:100%;white-space:nowrap;overflow:hidden}'+
'#infoscreen .is-s{color:var(--muted,#9bb2c4);font-weight:600;width:100%}#infoscreen .is-s div{white-space:nowrap;overflow:hidden;line-height:1.3}'+
'#infoscreen .is-cd{margin-top:2.2vh;display:flex;gap:1.4vw;justify-content:center;align-items:flex-end}'+
'#infoscreen .is-cd div{background:color-mix(in srgb,var(--card,#0e2233) 75%,transparent);border:1px solid var(--border,#23445e);border-radius:16px;padding:.5vh 1.6vw;min-width:7vw}'+
'#infoscreen .is-cd b{display:block;font-size:clamp(26px,8vh,110px);font-weight:850;font-variant-numeric:tabular-nums;line-height:1.05}'+
'#infoscreen .is-cd i{display:block;font-style:normal;font-size:clamp(11px,1.8vh,22px);color:var(--muted,#9bb2c4);text-transform:uppercase;letter-spacing:.14em;padding-bottom:.6vh}'+
'#infoscreen .is-cdl{margin-top:1.4vh;font-size:clamp(13px,2.4vh,30px);color:var(--muted,#9bb2c4)}'+
'#infoscreen svg.is-wv{position:absolute;left:0;bottom:0;width:100%;height:42vh;z-index:1}'+
'#infoscreen .is-wave{fill:var(--accent,#38bdf8);animation:isWv linear infinite}'+
'#infoscreen .w1{opacity:.36;animation-duration:6.4s}#infoscreen .w2{opacity:.18;animation-duration:9.2s;animation-direction:reverse}#infoscreen .w3{opacity:.5;animation-duration:4.8s}'+
'@keyframes isWv{to{transform:translateX(-1200px)}}'+
'#infoscreen .is-bub{position:absolute;bottom:-30px;border:2px solid var(--accent,#38bdf8);border-radius:50%;opacity:0;z-index:2;animation:isBub linear infinite}'+
'@keyframes isBub{0%{transform:translate(0,0);opacity:0}10%{opacity:.75}50%{transform:translate(var(--dx,12px),-28vh)}90%{opacity:.4}100%{transform:translate(0,-56vh);opacity:0}}'+
'#infoscreen .is-tick{position:absolute;left:0;right:0;bottom:12vh;overflow:hidden;white-space:nowrap;z-index:3;color:#fff;font-weight:700;font-size:clamp(16px,3.4vh,44px);mix-blend-mode:soft-light;opacity:.9}'+
'#infoscreen .is-tick span{display:inline-block;padding-left:100%;animation:isTick var(--td,30s) linear infinite}'+
'#infoscreen .is-tick span i{font-style:normal;margin:0 1.4em;opacity:.7}'+
'@keyframes isTick{to{transform:translateX(-100%)}}'+
'#infoscreen .is-foot{position:absolute;left:0;right:0;bottom:3vh;text-align:center;z-index:3;font-size:clamp(13px,2.2vh,26px);color:#fff;font-weight:700;letter-spacing:.04em;text-shadow:0 1px 6px rgba(0,0,0,.35)}'+
'#infoscreen .is-close{position:absolute;right:4vw;top:calc(3.5vh + clamp(34px,8vh,96px) + 12px);z-index:4;background:rgba(0,0,0,.25);color:#fff;border:1px solid rgba(255,255,255,.35);border-radius:999px;padding:6px 14px;font:inherit;font-size:13px;cursor:pointer}'+
'#infoscreen.standalone .is-close{display:none}'+
'#infoscreen .is-pres{position:absolute;inset:0;z-index:5;background:#000;display:none}#infoscreen.pres .is-pres{display:block}'+
'#infoscreen .is-pres img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;opacity:0;transition:opacity .9s ease}'+
'#infoscreen .is-pres img.show{opacity:1}'+
'#infoscreen.pres .is-tick{z-index:6;bottom:0;padding:.6vh 0;background:rgba(0,0,0,.62);mix-blend-mode:normal;opacity:1}'+
'#infoscreen.pres .is-foot{display:none}#infoscreen.pres .is-close{z-index:7}#infoscreen.pres.tk .is-pres{bottom:calc(clamp(16px,3.4vh,44px)*1.25 + 1.2vh)}'+
'@media (orientation:portrait){#infoscreen .is-main{top:14vh;bottom:40vh}#infoscreen .is-cd b{font-size:clamp(24px,6vh,80px)}}'+
'@media (prefers-reduced-motion:reduce){#infoscreen .is-wave,#infoscreen .is-bub,#infoscreen .is-ic{animation:none}#infoscreen .is-tick span{animation:none;padding-left:0}}';

var el=null,info={mode:'off'},opts={standalone:false,page:'publiek'},live=false,connected=false,dismissed=null,tmr=null,eff=null;
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function wp(y,a){var d='M0 '+y;for(var x=0;x<2400;x+=150)d+=' q 37.5 '+(-a)+' 75 0 t 75 0';return d+' V600 H0Z'}
function build(){
  if(el)return;
  var st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
  el=document.createElement('div');el.id='infoscreen';el.setAttribute('role','status');
  var bubs='';for(var i=0;i<18;i++){var s=(6+Math.random()*22).toFixed(0);bubs+='<span class="is-bub" style="left:'+(2+Math.random()*96).toFixed(1)+'%;width:'+s+'px;height:'+s+'px;--dx:'+((Math.random()-.5)*40).toFixed(0)+'px;animation-duration:'+(5+Math.random()*7).toFixed(1)+'s;animation-delay:-'+(Math.random()*10).toFixed(1)+'s"></span>'}
  el.innerHTML='<svg class="is-wv" viewBox="0 0 1200 600" preserveAspectRatio="none" aria-hidden="true"><path class="is-wave w2" d="'+wp(150,14)+'"/><path class="is-wave w1" d="'+wp(175,12)+'"/><path class="is-wave w3" d="'+wp(205,10)+'"/></svg>'+bubs+
    '<div class="is-top"><img class="is-logo" src="/img/hzs-wordmark.png" alt="HZS Timing"><div class="is-clock"></div></div>'+
    '<div class="is-main"><div class="is-ic"></div><div class="is-k"></div><div class="is-t"></div><div class="is-s"></div><div class="is-cd"></div><div class="is-cdl"></div></div>'+
    '<div class="is-pres"><img alt=""><img alt=""></div><div class="is-tick"><span></span></div><div class="is-foot"></div><button class="is-close" type="button">Live uitslagen bekijken</button>';
  document.body.appendChild(el);
  if(opts.standalone)el.classList.add('standalone');
  el.querySelector('.is-close').onclick=function(){dismissed=info.updatedAt||1;render()};
  tick();setInterval(tick,1000);
  setInterval(function(){if(info&&info.mode==='auto')render()},60000);     // dagwissel / start bereikt
}
function q(s){return el.querySelector(s)}
function ymd(d){return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2)}
/* modus "auto": uit de agenda afleiden wat er nu getoond wordt */
function effective(){
  if(!info||info.mode!=='auto')return info||{mode:'off'};
  var today=ymd(new Date()),now=new Date(),list=(info.schedule||[]).filter(function(m){return m.date>=today});
  var base={kicker:info.kicker,footer:info.footer,ticker:info.ticker,autoHide:info.autoHide,allowClose:info.allowClose,pages:info.pages,updatedAt:info.updatedAt};
  var nx=list[0];
  if(!nx)return Object.assign(base,{mode:'geen',text:info.text});
  var start=nx.start?new Date(nx.date+'T'+('0'+nx.start).slice(-5)+':00'):null;
  var days=Math.round((new Date(nx.date+'T12:00:00')-new Date(today+'T12:00:00'))/864e5);
  var o={meet:nx.name,date:nx.date,place:nx.place,until:nx.start||''};
  if(days===0&&(!start||now<start))return Object.assign(base,o,{mode:'welkom',text:info.text});
  if(days===0)return Object.assign(base,o,{mode:'geen',text:'Vandaag: '+(nx.name||'wedstrijd')+(nx.place?' · '+nx.place:'')+(list[1]?'\nDaarna: '+(list[1].name||'')+' – '+fmtDate(list[1].date):'')});
  if(days<=(info.daysBefore==null?7:info.daysBefore))return Object.assign(base,o,{mode:'volgende'});
  return Object.assign(base,o,{mode:'geen',until:'',text:info.text});
}
function target(){
  var info=eff||effective();
  if(info.untilAt&&!info.until)return new Date(info.untilAt*1000);
  var u=(info.until||'').trim(),d=(info.date||'').trim(),t=null,m;
  if((m=/^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})$/.exec(u)))t=new Date(m[1]+'T'+('0'+m[2]).slice(-2)+':'+m[3]+':00');
  else if((m=/^(\d{1,2})[:.h](\d{2})$/.exec(u))){
    if(/^\d{4}-\d{2}-\d{2}$/.test(d))t=new Date(d+'T'+('0'+m[1]).slice(-2)+':'+m[2]+':00');
    else{t=new Date();t.setHours(+m[1],+m[2],0,0);if(t-Date.now()<-12*3600e3)t.setDate(t.getDate()+1)}}
  else if(/^\d{4}-\d{2}-\d{2}$/.test(d)&&(info.mode==='volgende'||info.mode==='welkom'))t=new Date(d+'T00:00:00');
  return t&&!isNaN(t)?t:null;
}
function fmtDate(d){if(!/^\d{4}-\d{2}-\d{2}$/.test(d||''))return d||'';var x=new Date(d+'T12:00:00');return x.toLocaleDateString('nl-BE',{weekday:'long',day:'numeric',month:'long',year:'numeric'})}
function hhmm(t){return t.toLocaleTimeString('nl-BE',{hour:'2-digit',minute:'2-digit'})}
function tick(){
  if(!el)return;
  q('.is-clock').textContent=new Date().toLocaleTimeString('nl-BE',{hour:'2-digit',minute:'2-digit'});
  var info=eff||effective();
  var cd=q('.is-cd'),cdl=q('.is-cdl'),t=target();
  if(!t||!el.classList.contains('on')||info.mode==='prijsuitreiking'||info.mode==='einde'||info.mode==='geen'){cd.innerHTML='';cdl.textContent='';return}
  var s=Math.round((t-Date.now())/1000),parts;
  if(s<=0){cd.innerHTML='';cdl.textContent=info.mode==='pauze'?'We hervatten zo dadelijk':(info.mode==='volgende'?'Vandaag!':'Zo dadelijk');return}
  var dd=Math.floor(s/86400),hh=Math.floor(s%86400/3600),mm=Math.floor(s%3600/60),ss=s%60;
  parts=dd?[[dd,dd===1?'dag':'dagen'],[hh,'uur'],[mm,'min']]:(hh?[[hh,'uur'],[('0'+mm).slice(-2),'min'],[('0'+ss).slice(-2),'sec']]:[[mm,'min'],[('0'+ss).slice(-2),'sec']]);
  cd.innerHTML=parts.map(function(p){return '<div><b>'+p[0]+'</b><i>'+p[1]+'</i></div>'}).join('');
  cdl.textContent={pauze:'We hervatten om '+hhmm(t),inzwemmen:'De wedstrijd start om '+hhmm(t),welkom:'Start om '+hhmm(t),volgende:/\d[:.h]\d/.test(info.until||'')?'Start om '+hhmm(t):'',geen:'',bericht:'Om '+hhmm(t)}[info.mode]||'';
}
function visible(){
  if(opts.standalone)return true;
  if(!info||!info.mode||info.mode==='off')return false;
  if(info.mode==='auto'&&connected)return false;          // automatisch: enkel als er geen live gegevens zijn
  var pages=info.pages||{publiek:true,callroom:true};
  if(pages[opts.page]===false)return false;
  if(info.autoHide!==false&&live)return false;
  if(dismissed&&dismissed===(info.updatedAt||1))return false;
  return true;
}
function render(){
  build();
  var full=info;eff=effective();
  var on=visible();el.classList.toggle('on',on);
  document.documentElement.classList.toggle('infoscreen-on',on);
  presRun(on?eff:{});
  if(!on)return;
  var info=eff;
  var mode=(info&&info.mode&&info.mode!=='off')?info.mode:'idle',M=MODES[mode]||MODES.bericht;
  var title=info.title||(mode==='volgende'?(info.meet||'Volgende wedstrijd'):(mode==='welkom'&&info.meet?'Welkom op '+info.meet:M.t));
  var sub=info.text||'';
  if(!sub){
    if(mode==='geen')sub=info.meet?('Volgende wedstrijd: '+info.meet+(info.date?' – '+fmtDate(info.date):'')+(info.place?' · '+info.place:'')):'Tijdens de volgende wedstrijd verschijnen hier de live uitslagen.';
    else if(mode==='volgende')sub=[fmtDate(info.date),info.place].filter(Boolean).join(' · ');
    else if(mode==='einde')sub=info.meet||'';
    else if(mode==='idle')sub='Tijdens een wedstrijd verschijnen hier de live uitslagen.';
  }
  q('.is-ic').textContent=M.ic||'';q('.is-ic').style.display=M.ic?'':'none';
  q('.is-k').textContent=info.kicker||M.k;q('.is-t').textContent=title;
  // één regel per onderdeel (geen afgebroken namen); te lang = kleiner, niet afbreken
  var lines=[];String(sub||'').split(String.fromCharCode(10)).forEach(function(l){l.split(' – ').forEach(function(x){x=x.replace(/^\s*·\s*/,'').trim();if(x)lines.push(x)})});
  q('.is-s').innerHTML=lines.map(function(l){return '<div>'+esc(l)+'</div>'}).join('');
  fitAll();
  var lines=String(info.ticker||'').split(/\n+/).map(function(x){return x.trim()}).filter(Boolean),tk=q('.is-tick span');
  tk.innerHTML=lines.map(esc).join('<i>·</i>');q('.is-tick').style.display=lines.length?'':'none';el.classList.toggle('tk',lines.length>0);
  tk.style.setProperty('--td',Math.max(18,lines.join(' ').length*0.32)+'s');
  var ft=(info.footer||'').trim();q('.is-foot').textContent=ft==='-'?'':(ft||('Live uitslagen: '+location.host));
  q('.is-close').style.display=(opts.standalone||!info.allowClose)?'none':'';
  tick();
}
/* presentatie: dia's na elkaar, overvloeien (op een Pi 3 zonder animaties gewoon wisselen); volgende dia vooraf laden */
var pres={key:'',n:0,t:null,cur:0};
function slideUrl(id,n){return '/slides/'+id+'/'+('00'+(n+1)).slice(-3)+'.jpg'}
function presStop(){clearTimeout(pres.t);pres.t=null;pres.key=''}
function presRun(i){
  var on=el.classList.contains('on')&&i.mode==='presentatie'&&i.deck&&i.deckCount>0;
  el.classList.toggle('pres',!!on);
  if(!on){presStop();return}
  var key=i.deck+'|'+i.deckCount+'|'+i.slideSec;
  if(key===pres.key)return;
  presStop();pres.key=key;pres.n=-1;
  var imgs=el.querySelectorAll('.is-pres img');
  function next(){
    if(pres.key!==key)return;
    pres.n=(pres.n+1)%i.deckCount;
    var nx=imgs[1-pres.cur],cur=imgs[pres.cur],n=pres.n;
    nx.onload=function(){if(pres.key!==key)return;nx.classList.add('show');cur.classList.remove('show');pres.cur=1-pres.cur;
      if(i.deckCount>1){var pre=new Image();pre.src=slideUrl(i.deck,(n+1)%i.deckCount);pres.t=setTimeout(next,Math.max(3,i.slideSec||10)*1000)}};
    nx.onerror=function(){if(pres.key===key)pres.t=setTimeout(next,5000)};
    nx.src=slideUrl(i.deck,n);
  }
  next();
}
/* tekst passend maken: zo groot als mag volgens de schermhoogte, kleiner tot de regel in de breedte past */
function fit(el,max,min){if(!el)return max;var f=max;el.style.fontSize=f+'px';
  while(el.scrollWidth>el.clientWidth+1&&f>min){f=Math.max(min,f*0.94);el.style.fontSize=f+'px'}return f}
function fitAll(){if(!el||!el.classList.contains('on'))return;var H=innerHeight,P=H>innerWidth;
  fit(q('.is-t'),Math.max(26,Math.min(150,H*(P?0.06:0.095))),14);
  var subs=el.querySelectorAll('.is-s div'),mx=Math.max(16,Math.min(56,H*(P?0.03:0.042))),f=mx;
  subs.forEach(function(d){f=Math.min(f,fit(d,mx,11))});subs.forEach(function(d){d.style.fontSize=f+'px'})}
window.addEventListener('resize',function(){clearTimeout(fitAll._t);fitAll._t=setTimeout(fitAll,120)});
window.InfoScreen={
  init:function(o){for(var k in o)opts[k]=o[k];
    var pv=/[?&]preview=([^&]*)/.exec(location.search);
    if(pv){try{info=JSON.parse(decodeURIComponent(pv[1]))}catch(e){}render();return}
    fetch('/api/info',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){info=d||{mode:'off'};render()}).catch(function(){if(opts.standalone)render()});
    if(opts.standalone)render();},
  apply:function(d){info=d||{mode:'off'};
    // onbekende modus = deze pagina draait nog een oud script (na een update): één keer herladen
    if(info.mode&&!MODES[info.mode]&&info.mode!=='off'&&info.mode!=='auto'){var k='lt_is_reload',t=0;
      try{t=+sessionStorage.getItem(k)||0}catch(e){}
      if(Date.now()-t>120000){try{sessionStorage.setItem(k,String(Date.now()))}catch(e){}location.reload();return}}
    render()},
  setLive:function(b){b=!!b;if(b!==live){live=b;if(el)render()}},
  setConnected:function(b){b=!!b;if(b!==connected){connected=b;if(el)render()}}
};
})();
