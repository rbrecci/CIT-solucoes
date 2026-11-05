'use strict';
(()=>{
const key='cti-theme',root=document.documentElement;
const saved=localStorage.getItem(key);
root.dataset.theme=saved==='dark'||saved==='light'?saved:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
function paint(){const b=document.getElementById('theme-toggle'),dark=root.dataset.theme==='dark';if(!b)return;b.textContent=dark?'☀':'☾';b.setAttribute('aria-pressed',String(dark));b.title=dark?'Voltar ao modo claro':'Ativar modo escuro';}
document.addEventListener('DOMContentLoaded',paint);
document.addEventListener('click',e=>{if(!e.target.closest('#theme-toggle'))return;const next=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=next;localStorage.setItem(key,next);paint();});
})();
