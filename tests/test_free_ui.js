const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(tag,id=''){this.tagName=tag.toUpperCase();this.id=id;this.children=[];this.dataset={};this.style={setProperty(){}};this.hidden=false;this.value='';this._text='';this.disabled=false;this.className='';this.scrollHeight=300;this.scrollTop=0;this.clientHeight=300;this.classList={values:new Set(),add:x=>this.classList.values.add(x),remove:x=>this.classList.values.delete(x),contains:x=>this.classList.values.has(x),toggle(){}}}
 append(...values){for(const value of values)if(value&&value.tagName==='#FRAGMENT')this.children.push(...value.children);else {this.children.push(value);if(this.id==='creator-model'&&!this.value&&value.value)this.value=value.value}}
 prepend(value){if(value.tagName==='#FRAGMENT')this.children.unshift(...value.children);else this.children.unshift(value)}
 replaceChildren(...v){this.children=[];this._text='';this.append(...v)}
 set textContent(v){this.children=[];this._text=String(v)}
 get textContent(){return this._text+this.children.map(x=>x.textContent).join('')}
 setAttribute(k,v){this[k]=v} addEventListener(){} focus(){} remove(){}
 matches(s){if(s==='[data-typing]')return this.dataset.typing!==undefined;if(s[0]==='.')return this.className.split(' ').includes(s.slice(1));return false}
 querySelector(s){for(const v of this.children){if(v.matches(s))return v;const found=v.querySelector(s);if(found)return found}return null}
 querySelectorAll(s){return this.children.flatMap(v=>[...(v.matches(s)?[v]:[]),...v.querySelectorAll(s)])}
}
const html=fs.readFileSync('miniapp.html','utf8');let ids={};for(const m of html.matchAll(/\bid="([^"]+)"/g))ids[m[1]]=new Element('div',m[1]);
const document={body:new Element('body'),getElementById(id){return ids[id]},querySelectorAll(){return []},createElement(tag){return new Element(tag)},createDocumentFragment(){return new Element('#fragment')},createTextNode(t){let x=new Element('#text');x.textContent=t;return x}};
const formatter=require('../support_render.js');const tg={ready(){},expand(){},initData:'signed'},window={Telegram:{WebApp:tg},RakSupportFormatter:formatter,scrollTo(){},matchMedia(){return {matches:false}}};

let freeCalls=0,claims=[];
const response=(obj,status=200)=>({ok:status>=200&&status<300,status,json:async()=>obj,text:async()=>typeof obj==='string'?obj:JSON.stringify(obj)});
async function fetch(url,opts={}){
 if(url==='/api/catalog')return response({products:[]});
 if(url==='/api/ai/free'){
  if(opts.method==='POST'){const model=JSON.parse(opts.body).model;claims.push(model);return model==='bad'?response('Ошибка начисления',500):response({})}
  freeCalls++;
  if(freeCalls===1)return response('500 Internal Server Error\nServer got itself in trouble',500);
  if(freeCalls===2)return response('Подпишитесь на обязательные каналы',403);
  return response({models:[{model:'glm',name:'GLM 5.3',amount:4000000,balance:0,next_claim_at:0,pending:false,configured:true},{model:'bad',name:'Ошибка',amount:2000000,balance:0,next_claim_at:0,pending:false,configured:true},{model:'waiting',name:'Ожидание',amount:2000000,balance:0,next_claim_at:Math.floor(Date.now()/1000)+3600,pending:false,configured:true}]});
 }
 return response({});
}
const context={document,window,fetch,location:{search:'',href:''},URL,URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval:()=>1,clearInterval,Map,Array,JSON,Promise};
const source=html.slice(html.lastIndexOf('<script>')+8,html.lastIndexOf('</script>'));vm.runInNewContext(source,context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert(ids['free-models'].textContent.includes('Не удалось загрузить токены'));
 let retry=ids['free-models'].querySelector('.free-claim');assert(retry);await retry.onclick();
 assert(ids['free-models'].textContent.includes('Подпишитесь на обязательные каналы'));
 retry=ids['free-models'].querySelector('.free-claim');assert(retry);await retry.onclick();
 assert(ids['free-models'].textContent.includes('GLM 5.3'));assert(ids['free-models'].textContent.includes('Получить'));
 assert.equal(ids['free-models'].querySelectorAll('.free-claim').length,1);
 await ids['free-models'].querySelector('.free-claim').onclick();
 assert.deepStrictEqual(claims,['glm','bad']);
 assert(ids['free-status'].textContent.includes('Начислено: GLM 5.3'));
 assert(ids['free-status'].textContent.includes('Не удалось начислить: Ошибка'));
 console.log('free tokens retry, combined claim, cooldown and partial failure OK');
})().catch(e=>{console.error(e);process.exitCode=1});
