const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('miniapp.html','utf8');
const start=html.indexOf('async function confirmAccountsReplacement()');
const end=html.indexOf('let luaSession=',start);
assert(start>=0 && end>start,'update confirmation is wired into the real page');
const code=html.slice(start,end), inputs={
 'vps-user':{value:'root',checkValidity:()=>true},
 'vps-password':{value:'secret',checkValidity:()=>true},
 'vps-confirm':{checked:true}
};
let ok=false,requests=[],prompts=0;
const buttons=['update_bot','stop_bot','resources'].map(action=>({dataset:{vpsAction:action},disabled:false}));
assert(html.includes('data-vps-action="resources"'),'resource button must be present on the actual page');
const context={document:{querySelectorAll:()=>buttons},window:{confirm:()=>ok},tg:{showConfirm:(msg,callback)=>{prompts++;callback(ok)}},
 $:id=>inputs[id],installProbe:'probe',installStatus:{textContent:''},installVerify:{hidden:false},installRun:{hidden:true},installJob:null,
 pollInstall:()=>{},api:async(path,opts)=>{requests.push(JSON.parse(opts.body));return {job_id:'job'}},Promise};
vm.runInNewContext(code,context);
(async()=>{
 await buttons[0].onclick();assert.equal(prompts,1);assert.equal(requests.length,0,'cancel must not start an update');
 ok=true;await buttons[0].onclick();assert.equal(requests.length,1);assert.equal(requests[0].action,'update_bot');assert.strictEqual(requests[0].replace_accounts,true);
 context.installProbe='probe';inputs['vps-password'].value='secret';buttons.forEach(x=>x.disabled=false);
 await buttons[1].onclick();assert.equal(prompts,2,'non-update actions do not prompt for accounts');assert.strictEqual(requests[1].replace_accounts,false);
 context.installProbe='probe';inputs['vps-password'].value='secret';buttons.forEach(x=>x.disabled=false);
 await buttons[2].onclick();assert.equal(prompts,2,'resource check does not prompt to replace accounts');
 assert.equal(requests[2].action,'resources');assert.strictEqual(requests[2].replace_accounts,false);
 console.log('VPS account confirmation flow OK');
})().catch(e=>{console.error(e);process.exitCode=1});
