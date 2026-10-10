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
  function esc(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
  function ms(value){return value==null?"—":Number(value)<1000?fmt(value,0)+" ms":fmt(Number(value)/1000,1)+" s";}
  function words(key){return String(key||"").replaceAll("_"," ");}
  function egressNote(byEgress){
    return Object.entries(byEgress||{}).map(([address,value])=>{
      if(typeof value==="number")return esc(address)+" ×"+value;
      const parts=[value.p95_wait_ms!=null?"wait P95 "+ms(value.p95_wait_ms):null,value.p95_ms!=null?"P95 "+ms(value.p95_ms):null,
        value.median_mbps!=null?fmt(value.median_mbps,1)+" Mbit/s":null].filter(Boolean);
      return esc(address)+(parts.length?" ("+parts.join(", ")+")":"");
    }).join(" · ");
  }
  // Findings explain what the simulated users feel and which appliance address (WAN) carried it.
  function findingsHtml(diagnosis){
    const items=(diagnosis&&diagnosis.findings)||[];
    if(!items.length)return '<li class="finding-empty">No problems detected in this window.</li>';
    return items.map(item=>{
      const via=egressNote(item.by_egress);
      return '<li class="finding '+esc(item.severity)+'"><span class="finding-dot" aria-hidden="true"></span><div>'+
        '<div class="finding-title">'+esc(item.title)+'</div><div class="finding-detail">'+esc(item.detail)+'</div>'+
        (via?'<div class="finding-meta">Seen via '+via+'</div>':'')+'</div></li>';
    }).join("");
  }
  function causeText(causes,labels){
    const entries=Object.entries(causes||{});
    return entries.length?entries.map(([key,count])=>esc((labels||{})[key]||words(key))+" ×"+count).join(", "):"—";
  }
  function egressRowsHtml(diagnosis){
    const entries=Object.entries((diagnosis&&diagnosis.egress)||{}).sort((a,b)=>b[1].requests-a[1].requests);
    if(!entries.length)return '<tr><td colspan="7" class="muted">No transactions in this window.</td></tr>';
    return entries.map(([address,item])=>{
      const worst=Object.entries(item.applications||{}).filter(([,app])=>app.failures>0).sort((a,b)=>a[1].availability_pct-b[1].availability_pct)[0];
      return "<tr><td class='mono'>"+esc(address)+"</td><td>"+(item.experience_score??"—")+"</td><td>"+(item.availability_pct==null?"—":fmt(item.availability_pct,1)+"%")+
        "</td><td>"+ms(item.p95_ms)+"</td><td>"+item.requests+"</td><td>"+causeText(item.causes,diagnosis.cause_labels)+"</td><td>"+
        (worst?esc(words(worst[0]))+" "+fmt(worst[1].availability_pct,1)+"%":"—")+"</td></tr>";
    }).join("");
  }
  function appRowsHtml(applications,labels){
    return Object.entries(applications||{}).map(([key,item])=>{
      const rate=[item.median_down_mbps!=null?"↓ "+fmt(item.median_down_mbps,1):null,item.median_up_mbps!=null?"↑ "+fmt(item.median_up_mbps,1):null].filter(Boolean).join(" · ");
      const media=item.media?fmt(item.media.loss_pct,2)+"% · "+(item.media.bursts_with_loss+item.media.no_reply)+"/"+item.media.bursts+" bursts":"—";
      const top=item.top_cause?esc((labels||{})[item.top_cause]||words(item.top_cause))+" ×"+(item.causes||{})[item.top_cause]:"—";
      return "<tr><td>"+esc(words(key))+"</td><td>"+(item.experience_score??"—")+"</td><td>"+(item.availability_pct==null?"—":fmt(item.availability_pct,1)+"%")+
        "</td><td>"+ms(item.p95_ms)+"</td><td>"+ms(item.p95_wait_ms)+"</td><td>"+ms(item.p95_transfer_ms)+"</td><td>"+(rate?rate+" Mbit/s":"—")+
        "</td><td>"+media+"</td><td>"+top+"</td><td>"+item.requests+"</td></tr>";
    }).join("");
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
        const top=((dem.diagnosis||{}).findings||[])[0];
        const finding=document.getElementById("live-finding");
        if(finding){finding.textContent=top?top.title:"No problems detected in the last 60 seconds.";finding.className="live-finding "+(top?top.severity:"good");}
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
          document.getElementById("dem-p95").textContent=s.interactive_p95_ms==null?"—":fmt(s.interactive_p95_ms,0)+" ms";
          document.getElementById("dem-p95-all").textContent="web, collaboration, DNS · all apps "+(s.p95_ms==null?"—":fmt(s.p95_ms,0)+" ms");
          document.getElementById("dem-users").textContent=payload.users??0;
          const diagnosis=s.diagnosis||{};
          document.getElementById("dem-findings").innerHTML=findingsHtml(diagnosis);
          document.getElementById("dem-egress").innerHTML=egressRowsHtml(diagnosis);
          document.getElementById("dem-media-mode").textContent=words(diagnosis.media_mode||"strict").replace(/^./,c=>c.toUpperCase())+" media";
          document.getElementById("dem-apps").innerHTML=appRowsHtml(s.applications||{},diagnosis.cause_labels);
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
  function fillAppliance(data){
    for(const [suffix,key] of [['interface','candidateInterface'],['gateway','candidateGateway'],['name','candidateName'],['vendor','candidateVendor'],['model','candidateModel'],['firmware','candidateFirmware']]){
      const field=document.getElementById('appliance-'+suffix);if(field)field.value=data[key]||'';
    }
    const details=document.getElementById('detected-appliance-details');if(details)details.textContent=(data.candidateEvidence||'No exposed identity')+'. Gateway role requires Verify & select.';
  }
  const UPDATE_STEPS=[['installing','Installing release'],['restarting','Restarting service'],['reconnecting','Reconnecting']];
  const UPDATE_TEXT={
    installing:['Installing update','Downloading the release and keeping a rollback copy. The simulator restarts shortly.'],
    restarting:['Restarting simulator','The service is offline while the new version installs and starts. This page reconnects automatically.'],
    reconnecting:['Finishing update','The simulator is back online. Waiting for the update worker to confirm the new service.'],
    complete:['Update complete','Reloading…'],failed:['Update failed','']};
  let updateActive=false;
  // state: /settings/system/data payload, or null while the service is unreachable.
  function updatePhase(state,sawDowntime){
    if(!state)return 'restarting';
    if(state.updating)return sawDowntime?'reconnecting':'installing';
    const job=state.job||{};
    return job.action==='install_update'&&job.state==='failed'?'failed':'complete';
  }
  function updateScreen(from,to){
    if(updateActive)return;updateActive=true;
    cleanups.splice(0).forEach(stop=>stop());
    const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!=null)node.textContent=text;return node;};
    const screen=el('div','update-screen'),card=el('div','update-card'),ring=el('div','update-ring');
    screen.setAttribute('role','dialog');screen.setAttribute('aria-modal','true');screen.setAttribute('aria-labelledby','update-title');
    ring.append(el('span','update-mark','NT'));
    const title=el('h2',null,UPDATE_TEXT.installing[0]);title.id='update-title';
    const message=el('p','update-message');message.setAttribute('aria-live','polite');
    const steps=el('ol','update-steps'),items=UPDATE_STEPS.map(([,label])=>steps.appendChild(el('li',null,label)));
    const meta=el('div','update-meta mono'),actions=el('div','update-actions');
    card.append(ring,title,el('div','update-versions mono','v'+from+(to?' → '+to:'')),message,steps,meta,actions);card.tabIndex=-1;
    screen.append(card);document.body.append(screen);document.body.classList.add('update-active');card.focus();
    const started=Date.now();let sawDowntime=false,phase='installing',reached=0;
    const clock=setInterval(()=>{
      const s=Math.floor((Date.now()-started)/1000);
      meta.textContent='Elapsed '+Math.floor(s/60)+':'+String(s%60).padStart(2,'0')+(s>600?' · taking longer than usual; see journalctl -u netem-traffic-simulator-admin':'');
    },1000);
    function render(state){
      const index=phase==='complete'?UPDATE_STEPS.length:UPDATE_STEPS.findIndex(([key])=>key===phase);
      if(index>=0)reached=index;
      items.forEach((item,i)=>item.className=i<reached?'done':i===reached?(phase==='failed'?'failed':'active'):'');
      screen.className='update-screen '+phase;title.textContent=UPDATE_TEXT[phase][0];
      message.textContent=phase==='complete'?'Now running v'+state.version+'. Reloading…':phase==='failed'?(state.job.message||'The update did not finish.'):UPDATE_TEXT[phase][1];
      if(phase==='failed'){const back=el('button','btn primary','Back to System');back.type='button';back.addEventListener('click',()=>location.reload());actions.replaceChildren(back);back.focus();}
    }
    async function poll(){
      let state=null;
      try{
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
        const response=await fetch('/settings/system/data',{cache:'no-store',signal:controller.signal});clearTimeout(timer);
        // A non-JSON success is the sign-in page: let the browser show it.
        if(response.ok&&!(response.headers.get('content-type')||'').includes('json')){location.reload();return;}
        if(response.ok)state=await response.json();
      }catch(_){}
      if(!state)sawDowntime=true;
      phase=updatePhase(state,sawDowntime);render(state);
      if(phase==='complete'||phase==='failed')clearInterval(clock);
      if(phase==='complete')setTimeout(()=>location.reload(),2000);
      else if(phase!=='failed')setTimeout(poll,2000);
    }
    render(null);poll();
  }
  function renderTargetState(state){
    const target=state.target||{},release=target.release||{},job=target.job||{};
    const status=document.getElementById('target-status');if(!status)return;
    status.textContent=target.connected?'Connected to '+target.host+' · installed v'+target.version:'Connect the update service on your virtual ISP modem.';
    const match=document.getElementById('target-version-match');
    if(match){
      const mismatch=target.connected&&target.version_match===false;
      match.className=mismatch?'warning':'muted';
      match.textContent=mismatch?'This simulator runs v'+target.simulator_version+'. '+(target.behind_simulator?'Update the target to the same release: ':'The target runs a newer release; update the simulator to match: ')+'per-WAN attribution and voice/video replies depend on matching versions.'
        :(target.connected&&target.version_match?'Matches this simulator (v'+target.simulator_version+').':'');
    }
    document.getElementById('target-release-status').textContent=release.tag?'Latest: '+(release.display_tag||release.tag)+(release.available?' · update available':' · up to date'):'No target release check yet.';
    document.getElementById('target-job-status').textContent=target.busy?'Target task running…':(job.message||'');
    document.getElementById('target-release-tag').value=release.tag||'';
    for(const button of document.querySelectorAll('[data-target-action]')){
      button.disabled=!state.ready||state.busy||!target.connected||(target.busy&&button.dataset.targetAction!=='target_status')||
        (button.dataset.targetAction==='target_install'&&(!release.available||state.workload_active));
    }
  }
  function systemPage(){
    let alive=true;cleanups.push(()=>alive=false);
    const jobStatus=document.getElementById('system-job');
    if(jobStatus.dataset.updating==='true'){updateScreen(jobStatus.dataset.version,jobStatus.dataset.release);return;}
    let previous=jobStatus.dataset.busy==='true';
    document.getElementById('detected-appliance')?.addEventListener('change',event=>{
      const option=event.target.selectedOptions[0];if(!option?.value)return;
      fillAppliance(option.dataset);
    });
    async function poll(){
      try{
        const response=await fetch('/settings/system/data',{cache:'no-store'});if(!response.ok)return;
        const state=await response.json();if(!alive)return;
        renderTargetState(state);
        if(state.updating){updateScreen(state.version,state.release.display_tag||state.release.tag);return;}
        const job=state.job||{};
        jobStatus.textContent=state.busy?('Working: '+(job.action||'queued task')+'…'):(job.message||'No administration task yet.');
        if(previous===true&&!state.busy){await refreshPage();return;}previous=state.busy;
        if(state.release.tag)document.getElementById('release-status').textContent='Latest: '+(state.release.display_tag||state.release.tag)+(state.release.available?' · update available':' · up to date');
        const health=document.getElementById('route-health');if(health&&state.route_health){health.textContent=state.route_health.message;health.classList.toggle('error',state.route_health.state==='error');}
        if(state.selected)document.getElementById('selected-route').textContent='Saved selection: '+state.selected.target+' → '+state.selected.gateway+' via '+state.selected.interface;
        const select=document.getElementById('detected-appliance');
        if(select && !select.matches(':focus')){
          const selected=select.selectedOptions[0];
          const key=selected?.dataset.candidateInterface+'|'+selected?.dataset.candidateGateway;
          const options=(state.scanned_candidates||[]).map((candidate,index)=>{
            const option=document.createElement('option');option.value=String(index);
            for(const [name,value] of Object.entries({Interface:candidate.interface,Gateway:candidate.gateway,Name:candidate.name,Vendor:candidate.vendor,Model:candidate.model||'',Firmware:candidate.firmware||'',Evidence:candidate.identity_evidence+' · '+candidate.confidence}))option.dataset['candidate'+name]=value;
            option.textContent=candidate.name+' · '+(candidate.vendor==='Other'?'Unknown':candidate.vendor)+' · '+candidate.gateway+' on '+candidate.interface;
            option.selected=key===candidate.interface+'|'+candidate.gateway;return option;
          });
          const empty=document.createElement('option');empty.value='';empty.textContent='Select a discovered appliance…';
          select.replaceChildren(empty,...options);
        }
      }catch(_){}
    }poll();repeat(poll,2000);
  }
  return {liveStatus,demPage,renderTimestamps,mount,systemPage,refreshPage,animatePath,pathFor,fillAppliance,updatePhase,updateScreen,findingsHtml,egressRowsHtml,appRowsHtml,renderTargetState};
})();
