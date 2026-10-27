/* AgenticML local dashboard. All store strings enter the DOM as text. */
const app = document.querySelector('#app');
const token = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const hash = s => [...s].reduce((h, c) => ((h << 5) - h + c.charCodeAt(0)) | 0, 0);
const color = id => token(`--s${Math.abs(hash(id)) % 5 + 1}`);
const fmt = v => typeof v === 'number' ? Number(v.toPrecision(5)).toLocaleString() : v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
const date = v => v ? new Date(v).toLocaleString() : '—';
const routeRun = id => `#/run/${encodeURIComponent(id)}`;
const apiRun = id => `/api/runs/${encodeURIComponent(id)}`;
const compareURL = ids => `#/compare?${new URLSearchParams({runs: ids.join(',')})}`;
const objectURL = (sha, type) => `/api/objects/${encodeURIComponent(sha)}?${new URLSearchParams({type})}`;
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key in node && !key.startsWith('aria')) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter(x => x != null));
  return node;
}
const button = (text, onclick, cls = '') => el('button', {text, onclick, class: cls});
const badge = status => el('span', {class: `badge ${['running','finished','crashed','stale'].includes(status) ? status : ''}`, text: status || 'unknown'});
let epoch = 0, cleanup = () => {}, controller;
async function get(url, signal = controller?.signal) {
  const response = await fetch(url, {signal});
  if (!response.ok) throw new Error(`${response.status}: ${url.startsWith('/api/objects/') ? 'Object unavailable' : await response.text()}`);
  return response.json();
}
function error(node, err) { if (err.name !== 'AbortError') node.replaceChildren(el('p', {class:'error', text:err.message})); }
function table(headers, rows, cls = '') {
  return el('div', {class:'table-scroll'}, el('table', {class:cls}, el('thead', {}, el('tr', {}, ...headers.map(h => el('th', {text:h})))), el('tbody', {}, ...rows)));
}
const row = cells => el('tr', {}, ...cells.map(v => el('td', {}, v instanceof Node ? v : fmt(v))));
function kv(title, data) {
  const input = el('input', {type:'search', placeholder:`Search ${title.toLowerCase()}`, 'aria-label':`Search ${title}`});
  const rows = Object.entries(data || {}).map(([k,v]) => ({k, v, node:row([k,v])}));
  input.addEventListener('input', () => {for (const r of rows) r.node.hidden = !`${r.k} ${fmt(r.v)}`.toLowerCase().includes(input.value.toLowerCase());});
  return el('section', {class:'card'}, el('h2', {text:title}), el('div', {class:'toolbar'}, input), table(['Key','Value'], rows.map(r=>r.node), 'kv'));
}
let theme = 'system';
try { theme = localStorage.getItem('agenticml-theme') || 'system'; } catch {}
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
function applyTheme() {
  document.documentElement.dataset.theme = theme === 'system' ? systemTheme.matches ? 'dark' : 'light' : theme;
  document.querySelector('#theme').textContent = `Theme: ${theme}`;
  window.dispatchEvent(new Event('dashboard-theme'));
}
document.querySelector('#theme').onclick = () => {theme = ['system','light','dark'][(['system','light','dark'].indexOf(theme)+1)%3]; try {localStorage.setItem('agenticml-theme',theme);} catch {} applyTheme();};
systemTheme.addEventListener('change', applyTheme);
applyTheme();
document.querySelector('#close-lightbox').onclick = () => document.querySelector('#lightbox').close();

