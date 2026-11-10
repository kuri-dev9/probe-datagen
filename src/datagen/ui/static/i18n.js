
const LANG_KEY='gp-ui-lang';
const LANG=localStorage.getItem(LANG_KEY)==='ja'?'ja':'ko';
document.documentElement.lang=LANG;
function T(ko, a){
  let s=(LANG==='ja' && JA[ko]!=null) ? JA[ko] : ko;
  return a ? s.replace(/\{(\w+)\}/g,(m,k)=>(k in a ? a[k] : m)) : s;
}
function applyLang(root){
  if(LANG!=='ja') return;
  root=root||document.body;
  const w=document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for(let n=w.nextNode(); n; n=w.nextNode()){
    const t=n.nodeValue.trim();
    if(t && JA[t]!=null) n.nodeValue=n.nodeValue.replace(t, JA[t]);
  }
  for(const e of root.querySelectorAll('[title],[placeholder],[aria-label]'))
    for(const a of ['title','placeholder','aria-label']){
      const v=e.getAttribute(a); if(v && JA[v]!=null) e.setAttribute(a, JA[v]); }
  if(JA[document.title]!=null) document.title=JA[document.title];
}
function langSelect(id){
  const s=document.getElementById(id); if(!s) return;
  s.value=LANG;
  s.onchange=()=>{ localStorage.setItem(LANG_KEY, s.value); location.reload(); };
}
