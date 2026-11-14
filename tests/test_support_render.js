const assert=require('assert');
const render=require('../support_render.js').render;
class Node {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this._text='';this.className=''}
  append(...items){this.children.push(...items)}
  replaceChildren(...items){this.children=[...items];this._text=''}
  set textContent(value){this._text=String(value);this.children=[]}
  get textContent(){return this._text+this.children.map(x=>x.textContent).join('')}
}
const doc={createElement(tag){return new Node(tag)},createTextNode(text){let n=new Node('#text');n.textContent=text;return n}};
function flatten(node){return [node,...node.children.flatMap(flatten)]}
const box=new Node('div');
render(doc,box,'Привет, ** мир **!\n### Настройка\n- `python`\n- [Документация](https://example.com/help)\n<script>alert(1)</script>\n[x](javascript:alert(1))');
const all=flatten(box);
assert(all.some(n=>n.tagName==='STRONG'&&n.textContent==='мир'));
assert(all.some(n=>n.tagName==='STRONG'&&n.textContent==='Настройка'));
assert(all.some(n=>n.tagName==='CODE'&&n.textContent==='python'));
assert(all.some(n=>n.tagName==='UL')&&all.filter(n=>n.tagName==='LI').length===2);
assert(all.some(n=>n.tagName==='A'&&n.href==='https://example.com/help'&&n.rel==='noopener noreferrer'));
assert(!all.some(n=>n.tagName==='SCRIPT'));
assert(box.textContent.includes('<script>alert(1)</script>'));
assert(!all.some(n=>n.tagName==='A'&&String(n.href).startsWith('javascript:')));
render(doc,box,'```lua\nprint("<ok>")\n```');
assert(flatten(box).some(n=>n.tagName==='PRE'&&n.textContent==='print("<ok>")'));
console.log('safe Markdown rendering OK');