async function home(version) {
  const selected = new Set();
  const search = el('input', {type:'search',placeholder:'Filter runs…','aria-label':'Filter runs'});
  const projects = el('select', {'aria-label':'Project filter'});
  const compare = button('Compare selected (0)', () => {location.hash = compareURL([...selected]);}, 'primary');
  compare.disabled = true;
  const content = el('div');
  const status = el('span', {class:'poll-status'});
  app.replaceChildren(el('div',{class:'heading'},el('h1',{text:'Experiments'}),compare), el('div',{class:'toolbar'},search,projects,status),content);
  let runs = [], timer;
  function draw() {
    const visible = runs.filter(r => (!projects.value || r.project === projects.value) && `${r.name} ${r.run_id} ${r.project} ${(Array.isArray(r.tags) ? r.tags : []).join(' ')}`.toLowerCase().includes(search.value.toLowerCase()));
    const rows = visible.map(r => {
      const check = el('input',{type:'checkbox',checked:selected.has(r.run_id),'aria-label':`Select ${r.name || r.run_id}`,onchange:() => {
        check.checked ? selected.add(r.run_id) : selected.delete(r.run_id);
        compare.textContent = `Compare selected (${selected.size})`; compare.disabled = !selected.size;
      }});
      return row([check, el('a',{href:routeRun(r.run_id),text:r.name || r.run_id}),r.project,badge(r.status),date(r.created_at),el('span',{class:'summary',text:Object.entries(r.summary || {}).slice(0,3).map(([k,v])=>`${k}: ${fmt(v)}`).join(' · ')})]);
    });
    content.replaceChildren(visible.length ? el('div',{class:'card'},table(['','Run','Project','Status','Created','Summary'],rows)) : el('p',{class:'empty',text:'No runs found. Start a local experiment to see it here.'}));
  }
  async function refresh() {
    try {
      runs = await get('/api/runs'); if (epoch !== version) return;
      const previous = projects.value;
      projects.replaceChildren(el('option',{value:'',text:'All projects'}), ...[...new Set(runs.map(r=>r.project).filter(Boolean))].sort().map(p=>el('option',{value:p,text:p})));
      projects.value = previous;
      for (const id of selected) if (!runs.some(r=>r.run_id === id)) selected.delete(id);
      compare.textContent = `Compare selected (${selected.size})`; compare.disabled = !selected.size;
      draw(); status.textContent = runs.some(r=>r.status === 'running') ? 'Live · refreshes every 5 s' : `${runs.length} runs`;
      if (runs.some(r=>r.status === 'running')) timer = setTimeout(refresh,5000);
    } catch (err) {if (epoch === version) {error(content,err); timer=setTimeout(refresh,5000);}}
  }
  search.oninput = projects.onchange = draw;
  cleanup = () => clearTimeout(timer);
  await refresh();
}

