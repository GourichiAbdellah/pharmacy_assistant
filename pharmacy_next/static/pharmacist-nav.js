'use strict';
// Navigation is independent of authentication, data fetching and chat scripts.
(()=>{
  const titles={overview:'Vue d’ensemble',conversations:'Conversations',schedule:'Ouverture et gardes'};
  function screen(name){
    if(!titles[name])name='overview';
    for(const n of document.querySelectorAll('[data-screen]'))n.hidden=n.dataset.screen!==name;
    for(const n of document.querySelectorAll('[data-view]')){n.classList.toggle('is-current',n.dataset.view===name);n.setAttribute('aria-current',n.dataset.view===name?'page':'false');}
    const title=document.getElementById('view-title');if(title)title.textContent=titles[name];
  }
  for(const n of document.querySelectorAll('[data-view]'))n.addEventListener('click',()=>{location.hash=n.dataset.view;screen(n.dataset.view);});
  window.addEventListener('hashchange',()=>screen(location.hash.slice(1)));screen(location.hash.slice(1));
  window.DeskNavigation={screen};
})();
