'use strict';
(()=>{
  if(!window.PH||!window.HumanRelay){document.getElementById('desk-error').textContent='Chargement incomplet de l’espace pharmacien. Rechargez la page avec Ctrl+F5.';return;}
  const {el,button,api,date,requestCard,medication}=PH,$=id=>document.getElementById(id);
  let token=PH.storage.get('pharmacy_partner_token')||'',inflight=null,ready=false,requests=[],threads=[],selectedId=null,conversation=null;
  const call=(path,options={})=>api('/api/v1/pharmacist/'+path,{...options,token});
  if(!window.DeskNavigation){
    for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{
      for(const section of document.querySelectorAll('[data-screen]'))section.hidden=section.dataset.screen!==button.dataset.view;
      for(const other of document.querySelectorAll('[data-view]'))other.classList.toggle('is-current',other===button);
      $('view-title').textContent=button.textContent.trim();
    });
  }
  async function attempt(fn){try{$('desk-error').textContent='';await fn();}catch(e){$('desk-error').textContent=e.message||'Connexion interrompue.';}}
  function profile(data){
    $('pharmacy-name').textContent=data.pharmacy.name;$('demo').hidden=!data.pharmacy.is_demo;
    const labels={unknown:'Ouverture non confirmée',confirmed_open:'Ouverture confirmée',confirmed_closed:'Fermeture déclarée'};
    $('opening').textContent=labels[data.pharmacy.opening_status]+(data.pharmacy.opening_status!=='unknown'?' · jusqu’au '+date(data.pharmacy.opening_valid_until):'');
    const nodes=data.duties.map(d=>{const n=el('div',undefined,'desk-duty');n.append(el('strong',PH.dutyText(d)),el('p',d.cancelled?'ANNULÉ':d.source_label,'ph-muted'));
      if(!d.cancelled&&d.source_label==='Déclaration de la pharmacie partenaire')n.append(button('Annuler ce créneau',()=>attempt(async()=>{await call('duties/'+d.id,{method:'DELETE'});await refresh();})));return n;});
    $('duties').replaceChildren(...nodes);if(!nodes.length)$('duties').append(el('p','Aucun créneau enregistré.','ph-muted'));
    $('regular-hours').textContent=PH.scheduleText(data.pharmacy);
  }
  function dashboard(d){
    const delay=d.mean_first_reply_seconds===null?'—':d.mean_first_reply_seconds<60?d.mean_first_reply_seconds+' s':Math.round(d.mean_first_reply_seconds/60)+' min';
    const metrics=[['Relais en attente',d.waiting,'À prendre en charge'],['Échanges en cours',d.active,'Conversations acceptées'],['Demandes reçues',d.requests_7d,'7 derniers jours'],['Première réponse',delay,d.first_reply_sample_count+' échange(s) mesuré(s)']];
    $('kpis').replaceChildren(...metrics.map(([label,value,note])=>{const n=el('article',undefined,'desk-kpi');n.append(el('p',label),el('strong',String(value)),el('span',note));return n;}));
    $('waiting-badge').textContent=d.waiting;document.title=(d.waiting?'('+d.waiting+') ':'')+'Espace pharmacien';
    const max=Math.max(1,...d.daily_requests.map(x=>x.count));
    $('daily-chart').replaceChildren(...d.daily_requests.map(x=>{const n=el('div',undefined,'desk-bar-column');const rail=el('div',undefined,'desk-bar-rail'),bar=el('div',undefined,'desk-bar');bar.style.height=Math.max(x.count?4:0,x.count/max*100)+'%';rail.append(bar);const day=new Intl.DateTimeFormat('fr-MA',{weekday:'short',timeZone:'Africa/Casablanca'}).format(new Date(x.date+'T12:00:00Z'));n.append(el('strong',String(x.count)),rail,el('span',day));n.setAttribute('aria-label',x.date+' : '+x.count+' demande(s)');return n;}));
    const statusLabels={pending:'En attente',available:'Disponible',unavailable:'Indisponible',needs_details:'Précision demandée',expired:'Expirée',cancelled:'Annulée'};
    const rows=Object.entries(d.request_statuses).map(([status,count])=>{const n=el('div',undefined,'desk-status-row');n.append(el('span',statusLabels[status]||status),el('strong',String(count)));return n;});
    $('status-chart').replaceChildren(...rows);if(!rows.length)$('status-chart').append(el('p','Les résultats apparaîtront après vos premières demandes.','desk-empty-copy'));
    $('top-medications').replaceChildren(...d.top_medications.map((m,i)=>{const n=el('div',undefined,'desk-product');n.append(el('span',String(i+1).padStart(2,'0'),'desk-product-rank'),el('p',medication(m)),el('strong',m.count+' demande(s)'));return n;}));
    if(!d.top_medications.length)$('top-medications').append(el('p','Aucune demande confirmée sur cette période.','ph-muted'));
    $('dashboard-note').textContent=d.note;$('dashboard-updated').textContent='Données actualisées le '+date(d.checked_at)+' · heure du Maroc';
  }
  async function choose(r){
    selectedId=r.id;
    const h=threads.find(t=>t.request_id===r.id);
    $('stock-actions').hidden=r.status!=='pending';
    if(h){
      if(!conversation){$('thread-host').replaceChildren();conversation=HumanRelay.thread($('thread-host'),{role:'pharmacist',getToken:()=>token,onState:()=>{},onError:e=>{$('desk-error').textContent=e.message;}});}
      await conversation.open(h);
    }else{
      conversation=null;$('thread-host').replaceChildren(el('h2','Demande de disponibilité'),el('p',medication(r.medication)),el('p','Le patient n’a pas ouvert de relais. Vous pouvez renseigner la disponibilité ci-dessous.','ph-muted'));
    }
    if(selectedId!==r.id)return;
    for(const card of document.querySelectorAll('#inbox [data-availability-id]'))card.classList.toggle('is-selected',card.dataset.availabilityId===r.id);
  }
  async function inbox(){
    const f=$('inbox-filter').value;
    const visible=requests.filter(r=>f==='all'||threads.some(t=>t.request_id===r.id&&t.status===f));
    $('inbox-count').textContent=`${visible.length} demande(s) · suivi automatique toutes les 3 secondes`;
    $('inbox').replaceChildren(...visible.map(r=>{
      const n=requestCard(r),h=threads.find(t=>t.request_id===r.id);
      n.querySelector('h3').textContent=h?h.patient_label:'Demande '+r.id.slice(-6).toUpperCase();
      n.classList.toggle('is-selected',r.id===selectedId);
      if(h)n.append(el('p',HumanRelay.labels[h.status],'desk-thread-label'));
      n.append(button('Ouvrir la demande',()=>attempt(()=>choose(r))));return n;
    }));
    if(!visible.length)$('inbox').append(el('p','Aucune demande confirmée pour ce filtre. Les brouillons du patient restent privés.','desk-empty-copy'));
    const selected=visible.find(r=>r.id===selectedId)||visible[0];
    if(!selected){selectedId=null;conversation=null;$('thread-host').replaceChildren(el('p','Aucune conversation pour ce filtre.','ph-muted'));$('stock-actions').hidden=true;}
    if(selected){
      if(!selectedId||!conversation||conversation.current?.request_id!==selected.id)await choose(selected);
      else{$('stock-actions').hidden=selected.status!=='pending';await conversation.refresh();}
    }
  }
  function refresh(){
    if(inflight)return inflight;
    if(!token)return Promise.reject(new Error('Connectez-vous avec votre code pharmacien.'));
    if(!ready)return Promise.reject(new Error('L’espace pharmacien finit de se charger. Réessayez dans un instant.'));
    inflight=load().finally(()=>{inflight=null;});return inflight;
  }
  async function load(){
    const access=token;
      const results=await Promise.allSettled([call('profile'),call('requests'),call('handoffs'),call('dashboard')]);
      if(token!==access)return;
      if(results[0].status==='rejected')throw results[0].reason;
      profile(results[0].value);
      const warnings=[];
      for(const [index,label] of [[1,'Demandes'],[2,'Messagerie'],[3,'Statistiques']]){
        const r=results[index];if(r.status==='rejected')warnings.push(label+' : '+r.reason.message);
      }
      if(results[1].status==='fulfilled')requests=results[1].value.requests;
      if(results[2].status==='fulfilled')threads=results[2].value.handoffs;
      if(results[3].status==='fulfilled')dashboard(results[3].value);
      try{await inbox();}catch(e){warnings.push('Conversation : '+e.message);}
      if(token===access){$('login').hidden=true;$('workspace').hidden=false;}
      if(warnings.length)throw new Error(warnings.join(' · '));
  }
  async function recordStock(status){
    const r=requests.find(r=>r.id===selectedId);if(!r)throw new Error('Sélectionnez une demande.');
    const comment=$('stock-comment').value.trim();if(status==='needs_details'&&!comment)throw new Error('Indiquez la précision à demander.');
    await call('requests/'+r.id+'/response',{method:'POST',body:{status,comment}});
    if(conversation?.current?.request_id===r.id&&conversation.current.status==='active'){
      const labels={available:'Le médicament demandé est disponible au moment de ma vérification. Aucune réservation.',unavailable:'Le médicament demandé est indisponible au moment de ma vérification.',needs_details:'J’ai besoin d’une précision sur votre demande.'};
      await conversation.send(labels[status]+(comment?' '+comment:''));
    }
    $('stock-comment').value='';await refresh();
  }
  for(const [id,s] of [['stock-available','available'],['stock-unavailable','unavailable'],['stock-details','needs_details']])$(id).onclick=()=>attempt(()=>recordStock(s));
  $('inbox-filter').onchange=()=>attempt(inbox);
  $('login-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{if(!ready)throw new Error('L’espace pharmacien est indisponible.');token=$('access-code').value.trim();await refresh();PH.storage.set('pharmacy_partner_token',token);$('access-code').value='';});};
  $('logout').onclick=()=>{token='';PH.storage.remove('pharmacy_partner_token');$('workspace').hidden=true;$('login').hidden=false;$('inbox').replaceChildren();$('thread-host').replaceChildren();conversation=null;selectedId=null;document.title='Espace pharmacien';};
  $('refresh').onclick=async()=>{
    const b=$('refresh');b.disabled=true;b.textContent='Actualisation…';$('desk-sync-status').textContent='Mise à jour en cours…';
    try{$('desk-error').textContent='';await refresh();$('desk-sync-status').textContent='Actualisé à '+new Date().toLocaleTimeString('fr-MA',{timeZone:'Africa/Casablanca'});}
    catch(e){$('desk-error').textContent=e.message;$('desk-sync-status').textContent='Échec de l’actualisation. Vous pouvez réessayer.';}
    finally{b.disabled=false;b.textContent='Actualiser';}
  };
  for(const [id,is_open] of [['open-now',true],['closed-now',false]])$(id).onclick=()=>attempt(async()=>{profile(await call('opening',{method:'POST',body:{is_open}}));});
  $('duty-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{await call('duties',{method:'POST',body:{starts_at:$('starts-at').value,ends_at:$('ends-at').value,duty_type:$('duty-type').value}});$('duty-form').reset();await refresh();});};
  $('desk-today').textContent=new Intl.DateTimeFormat('fr-MA',{timeZone:'Africa/Casablanca',dateStyle:'full'}).format(new Date());
  api('/api/v1/pharmacies/config').then(async c=>{
    ready=!!c.enabled;if(!ready){$('desk-error').textContent=c.message||'L’espace pharmacien est désactivé.';for(const n of $('login-form').elements)n.disabled=true;return;}
    if(token)await attempt(refresh);
  }).catch(()=>{$('desk-error').textContent='Connexion au serveur impossible. Rechargez la page.';});
  setInterval(()=>{if(!document.hidden&&token&&ready)attempt(refresh);},3000);
})();
