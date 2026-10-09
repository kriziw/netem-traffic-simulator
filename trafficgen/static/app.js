window.TrafficGen = (() => {
  let cleanups=[];
  let busy=false;
  function repeat(fn, delay){const id=setInterval(fn,delay);cleanups.push(()=>clearInterval(id));}
  const animations=new WeakMap();
  function animatePath(element, path){
    const pending=animations.get(element);if(pending)cancelAnimationFrame(pending);
    const from=(element.getAttribute("d")||"").match(/[ML]-?[\d.]+,-?[\d.]+/g)||[];
    const to=path.match(/[ML]-?[\d.]+,-?[\d.]+/g)||[];
    if(!from.length||!to.length||from.filter(x=>x[0]==="M").length!==1||to.filter(x=>x[0]==="M").length!==1||window.matchMedia?.("(prefers-reduced-motion: reduce)").matches){element.setAttribute("d",path);return;}
    let began;
    const frame=now=>{began??=now;const t=Math.min(1,(now-began)/600),ease=t*t*(3-2*t);
      element.setAttribute("d",to.map((p,i)=>{const a=from[Math.min(i,from.length-1)].slice(1).split(",").map(Number),b=p.slice(1).split(",").map(Number);return p[0]+b.map((n,j)=>(a[j]+(n-a[j])*ease).toFixed(2)).join(",");}).join(" "));
      if(t<1)animations.set(element,requestAnimationFrame(frame));else{element.setAttribute("d",path);animations.delete(element);}
    };animations.set(element,requestAnimationFrame(frame));
  }
  function mount(){
    renderTimestamps();
    document.getElementById('page-scripts')?.content.querySelectorAll('script').forEach(script=>new Function(script.textContent)());
  }
  async function updatePage(response){
    if(!response.ok)throw new Error("Request failed ("+response.status+"). Check status before retrying.");
    const next=new DOMParser().parseFromString(await response.text(),"text/html");
    const workspace=next.querySelector('main.workspace'),scripts=next.getElementById('page-scripts');
    if(!workspace||!scripts)throw new Error("Unexpected response. Please sign in again if your session expired.");
    const scroll=window.scrollY;
    const range=document.getElementById('dem-range')?.value;
    const chart=document.getElementById('dem-score-line');
    if(chart&&workspace.querySelector('#dem-score-line'))workspace.querySelector('#dem-score-line').replaceWith(chart);
    cleanups.splice(0).forEach(stop=>stop());
    document.querySelector('main.workspace').replaceWith(workspace);
    document.getElementById('page-scripts').replaceWith(scripts);
    const oldSidebar=document.querySelector('.sidebar'),sidebar=next.querySelector('.sidebar');
    if(oldSidebar&&sidebar)oldSidebar.replaceWith(sidebar);
    if(range&&document.getElementById('dem-range'))document.getElementById('dem-range').value=range;
    const url=new URL(response.url||location.href);history.replaceState(null,'',url.pathname+url.search);
    document.title=next.title;mount();window.scrollTo(0,scroll);
  }
  function showError(message){
    let el=document.getElementById('control-error');
    if(!el){el=document.createElement('div');el.id='control-error';el.className='flash error';el.setAttribute('role','alert');document.querySelector('main.workspace').prepend(el);}
    el.textContent=message;
  }
  async function refreshPage(){
    if(busy)return;busy=true;
    try{await updatePage(await fetch(location.href,{cache:'no-store'}));}catch(error){showError(error.message);}finally{busy=false;}
  }
  document.addEventListener('submit',async event=>{
    const form=event.target;if(event.defaultPrevented||!(form instanceof HTMLFormElement)||form.method.toLowerCase()!=='post')return;
    const action=new URL(form.getAttribute('action')||location.href,location.href);if(action.origin!==location.origin)return;
    event.preventDefault();if(busy)return;busy=true;
    const data=new FormData(form);if(event.submitter?.name)data.append(event.submitter.name,event.submitter.value);
    const buttons=[...form.querySelectorAll('button')],disabled=buttons.map(b=>b.disabled);buttons.forEach(b=>b.disabled=true);form.setAttribute('aria-busy','true');
    try{await updatePage(await fetch(action.href,{method:'POST',body:data}));}catch(error){showError(error.message);}finally{buttons.forEach((b,i)=>b.disabled=disabled[i]);form.removeAttribute('aria-busy');busy=false;}
  });

  function fmt(value,digits=1){
    if(value==null)return "—";
    const n=Number(value); return Number.isFinite(n)?n.toFixed(digits):"—";
  }
  function pathFor(values){
    if(!values.length) return "";
    const max=100,min=0;
    let drawing=false;
    return values.map((value,index)=>{
      if(value==null || !Number.isFinite(Number(value))){drawing=false;return "";}
      const x=values.length===1?0:index/(values.length-1)*100;
      const y=40-(Math.max(min,Math.min(max,Number(value)))-min)/(max-min)*40;
      const command=(drawing?"L":"M")+x.toFixed(2)+","+y.toFixed(2);
      drawing=true;return command;
    }).join(" ");
  }
  function renderTimestamps(){
    document.querySelectorAll("[data-ts]").forEach(el=>{
      el.textContent=new Date(Number(el.dataset.ts)*1000).toLocaleString();
    });
  }
  async function liveStatus(){
    let alive=true;cleanups.push(()=>alive=false);
    async function refresh(){
      try{
        const response=await fetch("/ui/status",{cache:"no-store"});
        if(!response.ok)return;
        const status=await response.json();if(!alive)return;
        const dem=status.dem||{};
        const set=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=value;};
        set("live-state",String(status.status||"idle").toUpperCase());
        set("live-run",status.run?.run_id||"No active run");
        set("live-users",status.users??0);
        set("live-score",dem.experience_score??"—");
        set("live-rating",dem.experience||"No data");
        set("live-availability",dem.availability_pct==null?"—":fmt(dem.availability_pct,2)+"%");
        set("live-p95","P95 "+(dem.p95_ms==null?"—":fmt(dem.p95_ms,0))+" ms");
        set("experience-score",dem.experience_score??"—");
        set("experience-rating",dem.experience||"No data");
        set("live-rps",dem.requests_per_second??0);
        set("live-fps",dem.failures_per_second??0);
        set("live-p50",(dem.p50_ms==null?"—":fmt(dem.p50_ms,0))+" ms");
        set("live-p95-card",(dem.p95_ms==null?"—":fmt(dem.p95_ms,0))+" ms");
      }catch(_){}
    }
    refresh();
    repeat(refresh,2500);
  }
  async function demPage(){
    const range=document.getElementById("dem-range");
    let alive=true;cleanups.push(()=>alive=false);
    async function load(){
      try{
        const minutes=Number(range.value||15);
        const response=await fetch("/dem/data?minutes="+minutes,{cache:"no-store"});
        if(!response.ok)return;
        const payload=await response.json();if(!alive)return;
        const samples=payload.samples||[];
        animatePath(document.getElementById("dem-score-line"),pathFor(samples.map(x=>x.experience_score)));
        document.getElementById("dem-start").textContent=samples.length?new Date(samples[0].timestamp*1000).toLocaleTimeString():"—";
        if(payload.summary){
          const s=payload.summary;
          document.getElementById("dem-score").textContent=s.experience_score??"—";
          document.getElementById("dem-rating").textContent=s.experience||"No data";
          document.getElementById("dem-availability").textContent=s.availability_pct==null?"—":fmt(s.availability_pct,2)+"%";
          document.getElementById("dem-p95").textContent=s.p95_ms==null?"—":fmt(s.p95_ms,0)+" ms";
          document.getElementById("dem-users").textContent=payload.users??0;
          document.getElementById("dem-apps").innerHTML=rows(s.applications||{});
          document.getElementById("dem-personas").innerHTML=rows(s.personas||{});
          document.getElementById("dem-endpoints").innerHTML=endpointRows(s.endpoints||{});
        }
      }catch(_){}
    }
    function rows(items){
      return Object.entries(items).map(([key,item])=>"<tr><td>"+key.replaceAll("_"," ")+"</td><td>"+(item.experience_score??"—")+"</td><td>"+(item.availability_pct==null?"—":fmt(item.availability_pct,1)+"%")+"</td><td>"+(item.p95_ms==null?"—":fmt(item.p95_ms,0)+" ms")+"</td><td>"+item.requests+"</td></tr>").join("");
    }
    function endpointRows(items){
      return Object.values(items)
        .sort((a,b)=>(a.experience_score??999)-(b.experience_score??999))
        .slice(0,200)
        .map(item=>"<tr><td class='mono'>"+item.endpoint_id+"</td><td>"+String(item.persona||"unknown").replaceAll("_"," ")+"</td><td>"+(item.experience_score??"—")+"</td><td>"+(item.availability_pct==null?"—":fmt(item.availability_pct,1)+"%")+"</td><td>"+(item.p95_ms==null?"—":fmt(item.p95_ms,0)+" ms")+"</td><td>"+item.requests+"</td></tr>").join("");
    }
    range.addEventListener("change",load);
    load();
    repeat(load,3000);
  }
  function systemPage(){
    let alive=true;cleanups.push(()=>alive=false);
    let previous=document.getElementById('system-job').dataset.busy==='true';
    document.getElementById('gateway-candidates')?.addEventListener('click',event=>{
      const button=event.target.closest('[data-candidate-interface]');if(!button)return;
      document.getElementById('appliance-interface').value=button.dataset.candidateInterface;
      document.getElementById('appliance-gateway').value=button.dataset.candidateGateway;
    });
    async function poll(){
      try{
        const response=await fetch('/settings/system/data',{cache:'no-store'});if(!response.ok)return;
        const state=await response.json();if(!alive)return;
        const job=state.job||{};
        document.getElementById('system-job').textContent=state.busy?('Working: '+(job.action||'queued task')+'…'):(job.message||'No administration task yet.');
        if(previous===true&&!state.busy){await refreshPage();return;}previous=state.busy;
        if(state.release.tag)document.getElementById('release-status').textContent='Latest: '+(state.release.display_tag||state.release.tag);
        const health=document.getElementById('route-health');if(health&&state.route_health){health.textContent=state.route_health.message;health.classList.toggle('error',state.route_health.state==='error');}
        if(state.selected)document.getElementById('selected-route').textContent='Saved selection: '+state.selected.target+' → '+state.selected.gateway+' via '+state.selected.interface;
        if(state.scanned_candidates?.length){
          const body=document.getElementById('gateway-candidates');
          body.replaceChildren(...state.scanned_candidates.map(candidate=>{
            const tr=document.createElement('tr');for(const value of [candidate.interface,candidate.gateway,candidate.evidence]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
            const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='btn small';button.textContent='Use candidate';button.dataset.candidateInterface=candidate.interface;button.dataset.candidateGateway=candidate.gateway;td.append(button);tr.append(td);return tr;
          }));
        }
      }catch(_){}
    }poll();repeat(poll,2000);
  }
  return {liveStatus,demPage,renderTimestamps,mount,systemPage,refreshPage,animatePath,pathFor};
})();
