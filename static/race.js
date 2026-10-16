/* HZS Timing – race-weergave: onder elke rij van de live tabel een balkje met een stip die heen en weer gaat,
   GESCHAT uit de inschrijftijd (geleerde tempoverdeling) en bijgestuurd met elke live tussentijd. Nooit voorbij een
   muur waar nog een tussentijd moet komen; aan de keerkant zonder paneel keert hij gewoon.
   RaceView.mount(tabelcontainer, {getState, getClock, page}) – aan/uit per pagina via race_view_<page>. */
(function(){
'use strict';
var css=''+
'.rvtoggle.on,#rvbtn.on{background:var(--accent,#38bdf8);color:var(--onacc,#06202f);font-weight:700}'+
'@media (min-width:700px) and (min-height:500px){.rvon tr:not(.memrow):not(.empty):not(.free)>td{padding-bottom:.95em!important}}'+
'.rvb{position:absolute;left:0;bottom:.1em;height:.8em;pointer-events:none;z-index:2;overflow:hidden}'+
'.rvb .rvt{position:absolute;left:.35em;right:.35em;top:50%;height:.3em;margin-top:-.15em;border-radius:999px;'+
'background:color-mix(in srgb,var(--accent,#38bdf8) 16%,transparent);box-shadow:-.35em 0 0 -.05em color-mix(in srgb,var(--accent,#38bdf8) 45%,transparent),.35em 0 0 -.05em color-mix(in srgb,var(--accent,#38bdf8) 45%,transparent)}'+
'.rvb .rvd{position:absolute;top:50%;left:0;width:.7em;height:.7em;margin:-.35em 0 0 -.35em;border-radius:50%;background:var(--accent,#38bdf8);'+
'box-shadow:0 0 0 .12em rgba(0,0,0,.35);will-change:left}'+
'.rvb .rvd.fin{background:var(--success,#22c55e)}.rvb .rvd.p1{background:var(--gold,#facc15)}.rvb .rvd.p2{background:var(--silver,#cbd5e1)}.rvb .rvd.p3{background:var(--bronze,#d97706)}'+
'.rvb .rvx{position:absolute;top:50%;margin-top:-.6em;font-size:.68em;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.1;padding:0 .3em;border-radius:4px;background:transparent;text-shadow:0 0 3px var(--bg,#000),0 0 2px var(--bg,#000);white-space:nowrap}'+
'tr.hasmem .rvb.card{bottom:.25em}'+
'.rvb.card{left:.55em!important;right:.55em;width:auto!important;bottom:.1em;height:.8em;overflow:visible}'+
'.rvb .rvx.v{color:var(--success,#22c55e)}.rvb .rvx.a{color:var(--danger,#ef4444)}'+
'.rvb .rvd.hit{animation:rvHit .9s ease-out}@keyframes rvHit{0%{box-shadow:0 0 0 .12em var(--card,#0e2233),0 0 0 .12em var(--accent,#38bdf8)}100%{box-shadow:0 0 0 .12em var(--card,#0e2233),0 0 0 .9em transparent}}';

/* aandeel van elke slag in een wisselslag (individueel: vlinder-rug-school-vrij; aflossing: rug-school-vlinder-vrij) */
var IMW={vlinder:0.22,rug:0.25,school:0.29,vrij:0.24};
var SPM={vrij:0.85,rug:0.98,school:1.10,vlinder:0.95,wissel:0.98};   // s per meter als niets bekend is (jeugd)

function strokeOf(disc){disc=(disc||'').toLowerCase();
  if(disc.indexOf('wissel')>=0)return 'wissel';if(disc.indexOf('vlinder')>=0)return 'vlinder';
  if(disc.indexOf('school')>=0)return 'school';if(disc.indexOf('rug')>=0)return 'rug';return 'vrij'}
function legsOf(disc){var m=/(\d+)\s*[x×]\s*\d+/i.exec(disc||'');return m?+m[1]:1}

/* standaard voor wisselslag per niveau (trager = zwaardere schoolslag); wordt vervangen door wat geleerd is */
var IMB={A:{vlinder:0.22,rug:0.25,school:0.29,vrij:0.24},B:{vlinder:0.225,rug:0.25,school:0.295,vrij:0.23},C:{vlinder:0.23,rug:0.245,school:0.31,vrij:0.215}};
var PROF={data:null,base:null,at:0};
function loadProfiles(){var now=Date.now();if(now-PROF.at<60000)return;PROF.at=now;
  fetch('/api/race-profiles',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){if(d&&d.ok){PROF.data=d.profiles||{};PROF.base=d.base100}}).catch(function(){})}
function band(stroke,D,secs){var b=(PROF.base&&PROF.base[stroke])||{vrij:60,rug:67,school:75,vlinder:66,wissel:70}[stroke];
  if(!secs||!D)return 'B';var r=secs/(D/100)/b;return r<1.25?'A':(r<1.5?'B':'C')}
/* geleerde verdeling per tussentijdstuk -> gewicht per baanlengte */
function learned(n,stroke,legs,D,laps,bd){
  if(!PROF.data||!laps)return null;
  var p=PROF.data[stroke+'|'+legs+'|'+D+'|'+laps+'|'+bd]||PROF.data[stroke+'|'+legs+'|'+D+'|'+laps+'|B'];
  if(!p||p.f.length!==laps||n%laps)return null;
  var per=n/laps,w=[];
  for(var s=0;s<laps;s++){var sub=[];for(var j=0;j<per;j++)sub.push(s===0&&j===0?0.93:1);var t=sub.reduce(function(a,b){return a+b},0);
    sub.forEach(function(x){w.push(p.f[s]*x/t)})}
  return w;
}
/* gewicht per baanlengte (genormaliseerd naar som 1) */
function weights(n,stroke,legs,bd){
  var IMW=IMB[bd||'B'];
  var w=[],per=n/legs;
  for(var i=0;i<n;i++){
    var leg=Math.floor(i/per),inLeg=i-leg*per,f=1;
    if(stroke==='wissel'){
      var order=legs>1?['rug','school','vlinder','vrij']:['vlinder','rug','school','vrij'];
      var q=legs>1?order[leg%4]:order[Math.min(3,Math.floor(inLeg*4/per))];
      f=IMW[q]*4;
    }
    if(inLeg===0)f*=(leg===0?0.93:0.97);          // duik / vliegende wissel
    else f*=1+0.012*inLeg/Math.max(1,per);         // licht verval
    w.push(f);
  }
  var t=w.reduce(function(a,b){return a+b},0);return w.map(function(x){return x/t});
}
function cum(w){var c=[0];for(var i=0;i<w.length;i++)c.push(c[i]+w[i]);return c}
/* W(x): fractie van de tijd tot afstand x (in lengtes); en de inverse */
function Wx(c,x){var k=Math.min(c.length-2,Math.max(0,Math.floor(x)));return c[k]+(c[k+1]-c[k])*(x-k)}
function Winv(c,f){if(f<=0)return 0;for(var k=0;k<c.length-1;k++)if(f<=c[k+1])return k+(f-c[k])/((c[k+1]-c[k])||1);return c.length-1}

var views=[];
function mount(host,o){
  if(!host)return null;
  if(!document.getElementById('rvcss')){var st=document.createElement('style');st.id='rvcss';st.textContent=css;document.head.appendChild(st)}
  var v={host:host,o:o,key:'',bars:{},maxX:{},entries:{},entKey:'',lastFetch:0};
  views.push(v);return v;
}
function opt(S,k,d){var x=S&&S.settings&&S.settings[k];return x==null?d:x}
function pref(page){try{return localStorage.getItem('lt_race_'+page)}catch(e){return null}}
function enabled(v,S){var p=pref(v.o.page);if(p==='on')return true;if(p==='off')return false;return opt(S,'race_view_'+v.o.page,false)===true}
function toggle(){views.forEach(function(v){var S=v.o.getState(),now=enabled(v,S),def=opt(S,'race_view_'+v.o.page,false)===true;
  try{if(!now===def)localStorage.removeItem('lt_race_'+v.o.page);else localStorage.setItem('lt_race_'+v.o.page,now?'off':'on')}catch(e){}});btnState();loop()}
function btnState(){if(!views[0])return;var on=enabled(views[0],views[0].o.getState());
  Array.prototype.forEach.call(document.querySelectorAll('#rvbtn,.rvtoggle'),function(b){
    b.setAttribute('aria-pressed',on?'true':'false');b.classList.toggle('on',on);b.title='Race-weergave '+(on?'uitzetten':'aanzetten')+' (toets R) – geschatte positie per baan'})}
document.addEventListener('click',function(e){if(e.target&&e.target.closest&&e.target.closest('#rvbtn,.rvtoggle'))toggle()});
/* in de bovenbalk van de publieke pagina (de werkbalk met de Race-knop is daar verborgen) */
setTimeout(function(){var tb=document.querySelector('body.pub .tbr, .tbr'),th=document.getElementById('themebtn');
  if(document.body.classList.contains('pub')&&th&&!document.querySelector('.rvtoggle')){
    var b=document.createElement('button');b.type='button';b.className='tbtn rvtoggle';b.innerHTML='<span aria-hidden="true">&#127946;</span><span class="rvtl"> Race</span>';
    b.setAttribute('aria-label','Race-weergave');th.parentNode.insertBefore(b,th);btnState()}},500);
document.addEventListener('keydown',function(e){if((e.key==='r'||e.key==='R')&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!/INPUT|TEXTAREA|SELECT/.test((e.target&&e.target.tagName)||''))toggle()});
setInterval(btnState,1000);

function fetchEntries(v,key){
  var now=Date.now();if(v.entKey===key&&now-v.lastFetch<15000)return;v.lastFetch=now;
  fetch('/api/callroom',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    if(d&&d.liveEntries&&d.liveEntries.key===key){v.entries=d.liveEntries.lanes||{};v.entKey=key}
  }).catch(function(){});
}
function hkey(S){var m=String((S.heat&&S.heat.name)||'').match(/(\d+)\s*$/);return (S.event&&S.event.number)+'/'+(m?m[1]:(S.heat&&S.heat.number))}
function nm(l){var n=((l.firstName||'')+' '+(l.lastName||'')).trim();return n||l.team||l.relayName||''}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}

