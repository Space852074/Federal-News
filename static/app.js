const $=s=>document.querySelector(s); let news=[],sources=[],view='cards',selectedSource='';
async function load(){const opts={cache:'no-store'};const [n,s,m]=await Promise.all([fetch('/api/news',opts).then(r=>r.json()),fetch('/api/sources',opts).then(r=>r.json()),fetch('/api/meta',opts).then(r=>r.json())]);news=n;sources=s;renderMeta(m);renderSources();populateGroups();render();return m}
function renderMeta(m){const vals=[['新闻',m.items||0],['来源',m.sources||sources.length],['有更新',m.ok||0],['无近期内容',m.empty||0],['异常',m.error||0]];$('#stats').innerHTML=vals.map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');if(m.status==='running'){$('#notice').textContent=m.phase==='translating'?`正在翻译 ${m.translation_total||0} 条待翻译内容…`:`正在抓取并验证 ${m.sources||sources.length||49} 个官方来源…`;return}if(m.status==='error'){$('#notice').textContent=`刷新失败：${m.error_message||'请重试'}`;return}$('#notice').textContent=m.refreshed_at?`范围：${m.cutoff} 至今 · 上次刷新：${new Date(m.refreshed_at).toLocaleString()}`:'尚未抓取。点击“刷新并翻译”开始验证来源并抓取最近 5 天新闻。'}
function healthLabel(s){if(s.health==='ok')return s.strategy&&s.strategy!=='official_html'?'备用通道成功':'抓取正常';if(s.health==='empty')return'已重试，近5天无内容';if(s.http_status===403)return'网站拦截';if(s.http_status===404)return'网址失效';if(s.health==='error')return'连接失败';return'未检查'}
function renderSources(){const ordered=[...sources].sort((a,b)=>(b.items||0)-(a.items||0)||a.agency.localeCompare(b.agency,'zh-CN'));$('#all-sources').classList.toggle('active',!selectedSource);$('#all-sources').textContent=selectedSource?'显示全部来源':'全部来源';$('#sources').innerHTML=ordered.map(s=>`<button type="button" class="source ${selectedSource===s.id?'active':''}" data-source="${esc(s.id)}" title="${esc(s.message||s.note||'')}"><i class="dot ${s.health||''}"></i><span>${esc(s.agency)}<br><small>${s.http_status||'—'} · ${healthLabel(s)}</small></span><b>${s.items||0}</b></button>`).join('');document.querySelectorAll('[data-source]').forEach(el=>el.onclick=()=>{selectedSource=selectedSource===el.dataset.source?'':el.dataset.source;$('#search').value='';$('#group').value='';$('#state').value='';renderSources();render()})}
function populateGroups(){const groups=[...new Set(sources.map(x=>x.group))].filter(Boolean).sort();$('#group').innerHTML='<option value="">全部机构分组</option>'+groups.map(g=>`<option>${esc(g)}</option>`).join('')}
function render(){const q=$('#search').value.toLowerCase(),g=$('#group').value,st=$('#state').value;const rows=news.filter(x=>(!selectedSource||x.source_id===selectedSource)&&(!q||(x.title+x.title_zh+x.summary_zh).toLowerCase().includes(q))&&(!g||x.group===g)&&(!st||x.state===st));$('#news').className=view;$('#news').innerHTML=rows.length?rows.map(card).join(''):'<div class="empty-state">当前筛选下没有新闻</div>'}
function card(x){const displayTitle=x.title_zh||x.title;return `<article class="card"><div class="meta"><span class="agency">${esc(x.agency)}</span><time>${x.date}</time></div><h2 title="${esc(displayTitle)}">${esc(displayTitle)}</h2>${x.title_zh?`<div class="original">${esc(x.title)}</div>`:''}<footer><span class="tag">${esc(x.state)}</span><a href="${esc(x.url)}" target="_blank" rel="noreferrer">查看原文 ↗</a></footer></article>`}
function esc(s=''){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
$('#all-sources').onclick=()=>{selectedSource='';renderSources();render()};$('#search').addEventListener('input',render);$('#group').addEventListener('change',render);$('#state').addEventListener('change',render);document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{view=b.dataset.view;document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x===b));render()});
let refreshTimer=null;
async function pollRefresh(){
  try{
    const m=await load();
    if(m.status==='running'){refreshTimer=setTimeout(pollRefresh,2000);return}
  }catch(e){$('#notice').textContent='读取刷新进度失败，正在重试…';refreshTimer=setTimeout(pollRefresh,3000);return}
  $('#refresh').disabled=false; $('#translate').disabled=false; refreshTimer=null;
}
async function startJob(path,message){if(refreshTimer)return;$('#refresh').disabled=true;$('#translate').disabled=true;const r=await fetch(path,{method:'POST'});const result=await r.json();if(!r.ok){$('#notice').textContent=result.error||'无法启动任务';$('#refresh').disabled=false;$('#translate').disabled=false;return}$('#notice').textContent=result.status==='running'?'任务已在执行，正在读取进度…':message;refreshTimer=setTimeout(pollRefresh,1000)}
$('#refresh').onclick=()=>startJob('/api/refresh','正在后台抓取、验证并翻译…');
$('#translate').onclick=()=>startJob('/api/translate','正在翻译现有待翻译新闻…');
async function pollNotion(){
  const s=await fetch('/api/notion-status').then(r=>r.json());
  if(s.status==='running'){ $('#notice').textContent='正在同步 Notion…'; setTimeout(pollNotion,2000); return; }
  if(s.status==='complete'){$('#notice').textContent=`Notion 同步完成：新增 ${s.created} 条，跳过 ${s.skipped} 条`;return}
  if(s.status==='partial'){$('#notice').textContent=`Notion 部分完成：新增 ${s.created} 条，失败 ${(s.errors||[]).length} 条`;return}
  $('#notice').textContent=s.error||`Notion 状态：${s.status}`;
}
$('#notion').onclick=async()=>{const token=prompt('粘贴 Notion 内部集成令牌（仅在本机内存中使用，不会保存）');if(!token)return;const r=await fetch('/api/notion-sync',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token.trim(),database_id:'3a393e41daef80ea84bcfc4d06501ec3'})});const started=await r.json();if(!r.ok){$('#notice').textContent=started.error||'无法启动 Notion 同步';return}$('#notice').textContent='正在同步 Notion…';setTimeout(pollNotion,1500)};
load();
