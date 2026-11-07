'use strict';
window.HumanRelay=(()=>{
  const {el,button,api,date,medication}=PH;
  const labels={waiting:'En attente du pharmacien',active:'Le pharmacien a pris le relais',closed:'Échange terminé',expired:'Échange expiré'};
  function thread(root,{role,getToken,onState=()=>{},onError=()=>{}}){
    let current=null,after=0,loading=false,seen=new Set(),pending=null,generation=0;
    const head=el('div',undefined,'relay-heading'),title=el('h3','Discussion avec le pharmacien'),state=el('span','','relay-status');
    const context=el('p','','ph-muted'),expiry=el('p','','ph-muted'),feed=el('div',undefined,'relay-feed');
    feed.setAttribute('role','log');feed.setAttribute('aria-label','Messages avec le pharmacien');feed.setAttribute('aria-live','polite');
    const actions=el('div',undefined,'ph-row'),error=el('p','','ph-error');error.setAttribute('role','status');
    const prefix=role==='patient'?'/api/v1/availability/':'/api/v1/pharmacist/';
    const call=(tail,options={})=>api(prefix+'handoffs/'+current.id+tail,{...options,token:getToken()});
    async function attempt(fn){try{error.textContent='';await fn();}catch(e){error.textContent=e.message;onError(e);}}
    function update(h){
      current=h;title.textContent=role==='patient'?'Discussion · '+h.pharmacy_name:h.patient_label;
      state.textContent=labels[h.status];state.dataset.state=h.status;
      context.textContent=medication(h.medication)+` · ${h.boxes} boîte(s)`;
      expiry.textContent=`${h.is_demo?'DÉMONSTRATION · ':''}Échange textuel · heure du Maroc · valable jusqu’au ${date(h.expires_at)}`;
      actions.replaceChildren();
      if(role==='pharmacist'&&h.status==='waiting')actions.append(button('Prendre le relais',()=>attempt(async()=>{await call('/accept',{method:'POST'});await refresh();}),'ph-primary'));
      if(['waiting','active'].includes(h.status))actions.append(button(role==='patient'?'Revenir à l’assistant':'Terminer l’échange',()=>attempt(async()=>{await call('/close',{method:'POST'});await refresh();})));
      if(input){input.disabled=h.status!=='active';sendButton.disabled=h.status!=='active';}
      onState(h);
    }
    head.append(title,state);root.classList.add('relay-thread');root.append(head,context,expiry,feed,actions,error);
    let input=null,sendButton=null;
    if(role==='pharmacist'){
      const form=el('form',undefined,'relay-composer');input=el('textarea');input.maxLength=2000;input.placeholder='Répondre au patient…';input.setAttribute('aria-label','Message au patient');
      sendButton=el('button','Envoyer au patient','ph-primary');sendButton.type='submit';form.append(input,sendButton);root.append(form);
      form.onsubmit=e=>{e.preventDefault();attempt(async()=>{await send(input.value);input.value='';});};
    }
    async function refresh(){
      if(!current||loading)return;loading=true;
      const version=generation,identifier=current.id;
      try{
        let data;
        do{
          data=await api(prefix+'handoffs/'+identifier+'?after='+after,{token:getToken()});
          if(version!==generation)return;
          for(const m of data.messages){
            if(seen.has(m.id))continue;seen.add(m.id);
            const row=el('article',undefined,'relay-message relay-'+m.sender_role);row.dataset.messageId=m.id;
            const names={patient:role==='patient'?'Vous':'Patient',pharmacist:'Pharmacien',system:'Information de suivi'};
            row.append(el('div',names[m.sender_role]+' · '+date(m.sent_at),'relay-author'));
            const body=el('p',m.body);body.dir='auto';row.append(body);feed.append(row);
          }
          after=data.next_after;
        }while(data.has_more);
        update(data.handoff);
        // Only scroll if new messages arrived, so reading older messages remains possible.
        if(feed.dataset.lastSequence!==String(after)){feed.dataset.lastSequence=String(after);feed.scrollTop=feed.scrollHeight;}
      }finally{loading=false;}
    }
    async function send(text){
      text=text.trim();if(!text)return;
      if(!current||!['waiting','active'].includes(current.status))throw new Error('Cet échange est terminé.');
      if(text.length>2000)throw new Error('Le message doit contenir au maximum 2 000 caractères.');
      if(!pending||pending.text!==text)pending={text,client_message_id:crypto.randomUUID()};
      const saved=await call('/messages',{method:'POST',body:pending});pending=null;if(saved.id===current.id)update(saved);await refresh();
    }
    return {async open(h){if(current?.id!==h.id){generation++;feed.replaceChildren();after=0;seen=new Set();pending=null;}update(h);await refresh();},
      refresh,send,get current(){return current;},get isLive(){return !!current&&['waiting','active'].includes(current.status);}};
  }
  return {thread,labels};
})();
