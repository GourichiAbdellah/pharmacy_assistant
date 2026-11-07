'use strict';
window.PH = (() => {
  function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
  function report(error){const n=document.getElementById('desk-error')||document.getElementById('status');if(n)n.textContent=error.message||'Une erreur est survenue. Réessaie.';}
  function button(label, action, cls) { const b=el('button',label,cls); b.type='button'; b.onclick=async()=>{b.disabled=true;try{await action();}catch(e){report(e);}finally{b.disabled=false;}}; return b; }
  const storage={get(key){try{return sessionStorage.getItem(key)||'';}catch{return '';}},set(key,value){try{sessionStorage.setItem(key,value);}catch{}},remove(key){try{sessionStorage.removeItem(key);}catch{}}};
  async function api(path, {token, body, method='GET'}={}) {
    const headers={}; if(token)headers.Authorization='Bearer '+token;
    if(body!==undefined)headers['Content-Type']='application/json';
    let r;
    try{r=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',signal:AbortSignal.timeout(20000)});}
    catch(e){throw new Error(e.name==='TimeoutError'?'Le serveur met trop de temps à répondre. Réessaie.':'Connexion au serveur interrompue. Réessaie.');}
    let data;
    try{data=r.status===204?null:await r.json();}catch{throw new Error('Réponse du serveur illisible. Vérifie que la page et le backend utilisent la même version.');}
    if(!r.ok){const e=new Error(typeof data?.detail==='string'?data.detail:'La demande n’a pas pu être traitée. Vérifie les champs.');e.status=r.status;throw e;}
    return data;
  }
  const date=v=>v?new Intl.DateTimeFormat('fr-MA',{timeZone:'Africa/Casablanca',dateStyle:'short',timeStyle:'short'}).format(new Date(v)):'—';
  const medication=m=>[m.nom,[m.dosage,m.unite_dosage].filter(Boolean).join(' '),m.forme,m.presentation].filter(Boolean).join(' · ');
  const labels={draft:'Brouillon — non envoyé',pending:'En attente de réponse sur la disponibilité',available:'Disponible selon le pharmacien',unavailable:'Indisponible selon le pharmacien',needs_details:'Une précision est demandée',expired:'Demande expirée',cancelled:'Demande annulée'};
  function requestCard(r) {
    const box=el('article',undefined,'ph-card');box.dataset.availabilityId=r.id;
    if(r.is_demo)box.append(el('span','DÉMONSTRATION · aucune pharmacie réelle','ph-demo'));
    box.append(el('h3',r.pharmacy_name),el('p',medication(r.medication)),el('p',`${r.boxes} boîte(s) · ${labels[r.status]||r.status}`,'ph-badge'));
    if(r.comment)box.append(el('p','Pharmacien : '+r.comment));
    if(r.patient_note)box.append(el('p','Précision transmise : '+r.patient_note));
    box.append(el('p',`${r.response_at?'Réponse le '+date(r.response_at)+' · ':''}Expiration : ${date(r.expires_at)} (heure du Maroc)`,'ph-muted'));
    if(r.status==='available')box.append(el('p','Disponibilité observée à la date de réponse. Aucune réservation.','ph-muted'));
    return box;
  }
  function link(label,href) { const a=el('a',label);const url=new URL(href,location.origin);if(url.protocol!=='https:')return el('span',label);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';return a; }
  function scheduleText(p){if(!p.regular_hours)return 'Horaires habituels non renseignés.';const h=p.regular_hours;return h.open_24h?'Ouverture annoncée 24h/24, 7j/7.':`Horaires habituels : ${h.opens_at}–${h.closes_at} · ${h.days_of_week.map(i=>['lun.','mar.','mer.','jeu.','ven.','sam.','dim.'][i]).join(', ')}.`;}
  function dutyText(d){return d.duty_type==='24h'?`Garde 24h/24 · du ${date(d.starts_at)} au ${date(d.ends_at)}`:`Garde ${d.duty_type==='night'?'de nuit':'de jour'} · du ${date(d.starts_at)} au ${date(d.ends_at)}`;}
  return {el,button,api,date,medication,requestCard,link,storage,report,scheduleText,dutyText};
})();