async function runPage(id, version) {
  const [run, manifest, media, summaries] = await Promise.all([get(apiRun(id)),get(`${apiRun(id)}/code`),get(`${apiRun(id)}/media`),get('/api/runs')]);
  if (epoch !== version) return;
  const status = summaries.find(r=>r.run_id===id)?.status || run.status;
  const start = new Date(run.created_at), finish = run.finished_at ? new Date(run.finished_at) : new Date();
  const git = run.git || {};
  const commit = typeof git.git_commit === 'string' && git.git_commit ? git.git_commit.slice(0, 8) : '—';
  const branch = typeof git.git_branch === 'string' && git.git_branch ? git.git_branch : '—';
  const gitFact = el('span', {text: `Git ${commit} · ${branch}`});
  if (git.is_dirty) gitFact.append(document.createTextNode(' '), el('span', {class: 'badge', text: 'dirty'}));
  const facts = [run.project,status,`Created ${date(run.created_at)}`,`Finished ${date(run.finished_at)}`,`Duration ${fmt(Math.max(0,(finish-start)/1000))} s`,`Host ${run.hostname || '—'}`, gitFact];
  const env = {...(run.env || {})}, packages = env.packages; delete env.packages;
  const envCard = kv('Environment',env);
  if (packages) envCard.append(el('details',{},el('summary',{text:'Packages'}),el('pre',{text:typeof packages==='object' ? JSON.stringify(packages,null,2) : String(packages)})));
  const code = el('pre',{class:'code-view',text:'Choose a snapshot file.'});
  const tree = el('nav',{class:'file-tree','aria-label':'Code files'});
  const directories = new Map([['',tree]]);
  for (const file of [...manifest].sort((a,b)=>a.path.localeCompare(b.path))) {
    const parts = file.path.split('/'); let key='', parent=tree;
    for (const part of parts.slice(0,-1)) {
      key += `${part}/`;
      if (!directories.has(key)) {const d = el('details',{},el('summary',{text:part})); parent.append(d); directories.set(key,d);}
      parent = directories.get(key);
    }
    parent.append(button(parts.at(-1), async () => {
      code.textContent = 'Loading…';
      if (file.size > 1024*1024) {code.textContent = 'Too large: code previews are limited to 1 MB.';return;}
      try {
        const response = await fetch(objectURL(file.sha256,'text/plain'),{signal:controller.signal});
        if (!response.ok) throw new Error('Code snapshot unavailable');
        // Check the bytes too, in case the manifest has an incorrect size.
        const blob = await response.blob();
        if (blob.size > 1024*1024) {code.textContent='Too large: code previews are limited to 1 MB.';return;}
        const text = await blob.text();
        code.replaceChildren(...text.split('\n').map(line=>el('span',{class:'code-line',text:line || ' '})));
      } catch(err) {error(code,err);}
    }));
  }
  const mediaCard = el('section',{class:'card'},el('h2',{text:'Media'}));
  const mediaGroups = media && typeof media === 'object' && !Array.isArray(media) ? media : {};
  for (const [key,items] of Object.entries(mediaGroups)) {
    if (!Array.isArray(items)) continue;
    const strip = el('div',{class:'media-strip'});
    for (const item of [...items].sort((a,b)=>a.step-b.step)) {
      if (!/^image\/(png|jpeg|gif|webp|avif|bmp|x-icon)$/.test(item.content_type)) continue;
      const src = objectURL(item.sha256,item.content_type);
      const img = el('img',{src,loading:'lazy',alt:item.caption || key,tabIndex:0});
      const enlarge = () => {const dialog=document.querySelector('#lightbox');dialog.querySelector('img').src=src;dialog.querySelector('img').alt=item.caption || key;dialog.showModal();};
      img.onclick=enlarge;img.onkeydown=e=>{if(e.key==='Enter')enlarge();};
      strip.append(el('figure',{},img,el('figcaption',{text:`Step ${item.step}${item.caption ? ` · ${item.caption}` : ''}`})));
    }
    mediaCard.append(el('h3',{text:key}),strip);
  }
  const artifacts = table(['Name','Version','Type','Size','Path'],(run.artifacts || []).map(a=>row([a.name,a.version,a.type,a.size,a.copy || a.sha256 ? 'stored' : a.path || a.file_path || 'stored'])));
  app.replaceChildren(el('div',{class:'heading'},el('div',{},el('a',{href:'#/',text:'← Experiments'}),el('h1',{text:run.name || id})),el('a',{href:compareURL([id]),class:'primary',text:'Charts'})),badge(status),el('div',{class:'run-facts'},...facts.map(item => item instanceof Node ? item : el('span',{text:item}))),kv('Config',run.config),kv('Summary',run.summary),envCard,el('section',{class:'card'},el('h2',{text:`Code · ${manifest.length} ${manifest.length===1?'file':'files'}`}),el('div',{class:'code-browser'},tree,code)),mediaCard,el('section',{class:'card'},el('h2',{text:'Artifacts'}),artifacts));
}

