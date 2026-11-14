/* Render a small safe subset of assistant Markdown without interpreting HTML. */
(function(root){
'use strict';
function textNode(doc,value){return doc.createTextNode(value)}
function inline(doc,parent,text,depth){
  if(depth>3){parent.append(textNode(doc,text));return}
  const pattern=/\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*([^*\n]+)\*/g;
  let last=0,match;
  while((match=pattern.exec(text))!==null){
    parent.append(textNode(doc,text.slice(last,match.index)));
    if(match[1]!==undefined||match[2]!==undefined){
      const el=doc.createElement('strong');inline(doc,el,(match[1]??match[2]).trim(),depth+1);parent.append(el);
    }else if(match[3]!==undefined){const el=doc.createElement('code');el.textContent=match[3];parent.append(el)}
    else if(match[4]!==undefined){
      const el=doc.createElement('a');el.textContent=match[4];el.href=match[5];el.target='_blank';el.rel='noopener noreferrer';parent.append(el);
    }else{const el=doc.createElement('em');inline(doc,el,match[6].trim(),depth+1);parent.append(el)}
    last=pattern.lastIndex;
  }
  parent.append(textNode(doc,text.slice(last)));
}
function render(doc,target,content){
  target.replaceChildren();const lines=String(content??'').replace(/\r\n?/g,'\n').split('\n');let list=null,inCode=false,code=[];
  const endCode=()=>{const pre=doc.createElement('pre'),c=doc.createElement('code');c.textContent=code.join('\n');pre.append(c);target.append(pre);code=[]};
  for(const line of lines){
    if(/^\s*```/.test(line)){list=null;if(inCode){endCode();inCode=false}else{inCode=true}continue}
    if(inCode){code.push(line);continue}
    const item=line.match(/^\s*([-*•]|\d+[.)])\s+(.+)$/);
    if(item){const kind=/\d/.test(item[1][0])?'ol':'ul';if(!list||list.tagName.toLowerCase()!==kind){list=doc.createElement(kind);target.append(list)}const li=doc.createElement('li');inline(doc,li,item[2],0);list.append(li);continue}
    list=null;
    const heading=line.match(/^\s*#{1,6}\s+(.+)$/);
    if(heading){const el=doc.createElement('strong');el.className='chat-heading';inline(doc,el,heading[1],0);target.append(el);continue}
    const p=doc.createElement('div');if(line==='')p.className='chat-blank';else inline(doc,p,line,0);target.append(p);
  }
  if(inCode)endCode();
}
const api={render};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.RakSupportFormatter=api;
})(typeof window!=='undefined'?window:globalThis);
