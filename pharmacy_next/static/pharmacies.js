'use strict';
window.pharmacyUi = (() => {
  const {el,button,api,date,medication,requestCard,link}=PH;
  let enabled=false,isDemo=false,directoryProvider='local',position=null,selected=[],token=PH.storage.get('pharmacy_patient_token')||'',identityPromise=null;
  let panel,results,list,error,selectedLabel,city,district,onDuty,opened,positionLabel;
  let human=null,composerLocked=false,hasPhoto=false;
  const relayConsent=new Map();
  function syncComposer(locked,photo){
    if(typeof locked==='boolean')composerLocked=locked;
    if(typeof photo==='boolean')hasPhoto=photo;
    const live=!!human?.isLive;
    for(const id of ['upload','record','upload-photo']){const n=document.getElementById(id);if(n)n.disabled=live||composerLocked||(id!=='upload-photo'&&hasPhoto);}
    const input=document.getElementById('message');if(input){input.maxLength=live?2000:5000;input.placeholder=live?'Votre message au pharmacien…':'Écrivez votre question…';}
  }
  async function showHuman(h){
    if(!human){
      const root=el('section');root.id='patient-relay';document.getElementById('messages').append(root);
      human=HumanRelay.thread(root,{role:'patient',getToken:()=>token,onState:syncComposer});
    }
    await human.open(h);syncComposer();
  }
  async function recoverHuman(){
    if(!token||!enabled)return;
    if(human?.isLive){await human.refresh();syncComposer();return;}
    const data=await api('/api/v1/availability/handoffs',{token});
    if(data.handoffs.length)await showHuman(data.handoffs[0]);
  }
  function fail(e){const message=e.message||'Connexion interrompue.';if(error)error.textContent=message;const status=document.getElementById('status');if(status&&(!panel||!panel.open))status.textContent=message;}
  async function attempt(fn){try{if(error)error.textContent='';return await fn();}catch(e){fail(e);}}
  async function identity(){
    if(token)return token;
    if(!identityPromise)identityPromise=api('/api/v1/availability/identity',{method:'POST'}).then(data=>{token=data.token;PH.storage.set('pharmacy_patient_token',token);return token;}).finally(()=>{identityPromise=null;});
    return identityPromise;
  }
  function selection(){
    selectedLabel.textContent=selected.length===1?'Médicament retenu dans la conversation : '+medication(selected[0]):'Demandez la disponibilité dans le chat : l’assistant préparera la demande et vous posera une question si nécessaire.';
  }
  function askAssistant(message){
    if(composerLocked)throw new Error('L’assistant prépare déjà une réponse. Attends la fin pour envoyer cette demande.');
    if(panel?.open)panel.close();
    document.getElementById('message').value=message;document.getElementById('composer').requestSubmit();
  }
  function inlineDraft(r){
    const target=el('div',undefined,'ph-inline');target.append(patientCard(r));document.getElementById('messages').append(target);
    if(panel?.open)panel.close();target.scrollIntoView({block:'end'});
  }
  async function sharePosition(){
    if(!navigator.geolocation)throw new Error('La géolocalisation est indisponible. Indique ta ville et ton quartier dans le chat.');
    let p;
    try{p=await new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(resolve,reject,{timeout:10000,maximumAge:60000}));}
    catch{throw new Error('Position non partagée. Tu peux réessayer ou préciser ta ville et ton quartier dans le chat.');}
    position={latitude:p.coords.latitude,longitude:p.coords.longitude};
    if(city){city.value='';district.value='';positionLabel.textContent='Position partagée pour cette page · rayon de 10 km.';}
  }
  function open(){if(!panel.open)panel.showModal();selection();attempt(refresh);}
  function renderPharmacies(data,target){
    target.replaceChildren(el('p',data.coverage_note,'ph-muted'),el('p',data.distance_note,'ph-muted'));
    if(data.provider_status?.message)target.append(el('p',data.provider_status.message,'ph-muted'));
    if(data.local_warning)target.append(el('p',data.local_warning,'ph-error'));
    if(data.duty_coverage_note&&!data.is_demo)target.append(el('p',data.duty_coverage_note,'ph-muted'));
    if(data.more_results_possible||data.truncated)target.append(el('p','Liste partielle : précise le quartier ou utilise ta position pour affiner.','ph-muted'));
    if(!data.results.length)target.append(el('p','Aucun résultat vérifié pour ces critères. Cela ne signifie pas qu’il n’y a aucune pharmacie dans la zone.'));
    for(const [index,p] of data.results.entries()){
      const card=el('article',undefined,'ph-card');
      if(p.is_demo)card.append(el('span','PHARMACIE FICTIVE','ph-demo'));
      const identityBox=el('div',undefined,p.source==='google_places'?'ph-google-content':'ph-directory-content');
      identityBox.append(el('h3',`${index+1}. ${p.name}`),el('p',[p.address,p.district,p.city].filter(Boolean).join(' · ')));
      if(p.distance_km!==null)identityBox.append(el('p',`${p.distance_km} km à vol d’oiseau`));
      if(!p.is_demo&&p.maps_url)identityBox.append(link('Itinéraire',p.maps_url));
      if(p.source==='google_places'){
        const attribution=el('p','Google Maps','ph-google-attribution');attribution.setAttribute('translate','no');identityBox.append(attribution);
        for(const a of p.attributions||[]){const credit=el('p',undefined,'ph-muted');credit.append(a.url?link(a.label,a.url):el('span',a.label));identityBox.append(credit);}
      }else identityBox.append(el('p',p.source_label||'Annuaire du prototype','ph-muted'));
      card.append(identityBox);
      if(p.registered_name)card.append(el('p','Fiche vérifiée dans notre annuaire : '+p.registered_name,'ph-muted'));
      card.append(el('p',p.duty_status==='scheduled_now'?'Garde annoncée maintenant':'Garde non confirmée','ph-badge'));
      if(p.duty)card.append(el('p',PH.dutyText(p.duty)),el('p',`${p.duty.source_label} · vérifiée le ${date(p.duty.source_checked_at)}`,'ph-muted'));
      card.append(el('p',PH.scheduleText(p),'ph-muted'));
      if(p.duty?.source_url)card.append(link('Source du planning',p.duty.source_url));
      card.append(el('p',p.duty_note,'ph-muted'));
      const opening={unknown:'Ouverture non confirmée',confirmed_open:'Ouverture confirmée par la pharmacie',confirmed_closed:'Fermeture déclarée par la pharmacie'};
      card.append(el('p',opening[p.opening_status]),el('p','Stock du médicament : à vérifier','ph-muted'));
      if(p.opening_status!=='unknown')card.append(el('p','Confirmation valable jusqu’au '+date(p.opening_valid_until),'ph-muted'));
      const actions=el('div',undefined,'ph-row');
      if(!p.is_demo&&p.phone){const a=el('a','Appeler');a.href='tel:'+p.phone.replace(/[^+0-9]/g,'');actions.append(a);}
      if(p.accepting_requests)actions.append(button('Vérifier avec le pharmacien',()=>attempt(async()=>{
        if(selected.length!==1){askAssistant('Je veux vérifier la disponibilité du médicament dont on parle auprès de la pharmacie partenaire la plus proche. Peux-tu préparer la demande ?');return;}
        const draft=await api('/api/v1/availability/drafts',{method:'POST',token:await identity(),body:{pharmacy_id:p.id,medication_code:selected[0].code,boxes:1}});
        inlineDraft(draft);
      }),'ph-primary'));
      else actions.append(el('span','Demandes en ligne indisponibles','ph-muted'));
      card.append(actions);target.append(card);
    }
  }
  function patientCard(r){
    const card=requestCard(r),actions=el('div',undefined,'ph-row');
    const update=async(action,body)=>attempt(async()=>{const fresh=await api(`/api/v1/availability/requests/${r.id}/${action}`,{method:'POST',token:await identity(),body});replaceEverywhere(fresh);await refresh();});
    if(r.status==='draft'){
      card.append(el('p','Seuls le médicament, sa présentation et le nombre de boîtes seront transmis. Vérifie ce récapitulatif avant de confirmer.','ph-muted'));
      const consent=el('input');consent.type='checkbox';consent.checked=relayConsent.get(r.id)??true;
      consent.onchange=()=>relayConsent.set(r.id,consent.checked);
      const label=el('label','Continuer avec le pharmacien dans ce chat. Le produit, la quantité et les nouveaux messages seront partagés ; mon historique avec l’IA reste privé. ');label.prepend(consent);card.append(label);
      actions.append(button('Confirmer et envoyer',()=>attempt(async()=>{
        const wantsRelay=consent.checked;
        const fresh=await api(`/api/v1/availability/requests/${r.id}/confirm`,{method:'POST',token:await identity(),body:{confirmed:true}});
        replaceEverywhere(fresh);await refresh();
        if(wantsRelay){const h=await api(`/api/v1/availability/requests/${r.id}/handoff`,{method:'POST',token:await identity(),body:{confirmed:true}});await showHuman(h);panel.close();document.getElementById('patient-relay').scrollIntoView({block:'end'});}
      }),'ph-primary'));
    }
    if(['pending','available','unavailable','needs_details'].includes(r.status))actions.append(button('Échanger avec le pharmacien',()=>attempt(async()=>{
      if(!confirm('Partager le produit, la quantité et les nouveaux messages avec cette pharmacie ? Votre historique avec l’assistant reste privé.'))return;
      const h=await api(`/api/v1/availability/requests/${r.id}/handoff`,{method:'POST',token:await identity(),body:{confirmed:true}});await showHuman(h);panel.close();document.getElementById('patient-relay').scrollIntoView({block:'end'});
    })));
    if(r.status==='needs_details'){
      const input=el('textarea');input.maxLength=500;input.placeholder='Précision sur le produit ou la quantité uniquement';input.setAttribute('aria-label','Votre précision');card.append(input);
      actions.append(button('Envoyer la précision',()=>input.value.trim()?update('clarify',{text:input.value.trim()}):Promise.resolve()));
    }
    if(!['cancelled','expired'].includes(r.status))actions.append(button(r.status==='draft'?'Supprimer le brouillon':'Annuler la demande',()=>update('cancel')));
    card.append(actions);return card;
  }
  function replaceEverywhere(r){for(const node of document.querySelectorAll(`[data-availability-id="${r.id}"]`)){if(!(node.contains(document.activeElement)&&document.activeElement.tagName==='TEXTAREA'))node.replaceWith(patientCard(r));}}
  async function refresh(){
    const data=await api('/api/v1/availability/requests',{token:await identity()});
    for(const r of data.requests)replaceEverywhere(r);
    if(!(list.contains(document.activeElement)&&document.activeElement.tagName==='TEXTAREA')){list.replaceChildren(...data.requests.map(patientCard));if(!data.requests.length)list.append(el('p','Aucune demande pour cet accès.'));}
  }
  function build(){
    const launch=button('Pharmacies',open,'ph-launch');launch.id='pharmacies-open';document.querySelector('header').append(launch);
    panel=el('dialog',undefined,'ph-panel');panel.id='pharmacies-panel';panel.setAttribute('aria-labelledby','ph-title');
    const top=el('div',undefined,'ph-row');const title=el('h2','Pharmacies et demandes');title.id='ph-title';top.append(title,button('Fermer',()=>panel.close()));panel.append(top);
    if(isDemo)panel.append(el('p','DÉMONSTRATION · adresses et plannings fictifs · recherche Rabat','ph-demo'));
    if(directoryProvider==='google_places'){
      panel.append(el('p','Lors d’une recherche, la zone saisie ou la position partagée est envoyée à Google Maps. Le médicament et la conversation ne sont pas transmis à Google.','ph-muted'));
      const legal=el('p',undefined,'ph-muted');legal.append(link('Conditions Google Maps','https://maps.google.com/help/terms_maps/'),' · ',link('Confidentialité Google','https://policies.google.com/privacy'));panel.append(legal);
    }
    panel.append(el('p','Gardes annoncées et confirmations d’ouverture sont distinctes. Aucun stock n’est déduit.','ph-muted'));
    error=el('p','','ph-error');error.setAttribute('role','status');panel.append(error);
    const search=el('form');search.id='ph-search';const fields=el('div',undefined,'ph-row');
    const field=(label,input)=>{const l=el('label',label);l.append(input);return l;};
    city=el('input');city.placeholder='Rabat';city.maxLength=100;city.id='ph-city';city.autocomplete='address-level2';district=el('input');district.maxLength=100;district.placeholder='Facultatif';
    fields.append(field('Ville',city),field('Quartier',district));search.append(fields);
    const controls=el('div',undefined,'ph-row');onDuty=el('input');onDuty.type='checkbox';opened=el('input');opened.type='checkbox';
    controls.append(field('De garde maintenant ',onDuty),field('Ouverture confirmée ',opened));search.append(controls);
    positionLabel=el('p','Position non partagée.','ph-muted');
    search.append(button('Utiliser ma position',()=>attempt(sharePosition)),button('Effacer ma position',()=>{position=null;positionLabel.textContent='Position effacée. Indique une ville.';}),positionLabel);
    const submit=el('button','Rechercher');submit.type='submit';search.append(submit);
    search.onsubmit=e=>{e.preventDefault();attempt(async()=>{submit.disabled=true;try{const data=await api('/api/v1/pharmacies/search',{method:'POST',body:{...(position||{}),city:city.value.trim()||null,district:district.value.trim()||null,on_duty_only:onDuty.checked,open_confirmed_only:opened.checked}});renderPharmacies(data,results);}finally{submit.disabled=false;}});};
    panel.append(search);selectedLabel=el('p','','ph-muted');panel.append(selectedLabel);
    results=el('div');results.id='ph-results';panel.append(results,el('h2','Mes demandes'),button('Actualiser les demandes',()=>attempt(refresh)));
    panel.append(el('p','Suivi conservé dans cet onglet pendant 7 jours. Ne partage pas cet accès. Les brouillons expirent après 10 minutes et les demandes après 30 minutes.','ph-muted'));
    list=el('div');list.id='ph-requests';panel.append(list);
    panel.append(button('Réinitialiser mon accès',()=>{if(confirm('Créer un nouvel accès ? Le suivi des demandes de cet onglet sera perdu et la discussion recommencera.')){PH.storage.remove('pharmacy_patient_token');location.reload();}}));
    document.body.append(panel);
    setInterval(()=>{if(!document.hidden&&token)attempt(refresh);},15000);
    setInterval(()=>{if(!document.hidden&&token)recoverHuman().catch(fail);},3000);
  }
  const ready=api('/api/v1/pharmacies/config').then(async c=>{enabled=!!c.enabled;isDemo=!!c.is_demo;directoryProvider=c.directory_provider||'local';if(enabled){build();await recoverHuman();}}).catch(()=>{});
  return {
    async chatOptions(){await ready;if(!enabled)return {headers:{},location:null};return {headers:{Authorization:'Bearer '+await identity()},location:position};},
    headers(){return token?{Authorization:'Bearer '+token}:{};},
    reset(){selected=[];human=null;if(selectedLabel)selection();syncComposer();},
    syncComposer,
    isHumanActive(){return !!human?.isLive;},
    async interceptMessage(payload,kind){
      await ready;if(!enabled)return false;
      await recoverHuman();
      if(!human?.isLive)return false;
      if(kind!=='text')throw new Error('Le relais avec le pharmacien est textuel. Revenez à l’assistant pour utiliser un vocal ou une photo.');
      await human.send(payload);return true;
    },
    consume(result){if(!enabled)return;selected=result.selected_presentations||[];selection();for(const action of result.pharmacy_actions||[]){
      const target=el('div',undefined,'ph-inline');
      if(action.type==='pharmacies')renderPharmacies(action.data,target);
      if(action.type==='draft')target.append(patientCard(action.data));
      if(action.type==='requests')for(const r of action.data)target.append(patientCard(r));
      if(action.type==='location_request'){
        target.append(el('p','Pour trouver le partenaire le plus proche, partage ta position. Tu peux aussi préciser ta ville et ton quartier dans la discussion.'));
        target.append(button('Partager ma position',()=>attempt(async()=>{await sharePosition();askAssistant('Voici ma position. Prépare la demande de disponibilité pour le médicament dont on parle auprès de la pharmacie partenaire la plus proche.');}),'ph-primary'));
      }
      document.getElementById('messages').append(target);
    }},
  };
})();