function update(v){
  var S=v.o.getState(),clk=v.o.getClock();
  var on=S&&enabled(v,S)&&S.showing!=='previous'&&S.lanes&&S.lanes.length&&S.heat&&S.heat.distance;
  v.host.classList.toggle('rvon',!!on);
  if(!on){clearBars(v);return}
  var key=hkey(S);
  if(key!==v.key){v.key=key;v.maxX={};clearBars(v);v.entries={};v.entKey=''}
  fetchEntries(v,key);
  var L=+opt(S,'race_pool_length',25)||25,D=S.heat.distance,n=Math.max(1,Math.round(D/L));
  var disc=(S.event&&S.event.discipline)||'',stroke=strokeOf(disc),legs=legsOf(disc);
  loadProfiles();
  var laps=(S.heat&&S.heat.laps)||0,splitD=[];if(laps>0)for(var i=1;i<=laps;i++)splitD.push(D*i/laps);
  var cache={};
  function curve(bd){if(!cache[bd])cache[bd]=cum(learned(n,stroke,legs,D,laps,bd)||weights(n,stroke,legs,bd));return cache[bd]}
  var st=clk.status,t=null;
  if(st==='running'&&clk.rt!=null)t=(clk.rt+Math.min(30000,performance.now()-clk.at)*10)/10000;
  else if(clk.rt!=null&&st!=='ready')t=clk.rt/10000;
  var lanes=S.lanes.filter(function(l){return nm(l)&&!/^(DNS|DSQ|NS)$/i.test(l.status||'')});
  // tempo zonder gegevens: mediaan van de bekende inschrijftijden, anders een vaste schatting
  var known=lanes.map(function(l){var e=v.entries[l.lane];return e?e/10000:null}).filter(Boolean).sort(function(a,b){return a-b});
  var dflt=known.length?known[Math.floor(known.length/2)]:D*SPM[stroke]*1.0;
  var res=lanes.map(function(l){
    var x,eS=v.entries[l.lane]?v.entries[l.lane]/10000:null,c=curve(band(stroke,D,eS||dflt));
    var delta=null;
    if(l.final!=null){x=n;if(v.entries[l.lane])delta=l.final/10000-v.entries[l.lane]/10000}
    else if(t==null||st==='ready'||st==='idle'&&clk.rt==null){x=0}
    else{
      var sp=(l.splits||[]).filter(function(s){return s.time!=null&&s.distance>0}).sort(function(a,b){return a.distance-b.distance});
      var last=sp.length?sp[sp.length-1]:null,T;
      var ent=v.entries[l.lane]?v.entries[l.lane]/10000:dflt;
      if(last){var ts=last.time/10000;T=ts/Math.max(0.0001,Wx(c,last.distance/L))}       // tempo van vandaag
      else T=ent;
      if(last&&v.entries[l.lane])delta=ts-ent*Wx(c,last.distance/L);                    // t.o.v. schema van de inschrijftijd
      x=Winv(c,Math.min(1,t/T))*1;
      // niet voorbij de volgende muur met een verwachte tussentijd
      var nextD=null;for(var j=0;j<splitD.length;j++)if(splitD[j]>(last?last.distance:0)+0.01){nextD=splitD[j];break}
      if(nextD!=null)x=Math.min(x,nextD/L-0.04);
      if(last)x=Math.max(x,last.distance/L);
      x=Math.min(x,n-0.02);
    }
    x=Math.max(x,v.maxX[l.lane]||0);v.maxX[l.lane]=x;          // nooit achteruit
    return {l:l,x:x,delta:delta,ns:(l.splits||[]).length+(l.final!=null?1:0)};
  });
  // balkje onder de rij van elke baan in de live tabel (de tabel wordt telkens opnieuw opgebouwd: telkens terugplaatsen)
  var rowOf={};
  Array.prototype.forEach.call(v.host.querySelectorAll('tr'),function(tr){
    if(tr.classList.contains('empty')||tr.classList.contains('memrow'))return;
    var td=tr.querySelector('td.lane');if(!td)return;var ln=parseInt(td.textContent,10);if(!isNaN(ln))rowOf[ln]=td});
  var seen={},added=false;
  res.forEach(function(r){
    var td=rowOf[r.l.lane];if(!td)return;seen[r.l.lane]=1;
    var tr=td.parentNode,card=getComputedStyle(tr).display==='grid';   // smartphone: elke baan is een kaart
    if(card){if(getComputedStyle(tr).position==='static')tr.style.position='relative';td=tr.querySelector('td.rank')||td}   // over de onderrand van de kaart: geen extra hoogte
    var b=v.bars[r.l.lane];
    if(!b||!b.el.isConnected||b.el.parentNode!==td){
      if(b&&b.el.isConnected)b.el.remove();
      if(!card&&getComputedStyle(td).position==='static')td.style.position='relative';
      if(card)td.style.position='static';
      var el=document.createElement('div');el.className='rvb'+(card?' card':'');el.title='Geschatte positie (inschrijftijd en tussentijden)';
      el.innerHTML='<div class="rvt"></div><div class="rvd"></div><div class="rvx"></div>';td.appendChild(el);
      b=v.bars[r.l.lane]={el:el,d:el.querySelector('.rvd'),x:el.querySelector('.rvx'),ns:b?b.ns:null,w:0};
    }
    if(!card){var w=tr.getBoundingClientRect().width-2,off=td.getBoundingClientRect().left-tr.getBoundingClientRect().left;
      if(Math.abs(w-b.w)>0.5){b.w=w;b.el.style.width=w+'px';b.el.style.left=(-off+1)+'px'}}
    var k=Math.min(n-1,Math.floor(r.x)),f=r.x-k;if(r.x>=n){k=n-1;f=1}
    var pos=(k%2===0)?f:1-f;                                     // heen en terug per baanlengte
    var left='calc(.35em + (100% - .7em) * '+pos.toFixed(4)+')';if(b.d.style.left!==left)b.d.style.left=left;
    var xl=pos>0.8?'calc(.35em + (100% - .7em) * '+pos.toFixed(4)+' - 4.6em)':'calc(.35em + (100% - .7em) * '+pos.toFixed(4)+' + .9em)';   // verschil naast de stip
    if(b.x.style.left!==xl)b.x.style.left=xl;
    var fin=r.l.final!=null,cls='rvd'+(b.d.classList.contains('hit')?' hit':'')+(fin?(r.l.rank>=1&&r.l.rank<=3?' p'+r.l.rank:' fin'):'');
    if(b.d.className!==cls)b.d.className=cls;
    if(b.ns!=null&&r.ns>b.ns){b.d.classList.remove('hit');void b.d.offsetWidth;b.d.classList.add('hit')}   // tikking: kort oplichten
    b.ns=r.ns;
    var dt=r.delta==null?'':(r.delta<=0?'−':'+')+Math.abs(r.delta).toFixed(1).replace('.',',');
    if(b.x.textContent!==dt){b.x.textContent=dt;b.x.className='rvx'+(r.delta==null?'':(r.delta<=0?' v':' a'));b.x.title='t.o.v. de inschrijftijd'}
  });
  if(added&&v.o.refit&&Date.now()-(v.refitAt||0)>1000){v.refitAt=Date.now();setTimeout(v.o.refit,0)}                  // kaarten werden hoger: opnieuw passend schalen
  Object.keys(v.bars).forEach(function(k){if(!seen[k]){var b=v.bars[k];if(b.el.isConnected)b.el.remove();delete v.bars[k]}});
}
function clearBars(v){Object.keys(v.bars).forEach(function(k){var b=v.bars[k];if(b.el.isConnected)b.el.remove()});v.bars={}}
var lite=function(){var a=document.documentElement.getAttribute('data-anim');return a==='off'||a==='light'||document.documentElement.classList.contains('lite')};
function loop(){views.forEach(function(v){try{update(v)}catch(e){}})}
(function tick(){loop();if(lite())setTimeout(tick,250);else requestAnimationFrame(tick)})();
window.RaceView={mount:mount,refresh:loop};
})();
