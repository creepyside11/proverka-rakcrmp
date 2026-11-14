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
let sent=null, polls=0;
const response=(obj,status=200)=>({ok:status>=200&&status<300,status,json:async()=>obj,text:async()=>JSON.stringify(obj)});
async function fetch(url,opts={}){
 if(url==='/api/catalog')return response({products:[]});
 if(url==='/api/creator/models')return response({models:[{model:'glm',name:'GLM',balance:1000000},{model:'gpt_sol',name:'GPT',balance:500}]});
 if(url==='/api/creator/messages'&&opts.method==='POST'){sent=JSON.parse(opts.body);return response({ok:true,started:true,run_id:'run-one'})}
 if(url==='/api/creator/progress')return response({state:'idle',events:[]});
 if(url==='/api/creator/progress?run=run-one'){polls++;if(polls===1)return response({state:'running',events:[{id:'e1',kind:'file',message:'Читаю файл'}]});return response({state:'done',events:[{id:'e1',kind:'file',message:'Читаю файл'},{id:'e2',kind:'plan',message:'1. Создать файл'}],result:{ok:true,answer:'Готово\n\n```lua\nprint(1)\n```',options:['Ферма','Шахта'],check:null}})}
 if(url==='/api/creator/messages')return response({messages:[]});
 return response({});
}
const context={document,window,fetch,location:{search:'',href:''},URL,URLSearchParams,Intl,console,setTimeout:(fn)=>{fn();return 0},clearTimeout,setInterval,clearInterval,Map,Array,JSON,Promise};
const source=html.slice(html.lastIndexOf('<script>')+8,html.lastIndexOf('</script>'));vm.runInNewContext(source,context);
(async()=>{await ids.creator.onclick();assert.equal(ids['creator-model'].children.length,2);ids['creator-model'].value='gpt_sol';ids['creator-input'].value='Сделай бота';await ids['creator-form'].onsubmit({preventDefault(){}});assert.deepEqual(sent,{text:'Сделай бота',model:'gpt_sol',background:true});assert(ids['creator-messages'].textContent.includes('Готово'));assert(!ids['creator-messages'].textContent.includes('print(1)'));const file=ids['creator-messages'].querySelector('.creator-file-open');assert(file);file.onclick();assert.equal(ids['creator-code-content'].textContent,'print(1)\n');assert.equal(ids['creator-code-modal'].hidden,false);ids['creator-code-close'].onclick();const opt=ids['creator-messages'].querySelector('.creator-option');assert(opt);opt.onclick();assert.equal(ids['creator-input'].value,'Ферма');assert(ids['creator-messages'].textContent.includes('Создать файл'));assert.equal((ids['creator-messages'].textContent.match(/Читаю файл/g)||[]).length,1);assert(!ids['creator-events']);assert.equal(ids['creator-model'].value,'gpt_sol');ids['creator-back'].onclick();console.log('creator UI model selection OK')})().catch(e=>{console.error(e);process.exitCode=1});
