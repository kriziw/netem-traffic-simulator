const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function context(){
  let callback;
  const window={matchMedia:()=>({matches:false})};
  const ctx={window,document:{addEventListener(){}},requestAnimationFrame:fn=>{callback=fn;return 1},cancelAnimationFrame(){},setInterval(){return 1},clearInterval(){}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  return {ui:window.TrafficGen,frame:now=>callback(now)};
}

test('controller polling stays on the NetEm mounted origin',async()=>{
  let requested;
  const ctx={window:{TrafficGenControllerPrefix:'/simulator',fetch:async url=>{requested=url;return {ok:true,json:async()=>({status:'idle',dem:{}})}}},
    document:{addEventListener(){},getElementById(){return null}},setInterval(){return 1},clearInterval(){}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  await ctx.window.TrafficGen.liveStatus();
  assert.equal(requested,'/simulator/ui/status');
});
test('DEM animation reaches exact samples, with real gaps retained',()=>{
  const {ui,frame}=context();
  const path={d:'M0,40 L100,40',getAttribute(){return this.d},setAttribute(_,v){this.d=v}};
  ui.animatePath(path,ui.pathFor([0,100]));frame(0);frame(300);
  assert.match(path.d,/100.00,20.00/);frame(600);
  assert.equal(path.d,'M0.00,40.00 L100.00,0.00');
  ui.animatePath(path,ui.pathFor([0,null,100]));
  assert.equal((path.d.match(/M/g)||[]).length,2);
});

test('controls post to the action attribute when a named input shadows form.action', async () => {
  let submit, sent;
  class Form {constructor(){this.method='post';this.action={name:'action',value:'check_update'};}
    getAttribute(name){return name==='action'?'/settings/system/action':null;}
    querySelectorAll(){return [];}
    setAttribute(){} removeAttribute(){}}
  class Data {constructor(form){this.form=form;} append(){}}
  const ctx={window:{},document:{addEventListener:(name,fn)=>{submit=fn;},getElementById:()=>({textContent:''})},
    HTMLFormElement:Form,FormData:Data,URL,location:{href:'https://simulator.example/',origin:'https://simulator.example'},
    fetch:async(url,options)=>{sent={url,options};return {ok:false,status:400};}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const form=new Form();await submit({target:form,defaultPrevented:false,preventDefault(){}});
  assert.equal(sent.url,'https://simulator.example/settings/system/action');
  assert.equal(sent.options.method,'POST');
  assert.equal(sent.options.body.form,form);
});

test('update screen follows install, restart, reconnect and the final job result',()=>{
  const {ui}=context();
  const updating={updating:true,job:{action:'install_update',state:'running'}};
  assert.equal(ui.updatePhase(updating,false),'installing');
  assert.equal(ui.updatePhase(null,false),'restarting');
  assert.equal(ui.updatePhase(updating,true),'reconnecting');
  assert.equal(ui.updatePhase({updating:false,job:{action:'install_update',state:'completed'}},true),'complete');
  assert.equal(ui.updatePhase({updating:false,job:{action:'install_update',state:'failed',message:'restored'}},true),'failed');
  // Status lost or replaced: reload so the page shows the real state instead of waiting forever.
  assert.equal(ui.updatePhase({updating:false,job:{}},true),'complete');
});

test('selecting an appliance prefills identity and routing fields while preserving the target',()=>{
  const fields={};for(const id of ['interface','gateway','name','vendor','model','firmware'])fields['appliance-'+id]={value:''};
  fields['appliance-target']={value:'198.18.0.1'};fields['detected-appliance-details']={textContent:''};
  const ctx={window:{},document:{addEventListener(){},getElementById:id=>fields[id]}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  ctx.window.TrafficGen.fillAppliance({candidateInterface:'eth1',candidateGateway:'10.250.10.1',candidateName:'FortiGate',candidateVendor:'Fortinet',candidateModel:'FortiGate-VM64',candidateFirmware:'7.6.7',candidateEvidence:'Proxmox · inventory label'});
  assert.equal(fields['appliance-interface'].value,'eth1');assert.equal(fields['appliance-name'].value,'FortiGate');
  assert.equal(fields['appliance-vendor'].value,'Fortinet');assert.equal(fields['appliance-model'].value,'FortiGate-VM64');
  assert.equal(fields['appliance-firmware'].value,'7.6.7');assert.equal(fields['appliance-target'].value,'198.18.0.1');
});

test('diagnosis renders escaped findings, per-WAN rows and the application timing split',()=>{
  const {ui}=context();
  const diagnosis={media_mode:'strict',cause_labels:{media_loss:'Media packet loss'},
    findings:[{severity:'bad',title:'Video: <b>3</b> of 10 bursts lost packets',detail:'Packet loss 0.40%',by_egress:{'198.18.2.2':3}},
              {severity:'warn',title:'Downloads limited by bandwidth',detail:'P95 4.4 s',by_egress:{'198.18.1.2':{transfers:6,p95_ms:4400,median_mbps:11.2}}}],
    egress:{'198.18.2.2':{requests:10,experience_score:70,availability_pct:70,p95_ms:30,causes:{media_loss:3},applications:{video:{requests:10,failures:3,availability_pct:70}}}}};
  const findings=ui.findingsHtml(diagnosis);
  assert.match(findings,/&lt;b&gt;3&lt;\/b&gt;/);assert.doesNotMatch(findings,/<b>/);
  assert.match(findings,/Seen via 198\.18\.2\.2 ×3/);assert.match(findings,/198\.18\.1\.2 \(P95 4\.4 s, 11\.2 Mbit\/s\)/);
  assert.match(ui.findingsHtml({findings:[]}),/No problems detected/);
  const egress=ui.egressRowsHtml(diagnosis);
  assert.match(egress,/Media packet loss ×3/);assert.match(egress,/video 70\.0%/);
  const apps=ui.appRowsHtml({updates:{experience_score:60,availability_pct:100,p95_ms:4400,p95_wait_ms:100,p95_transfer_ms:4300,median_down_mbps:11.2,requests:6,causes:{}},
    video:{experience_score:70,availability_pct:70,p95_ms:30,media:{loss_pct:0.4,bursts:10,bursts_with_loss:3,no_reply:0},top_cause:'media_loss',causes:{media_loss:3},requests:10}},diagnosis.cause_labels);
  assert.match(apps,/4\.3 s/);assert.match(apps,/↓ 11\.2 Mbit\/s/);assert.match(apps,/0\.40% · 3\/10 bursts/);assert.match(apps,/Media packet loss ×3/);
  const quality=ui.appRowsHtml({erp:{critical:true,experience_score:0,availability_pct:100,requests:20,
    score_components:{deadline:0,reliability:100},quality:{on_time_pct:20,udp_rtt_variation_p95_ms:12},scoring_complete:false}},{});
  assert.match(quality,/critical/);assert.match(quality,/deadline/);assert.match(quality,/20.0%/);assert.match(quality,/incomplete evidence/);
});

test('target update controls reflect remote state and never enable installation during a workload',()=>{
  const fields={};for(const id of ['target-status','target-release-status','target-job-status','target-release-tag'])fields[id]={textContent:'',value:''};
  const buttons=['target_status','target_check','target_install'].map(action=>({dataset:{targetAction:action},disabled:false}));
  const ctx={window:{},document:{addEventListener(){},getElementById:id=>fields[id],querySelectorAll:()=>buttons}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const state={ready:true,busy:false,workload_active:false,target:{connected:true,host:'192.168.10.20',version:'0.7.0',
    busy:false,release:{tag:'v0.8.0',available:true},job:{message:'Verified'}}};
  ctx.window.TrafficGen.renderTargetState(state);
  assert.match(fields['target-status'].textContent,/installed v0.7.0/);
  assert.equal(fields['target-release-tag'].value,'v0.8.0');
  assert.equal(buttons[2].disabled,false);
  state.workload_active=true;ctx.window.TrafficGen.renderTargetState(state);
  assert.equal(buttons[2].disabled,true);assert.equal(buttons[1].disabled,false);
  state.target.busy=true;ctx.window.TrafficGen.renderTargetState(state);
  assert.equal(buttons[0].disabled,false);assert.equal(buttons[1].disabled,true);assert.equal(buttons[2].disabled,true);
  assert.match(fields['target-job-status'].textContent,/running/);
  state.target={connected:false};ctx.window.TrafficGen.renderTargetState(state);
  assert.ok(buttons.every(button=>button.disabled));assert.equal(fields['target-release-tag'].value,'');
});

test('target panel warns when the target and simulator run different releases',()=>{
  const fields={};for(const id of ['target-status','target-release-status','target-job-status','target-release-tag','target-version-match'])fields[id]={textContent:'',value:'',className:''};
  const ctx={window:{},document:{addEventListener(){},getElementById:id=>fields[id],querySelectorAll:()=>[]}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const state={ready:true,busy:false,target:{connected:true,host:'192.168.10.20',version:'0.7.0',simulator_version:'0.8.0',
    version_match:false,behind_simulator:true,release:{},job:{}}};
  ctx.window.TrafficGen.renderTargetState(state);
  assert.match(fields['target-version-match'].textContent,/runs v0\.8\.0\. Update the target to the same release/);
  assert.equal(fields['target-version-match'].className,'warning');
  Object.assign(state.target,{version:'0.8.0',version_match:true,behind_simulator:false});
  ctx.window.TrafficGen.renderTargetState(state);
  assert.match(fields['target-version-match'].textContent,/Matches this simulator/);
});


test('network setup replaces the source when changing NIC and requires wiring reconfirmation',()=>{
  let change;
  const nic={selectedOptions:[{dataset:{address:'10.252.10.10/24'}}],addEventListener:(name,fn)=>{change=fn;}};
  const address={value:'10.251.10.10/24'},confirmation={checked:true};
  const form={querySelector:selector=>selector==='[data-setup-configure]'?null:selector.includes('interface')?nic:selector.includes('address')?address:confirmation};
  const ctx={window:{},document:{addEventListener(){},querySelectorAll:selector=>selector==='[data-setup-form]'?[form]:[]}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  ctx.window.TrafficGen.networkSetup();change();
  assert.equal(address.value,'10.252.10.10/24');assert.equal(confirmation.checked,false);
  nic.selectedOptions=[{dataset:{address:''}}];change();assert.equal(address.value,'');
});


test('simple setup enables valid manual settings and preserves edits during background DHCP updates',async()=>{
  let input,pollTick,requests=0;
  const address={value:'10.251.10.10/24'},gateway={value:'10.251.10.1'};
  const button={disabled:true,dataset:{locked:'false'}};
  const form={querySelector:selector=>selector.includes('configure')?button:selector.includes('address')?address:selector.includes('gateway')?gateway:null,
    addEventListener:(name,fn)=>{input=fn;}};
  const poll={dataset:{state:JSON.stringify({busy:false,job:{},leases:{}}),statusUrl:'/settings/network-setup/status'}};
  const ctx={window:{},document:{addEventListener(){},querySelector:()=>poll,querySelectorAll:selector=>selector==='[data-setup-form]'?[form]:[]},
    setInterval:fn=>{pollTick=fn;return 1;},clearInterval(){},fetch:async()=>{requests++;}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  ctx.window.TrafficGen.networkSetup();input();assert.equal(button.disabled,false);
  await pollTick();assert.equal(requests,0);
  gateway.value='';input();assert.equal(button.disabled,true);
});


test('release checks show immediate progress and require the new job before revealing results',()=>{
  const fields={};for(const scope of ['simulator','target'])fields['release-check-'+scope]={dataset:{releaseCheckIndicator:scope},hidden:true,className:'',textContent:'',setAttribute(name,value){this[name]=value;}};
  const ctx={window:{},document:{addEventListener(){},getElementById:id=>fields[id]}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const ui=ctx.window.TrafficGen,sim=fields['release-check-simulator'];
  ui.beginReleaseCheck('simulator','old-job');
  assert.equal(sim.hidden,false);assert.match(sim.textContent,/Checking simulator/);assert.equal(sim['aria-busy'],'true');
  ui.releaseCheckState({busy:false,job:{id:'old-job',action:'check_update',state:'completed'},release:{tag:'v0.1.0',available:false}});
  assert.match(sim.className,/checking/);
  ui.releaseCheckState({busy:true,job:{id:'new-job',action:'check_update',state:'running'}});
  ui.releaseCheckState({busy:false,job:{id:'new-job',action:'check_update',state:'completed'},release:{tag:'v0.13.0',available:true}});
  assert.match(sim.textContent,/Update available: v0.13.0/);assert.match(sim.className,/success/);assert.equal(sim['aria-busy'],'false');
  assert.equal(fields['release-check-target'].hidden,true);
});

test('target release feedback distinguishes up-to-date from failure with cached release data',()=>{
  const element={dataset:{releaseCheckIndicator:'target'},hidden:true,className:'',textContent:'',setAttribute(){}};
  const ctx={window:{},document:{addEventListener(){},getElementById:id=>id==='release-check-target'?element:null}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const ui=ctx.window.TrafficGen;
  ui.beginReleaseCheck('target','old');
  ui.releaseCheckState({busy:false,job:{id:'new',action:'target_check',state:'completed'},target:{release:{tag:'v0.13.0',available:false}}});
  assert.match(element.textContent,/Target is up to date/);
  ui.beginReleaseCheck('target','new');
  ui.releaseCheckState({busy:false,job:{id:'failed',action:'target_check',state:'failed',message:'Cannot reach target'},target:{release:{tag:'v0.13.0',available:false}}});
  assert.match(element.className,/error/);assert.match(element.textContent,/Cannot reach target/);assert.doesNotMatch(element.textContent,/up to date/);
});

for(const scope of ['simulator','target'])for(const observeBusy of [false,true])test(scope+' check updates controls without refresh (busy poll: '+observeBusy+')',async()=>{
  let submit,pollTick,posts=0;
  const fields={};
  for(const id of ['system-job','release-status','simulator-release-tag','check-update','install-update','target-status','target-release-status','target-job-status','target-release-tag','release-check-'+scope])
    fields[id]={textContent:'',value:'',disabled:true,dataset:{},setAttribute(){}};
  fields['system-job'].dataset={jobId:'old',busy:'false'};
  fields['release-check-'+scope].dataset.releaseCheckIndicator=scope;
  const targetButtons=['target_status','target_check','target_install'].map(action=>({dataset:{targetAction:action},disabled:true}));
  let state={ready:true,busy:false,workload_active:false,release:{},job:{id:'old'},target:{connected:true,version:'1.0.0',release:{}}};
  class Form{constructor(){this.method='post';this.dataset={releaseCheck:scope};}
    getAttribute(){return '/settings/system/action';}querySelectorAll(){return [];}
    setAttribute(){}removeAttribute(){}}
  const ctx={window:{},document:{addEventListener:(_,fn)=>submit=fn,getElementById:id=>fields[id],querySelectorAll:()=>targetButtons},
    HTMLFormElement:Form,FormData:class{},URL,location:{href:'https://simulator.example/settings/system',origin:'https://simulator.example'},
    setInterval:fn=>{pollTick=fn;return 1},clearInterval(){},
    fetch:async(url,options)=>{
      if(options.method==='POST'){posts++;assert.equal(options.headers.Accept,'application/json');return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({job_id:'new'})};}
      assert.equal(url,'/settings/system/data');return {ok:true,json:async()=>state};
    }};
  // No DOMParser, workspace replacement, or reload is available: checks must not use them.
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('trafficgen/static/app.js','utf8'),ctx);
  const ui=ctx.window.TrafficGen;ui.systemPage();await new Promise(setImmediate);
  const install=scope==='simulator'?fields['install-update']:targetButtons[2];
  assert.equal(install.disabled,true);
  await submit({target:new Form(),defaultPrevented:false,preventDefault(){}});
  assert.equal(posts,1);assert.equal(install.disabled,true);
  const release={tag:'netem-traffic-simulator-v1.1.0',display_tag:'v1.1.0',available:true};
  state={...state,job:{id:'unrelated',action:scope==='simulator'?'check_update':'target_check',state:'completed'}};
  if(scope==='simulator')state.release=release;else state.target.release=release;
  await pollTick();assert.equal(install.disabled,true,'an unrelated check cannot finish this request');
  if(observeBusy){state.busy=true;state.job={...state.job,id:'new',state:'running'};await pollTick();assert.equal(install.disabled,true);}
  state.busy=false;state.job={...state.job,id:'new',state:'completed'};await pollTick();
  assert.equal(install.disabled,false);
  assert.equal(fields[scope==='simulator'?'simulator-release-tag':'target-release-tag'].value,release.tag);
  assert.match(fields[scope==='simulator'?'release-status':'target-release-status'].textContent,/v1.1.0 · update available/);
  assert.match(fields['release-check-'+scope].textContent,/Update available/);
  state.workload_active=true;await pollTick();assert.equal(install.disabled,true);
  state.workload_active=false;release.available=false;await pollTick();assert.equal(install.disabled,true);
  assert.match(fields[scope==='simulator'?'release-status':'target-release-status'].textContent,/up to date/);
});
