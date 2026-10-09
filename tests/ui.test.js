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
