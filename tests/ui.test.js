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
});
