const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(tag,id=''){this.tagName=tag.toUpperCase();this.id=id;this.children=[];this.dataset={};this.style={setProperty(){}};this.hidden=false;this.value='';this._text='';this.disabled=false;this.className='';this.scrollHeight=300;this.scrollTop=0;this.clientHeight=300;this.classList={values:new Set(),add:x=>this.classList.values.add(x),remove:x=>this.classList.values.delete(x),contains:x=>this.classList.values.has(x),toggle(){}}}
 append(...values){for(const value of values)if(value&&value.tagName==='#FRAGMENT')this.children.push(...value.children);else this.children.push(value)}
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
let ticket=false,messages=[],resolveSend;
const response=(obj,status=200)=>({ok:status>=200&&status<300,status,json:async()=>obj,text:async()=>JSON.stringify(obj),blob:async()=>null});
async function fetch(url,opts={}){
 if(url==='/api/catalog')return response({products:[]});
 if(url==='/api/support/info')return response({user_id:42,is_admin:false,configured:true});
 if(url==='/api/support/tickets'&&opts.method==='POST'){ticket=true;return response({number:'123456789'})}
 if(url.startsWith('/api/support/tickets')&&!url.includes('/123456789'))return response({tickets:ticket?[{number:'123456789',owner_id:42}]:[]});
 if(url.endsWith('/avatar'))return response({},204);
 if(url.endsWith('/messages'))return new Promise(resolve=>{resolveSend=()=>{messages=[{message_id:'a',role:'user',body:'Мой вопрос'},{message_id:'b',role:'assistant',body:'** Ответ **'}];resolve(response({ok:true,answer:'** Ответ **'}))}});
 if(url.startsWith('/api/support/tickets/'))return response({ticket:{owner_id:42},messages,has_more:false});
 return response({});
}
const context={document,window,fetch,location:{search:'',href:''},URL,URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval,Map,Array,JSON,Promise};
const source=html.rsplit?null:html.slice(html.lastIndexOf('<script>')+8,html.lastIndexOf('</script>'));vm.runInNewContext(source,context);
(async()=>{await ids.support.onclick();await ids['support-new'].onclick();ids['support-input'].value='Мой вопрос';const pending=ids['support-send-form'].onsubmit({preventDefault(){}});await new Promise(r=>setTimeout(r,10));let box=ids['support-messages'];assert(box.textContent.includes('Мой вопрос'),'user message appears immediately');assert(box.querySelector('[data-typing]'),'typing indicator appears before response');resolveSend();await pending;box=ids['support-messages'];assert(box.textContent.includes('Ответ'));assert(!box.textContent.includes('**'));assert(!box.querySelector('[data-typing]'));assert(!box.textContent.includes('Участник'));assert(!box.textContent.includes('ИИ-поддержка'));ids['support-back'].onclick();console.log('support chat optimistic send and response OK')})().catch(e=>{console.error(e);process.exitCode=1});