// Time indexes are interpolated in both directions; zoom API bounds remain steps.
function interpolate(points, value, from=0, to=1) {
  if (!points.length) return null;
  let lo=0,hi=points.length;
  while(lo<hi){const mid=(lo+hi)>>>1;if(points[mid][from]<value)lo=mid+1;else hi=mid;}
  const a=points[Math.max(0,lo-1)], b=points[Math.min(points.length-1,lo)];
  return a[from]===b[from] ? a[to] : a[to]+(value-a[from])/(b[from]-a[from])*(b[to]-a[to]);
}
export function ema(values,w) {
  let running=0, i=0;
  return values.map(v=>{if(!Number.isFinite(v))return null;running=running*w+(1-w)*v;return running/(1-w**(++i));});
}
async function charts(ids, version) {
  if (!ids.length) {app.replaceChildren(el('p',{class:'empty',text:'Select runs from Experiments to compare.'}));return;}
  const runs = await Promise.all(ids.map(async id=>{const run=await get(apiRun(id));run.run_id=id;return run;}));
  if(epoch!==version)return;
  const shown=new Set(ids), times=new Map(), cache=new Map(), cards=new Map();
  let weight=0, log=false, axis='step', zoom=null, timer, debounce, busy=false, queued=false, generation=0, destroyed=false;
  const message=el('span',{class:'poll-status'});
  const filter=el('input',{type:'search',placeholder:'Filter metric keys…','aria-label':'Filter metric keys'});
  const smoothing=el('input',{type:'range',min:0,max:.99,step:.01,value:0,'aria-label':'EMA smoothing'});
  const smoothingValue=el('span',{text:'0.00'});
  const logInput=el('input',{type:'checkbox','aria-label':'Logarithmic y axis'});
  const axisSelect=el('select',{'aria-label':'X axis'},el('option',{value:'step',text:'Step'}),el('option',{value:'time',text:'Relative time (s)'}));
  const reset=button('Reset zoom',()=>{zoom=null;generation++;schedule();});
  const list=el('div',{class:'run-list'});
  runs.forEach(run=>{
    const id=run.run_id, swatch=el('span',{class:'swatch'});swatch.style.background=color(id);
    list.append(el('label',{},el('input',{type:'checkbox',checked:true,onchange:e=>{e.target.checked?shown.add(id):shown.delete(id);generation++;schedule();}}),swatch,el('a',{href:routeRun(id),text:run.name || id})));
  });
  const sections=el('div');
  app.replaceChildren(el('div',{class:'heading'},el('div',{},el('a',{href:'#/',text:'← Experiments'}),el('h1',{text:ids.length===1 ? runs[0].name || 'Charts' : `Compare ${ids.length} runs`})),message),el('div',{class:'toolbar'},el('label',{},'EMA',smoothing,smoothingValue),el('label',{},logInput,'Log y'),el('label',{},'X axis',axisSelect),reset),el('div',{class:'compare'},el('aside',{class:'sidebar'},el('h2',{text:'Runs'}),list,filter,el('p',{class:'muted',text:'Drag to zoom · double-click to reset. Hover to share the cursor across charts.'})),sections));
  const keys=[...new Set(runs.flatMap(r=>r.keys))].sort();
  const groups=new Map();
  const observer=new IntersectionObserver(entries=>{for(const e of entries){cards.get(e.target.dataset.key).visible=e.isIntersecting;}schedule();},{rootMargin:'0px'});
  const resize=new ResizeObserver(entries=>{for(const entry of entries){const card=cards.get(entry.target.dataset.key);if(card?.plot && entry.contentRect.width>0)card.plot.setSize({width:Math.floor(entry.contentRect.width),height:230});}});
  function addKey(key){
    if(cards.has(key))return;
    sections.querySelector('.empty')?.remove();
    const prefix=key.includes('/')?key.split('/')[0]:'Metrics';
    if(!groups.has(prefix)){
      const grid=el('div',{class:'chart-grid'}), section=el('details',{class:'section',open:prefix!=='_system'},el('summary',{text:prefix}),grid);
      groups.set(prefix,{grid,section});sections.append(section);section.addEventListener('toggle',schedule);
    }
    const group=groups.get(prefix), host=el('div',{class:'chart-host','data-key':key}), tip=el('div',{class:'chart-tip',text:'Scroll into view to load'});
    const node=el('article',{class:'card chart-card','data-key':key},el('h3',{text:key}),host,tip);
    const card={key,node,host,tip,section:group.section,visible:false,plot:null,models:[],appliedZoom:null};
    cards.set(key,card);group.grid.append(node);observer.observe(node);resize.observe(host);
    host.addEventListener('dblclick',()=>{zoom=null;generation++;schedule();});
    node.hidden=!key.toLowerCase().includes(filter.value.toLowerCase());
  }
  keys.forEach(addKey);
  if(!keys.length)sections.append(el('p',{class:'empty',text:'These runs have no scalar metrics yet.'}));
  const active=()=>[...cards.values()].filter(c=>c.visible&&!c.node.hidden&&c.section.open);
  function schedule(){if(destroyed)return;clearTimeout(debounce);debounce=setTimeout(refresh,50);}
  filter.oninput=()=>{for(const card of cards.values())card.node.hidden=!card.key.toLowerCase().includes(filter.value.toLowerCase());for(const {grid,section} of groups.values())section.hidden=![...grid.children].some(n=>!n.hidden);schedule();};
  smoothing.oninput=()=>{weight=+smoothing.value;smoothingValue.textContent=weight.toFixed(2);for(const card of active())draw(card);};
  logInput.onchange=()=>{log=logInput.checked;for(const card of cards.values()){card.plot?.destroy();card.plot=null;}for(const card of active())draw(card);};
  axisSelect.onchange=()=>{axis=axisSelect.value;zoom=null;generation++;schedule();};
  const themeChanged=()=>{for(const card of cards.values()){card.plot?.destroy();card.plot=null;}for(const swatch of list.querySelectorAll('.swatch')){const index=[...list.querySelectorAll('.swatch')].indexOf(swatch);swatch.style.background=color(ids[index]);}for(const card of active())draw(card);};
  window.addEventListener('dashboard-theme',themeChanged);
  cleanup=()=>{destroyed=true;clearTimeout(timer);clearTimeout(debounce);observer.disconnect();resize.disconnect();window.removeEventListener('dashboard-theme',themeChanged);for(const c of cards.values())c.plot?.destroy();};
  function draw(card){
    if(destroyed || !card.visible || !card.section.open)return;
    const models=[];
    for(const run of runs){
      const result=cache.get(run.run_id)?.[card.key];
      const pts=result?.points || [], overview=result?.mode==='overview';
      const values=pts.map(p=>overview?p[4]:p[1]);
      const smoothed=ema(values,weight);
      // Sort for uPlot (store records may contain steps logged out of order).
      const mapped=pts.map((p,i)=>{
        const step=overview?(p[0]+p[1])/2:p[0];
        const x=axis==='step'?step:interpolate(times.get(run.run_id)||[],step);
        const safe=v=>Number.isFinite(v)&&(!log||v>0)?v:null;
        return {x,step,raw:safe(values[i]),mean:safe(smoothed[i]),low:overview?safe(p[2]):null,high:overview?safe(p[3]):null};
      }).filter(p=>Number.isFinite(p.x)).sort((a,b)=>a.x-b.x);
      models.push({run,points:mapped,overview});
    }
    card.models=models;
    const xs=[...new Set(models.flatMap(m=>shown.has(m.run.run_id)?m.points.map(p=>p.x):[]))].sort((a,b)=>a-b);
    const offsets=new Map(xs.map((x,i)=>[x,i]));
    const data=[xs], series=[{}], bands=[];
    models.forEach(m=>{
      const c=color(m.run.run_id), visible=shown.has(m.run.run_id);
      for(const field of ['raw','low','high','mean']){
        const values=Array(xs.length).fill(null);for(const p of m.points){if(offsets.has(p.x))values[offsets.get(p.x)]=p[field];}
        data.push(values);
        series.push({label:`${m.run.name || m.run.run_id} ${field}`,stroke:c,width:field==='mean'?2:field==='raw'?1:0,alpha:field==='raw'?.22:1,show:visible&&(field!=='raw'||weight>0),spanGaps:true,points:{show:false}});
      }
      const mean=series.length-1;
      bands.push({series:[mean-1,mean-2],fill:c+'26'});
    });
    if(!card.plot){
      card.plot=new uPlot({width:Math.max(100,Math.floor(card.host.clientWidth)),height:230,series,bands,legend:{show:false},scales:{x:{time:false},y:{distr:log?3:1}},axes:[{stroke:token('--axis'),grid:{show:false},font:'10px ui-monospace, monospace'},{stroke:token('--axis'),grid:{stroke:token('--grid'),width:1},font:'10px ui-monospace, monospace',size:55}],cursor:{drag:{x:true,y:false,setScale:false},sync:{key:'agenticml-local',scales:['x',null]}},hooks:{setSelect:[u=>{
        if(u.select.width<3)return;
        const a=u.posToVal(u.select.left,'x'),b=u.posToVal(u.select.left+u.select.width,'x');
        zoom=[Math.min(a,b),Math.max(a,b)];generation++;u.setSelect({left:0,top:0,width:0,height:0},false);schedule();
      }],setCursor:[u=>{
        if(u.cursor.left<0){card.tip.textContent='';return;}
        const x=u.posToVal(u.cursor.left,'x');
        const lines=[`${axis==='step'?'Step':'Seconds'} ${fmt(x)}`];
        for(const m of card.models){if(!shown.has(m.run.run_id)||!m.points.length)continue;let lo=0,hi=m.points.length;while(lo<hi){const mid=(lo+hi)>>>1;if(m.points[mid].x<x)lo=mid+1;else hi=mid;}const p=m.points[Math.min(lo,m.points.length-1)],q=m.points[Math.max(0,lo-1)],best=Math.abs(p.x-x)<Math.abs(q.x-x)?p:q;lines.push(`${m.run.name || m.run.run_id}: ${fmt(best.mean)}${m.overview?' (mean)':''}`);}
        card.tip.textContent=lines.join('\n');
      }]}},data,card.host);
    }else{
      series.slice(1).forEach((s,i)=>card.plot.setSeries(i+1,{show:s.show},false));
      card.plot.setData(data,true);
    }
    if(zoom)card.plot.setScale('x',{min:zoom[0],max:zoom[1]});
    card.tip.textContent=models.some(m=>m.overview)?'Overview · shaded min/max, mean line. Drag to inspect raw points.':xs.length?'Hover to inspect values':'No data in this range';
  }
  async function refresh(){
    if(destroyed)return;
    if(busy){queued=true;return;}
    busy=true;clearTimeout(timer);
    const currentGeneration=generation, visible=active(), visibleKeys=visible.map(c=>c.key);
    try{
      if(visibleKeys.length){
        await Promise.all(runs.filter(r=>shown.has(r.run_id)).map(async run=>{
          if(axis==='time')times.set(run.run_id,(await get(`${apiRun(run.run_id)}/time`)).sort((a,b)=>a[0]-b[0]));
          const query=new URLSearchParams({max_points:'1000'});
          for(const key of visibleKeys)query.append('key',key);
          if(zoom){const index=times.get(run.run_id)||[];const inverse=[...index].sort((a,b)=>a[1]-b[1]);const lo=axis==='step'?zoom[0]:interpolate(inverse,zoom[0],1,0),hi=axis==='step'?zoom[1]:interpolate(inverse,zoom[1],1,0);if(lo!=null&&hi!=null){query.set('start',Math.max(0,Math.floor(Math.min(lo,hi))));query.set('end',Math.ceil(Math.max(lo,hi)));}}
          const values=await get(`${apiRun(run.run_id)}/scalars?${query}`);
          const series=values && values.series && typeof values.series==='object' && !Array.isArray(values.series) ? values.series : {};
          if(!destroyed&&currentGeneration===generation)cache.set(run.run_id,{...(cache.get(run.run_id)||{}),...series});
        }));
        if(destroyed)return;
        if(currentGeneration===generation)for(const card of active())draw(card);else queued=true;
      }
      const latest=await get('/api/runs');
      if(destroyed)return;
      const live=latest.some(r=>shown.has(r.run_id)&&r.status==='running');
      message.textContent=live?'Live · refreshes every 5 s':'Up to date';
      // Discover keys logged since this view opened; keep existing plots intact.
      if(live){
        await Promise.all(runs.filter(r=>shown.has(r.run_id)).map(async run=>{
          const id=run.run_id;
          const meta=await get(apiRun(id));
          if(destroyed)return;
          run.keys=meta.keys;
          run.run_id=id;
          for(const key of meta.keys)addKey(key);
        }));
        timer=setTimeout(refresh,5000);
      }
    }catch(err){if(err.name!=='AbortError'&&!destroyed){message.textContent=`Could not refresh: ${err.message}`;timer=setTimeout(refresh,5000);}}
    finally{busy=false;if(queued&&!destroyed){queued=false;schedule();}}
  }
  schedule();
}

async function route(){
  const version=++epoch;cleanup();cleanup=()=>{};controller?.abort();controller=new AbortController();
  app.replaceChildren(el('p',{class:'muted',text:'Loading…'}));
  try{
    const hashURL=new URL((location.hash.slice(1)||'/'),'http://local');
    if(hashURL.pathname.startsWith('/run/'))await runPage(decodeURIComponent(hashURL.pathname.slice(5)),version);
    else if(hashURL.pathname==='/compare')await charts([...new Set((hashURL.searchParams.get('runs')||'').split(',').filter(Boolean))],version);
    else await home(version);
  }catch(err){if(epoch===version)error(app,err);}
}
window.addEventListener('hashchange',route);
route();
