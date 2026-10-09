window.TrafficGen = (() => {
  function fmt(value,digits=1){
    const n=Number(value); return Number.isFinite(n)?n.toFixed(digits):"—";
  }
  function pathFor(values){
    if(!values.length) return "";
    const max=100,min=0;
    return values.map((value,index)=>{
      const x=values.length===1?0:index/(values.length-1)*100;
      const y=40-(Math.max(min,Math.min(max,Number(value)))-min)/(max-min)*40;
      return (index?"L":"M")+x.toFixed(2)+","+y.toFixed(2);
    }).join(" ");
  }
  function renderTimestamps(){
    document.querySelectorAll("[data-ts]").forEach(el=>{
      el.textContent=new Date(Number(el.dataset.ts)*1000).toLocaleString();
    });
  }
  async function liveStatus(){
    async function refresh(){
      try{
        const response=await fetch("/ui/status",{cache:"no-store"});
        if(!response.ok)return;
        const status=await response.json();
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
    setInterval(refresh,2500);
  }
  async function demPage(){
    const range=document.getElementById("dem-range");
    async function load(){
      try{
        const minutes=Number(range.value||15);
        const response=await fetch("/dem/data?minutes="+minutes,{cache:"no-store"});
        if(!response.ok)return;
        const payload=await response.json();
        const samples=payload.samples||[];
        document.getElementById("dem-score-line").setAttribute("d",pathFor(samples.map(x=>x.experience_score)));
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
        }
      }catch(_){}
    }
    function rows(items){
      return Object.entries(items).map(([key,item])=>"<tr><td>"+key.replaceAll("_"," ")+"</td><td>"+(item.experience_score??"—")+"</td><td>"+(item.availability_pct==null?"—":fmt(item.availability_pct,1)+"%")+"</td><td>"+(item.p95_ms==null?"—":fmt(item.p95_ms,0)+" ms")+"</td><td>"+item.requests+"</td></tr>").join("");
    }
    range.addEventListener("change",load);
    load();
    setInterval(load,3000);
  }
  return {liveStatus,demPage,renderTimestamps};
})();
