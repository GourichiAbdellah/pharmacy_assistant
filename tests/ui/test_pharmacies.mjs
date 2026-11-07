import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
let browser,server,origin,token,tokenAgdal;
before(async()=>{
  server=spawn(process.env.TEST_PYTHON||'python',[fileURLToPath(new URL('pharmacies_server.py',import.meta.url))],{stdio:['ignore','pipe','pipe']});
  server.stderr.on('data',d=>process.stderr.write(d));
  const line=await new Promise((resolve,reject)=>{const rl=createInterface({input:server.stdout});rl.once('line',line=>{rl.close();resolve(line);});server.once('error',reject);server.once('exit',code=>reject(new Error('Fixture exited '+code)));});
  ({origin,token,token_agdal:tokenAgdal}=JSON.parse(line));
  let ready=false;for(let i=0;i<100;i++){try{ready=(await fetch(origin+'/health')).ok;}catch{}if(ready)break;await new Promise(r=>setTimeout(r,100));}assert.ok(ready);
  const options={headless:true};if(process.env.CHROMIUM_PACKAGE_PATH){const loaded=require(process.env.CHROMIUM_PACKAGE_PATH),pkg=loaded.default||loaded;options.executablePath=await pkg.executablePath();options.args=['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader'];}
  browser=await chromium.launch(options);
});
after(async()=>{await browser?.close();if(server&&!server.killed)server.kill('SIGTERM');});
async function screenshot(page,name){if(process.env.UI_SCREENSHOT_DIR){await mkdir(process.env.UI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:resolve(process.env.UI_SCREENSHOT_DIR,name+'.png'),fullPage:true});}}

test('patient confirms draft, pharmacist responds, patient receives dated result',async()=>{
  const patient=await browser.newContext({viewport:{width:1100,height:900}}),pharmacist=await browser.newContext({viewport:{width:1200,height:950}});
  const page=await patient.newPage(),desk=await pharmacist.newPage(),errors=[];
  for(const p of [page,desk])p.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.locator('#message').fill('Je veux le médicament de démonstration boîte de 12.');await page.locator('#send').click();await page.locator('.assistant').waitFor();
    await page.locator('#pharmacies-open').click();await page.locator('#ph-city').fill('Rabat');await page.getByLabel('De garde maintenant').check();await page.getByRole('button',{name:'Rechercher',exact:true}).click();
    await page.locator('#ph-results .ph-card').waitFor();assert.equal(await page.locator('#ph-results .ph-card').count(),1);
    await page.getByRole('button',{name:'Vérifier avec le pharmacien',exact:true}).click();await page.getByRole('button',{name:'Confirmer et envoyer'}).waitFor();
    await page.getByLabel(/Continuer avec le pharmacien/).uncheck();
    await page.locator('#pharmacies-open').click();
    await page.getByRole('button',{name:'Actualiser les demandes'}).click();
    assert.equal(await page.locator('#pharmacies-panel').getByLabel(/Continuer avec le pharmacien/).isChecked(),false);
    await desk.goto(origin+'/pharmacien');await desk.locator('#access-code').fill(token);await desk.getByRole('button',{name:'Se connecter'}).click();await desk.locator('#workspace').waitFor();
    await desk.getByRole('button',{name:'Conversations',exact:true}).click();
    assert.match(await desk.locator('#inbox').textContent(),/Aucune demande confirmée/);
    await screenshot(page,'patient-confirmation');
    await page.locator('#pharmacies-panel').getByRole('button',{name:'Confirmer et envoyer'}).click();await page.locator('#ph-requests .ph-card').filter({hasText:'En attente de réponse sur la disponibilité'}).waitFor();
    await desk.locator('#refresh').click();await desk.getByRole('button',{name:'Disponible',exact:true}).waitFor();
    await desk.getByLabel('Réponse du pharmacien').fill('Vérifié au comptoir. <img src=x onerror=alert(1)>');await desk.getByRole('button',{name:'Disponible',exact:true}).click();
    await desk.locator('#inbox .ph-card').filter({hasText:'Disponible selon le pharmacien'}).waitFor();
    await page.getByRole('button',{name:'Actualiser les demandes'}).click();await page.locator('#ph-requests .ph-card').filter({hasText:'Disponible selon le pharmacien'}).waitFor();
    assert.equal(await page.locator('#ph-requests img').count(),0);assert.equal(await desk.locator('#inbox img').count(),0);
    await screenshot(page,'patient-response');await screenshot(desk,'pharmacist-inbox');
    await page.reload();await page.locator('#pharmacies-open').click();await page.locator('#ph-requests .ph-card').filter({hasText:'Disponible selon le pharmacien'}).waitFor();
    assert.deepEqual(errors,[]);
  }finally{await patient.close();await pharmacist.close();}
});

test('chat can prepare a draft on mobile and still requires explicit sending',async()=>{
  const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.locator('#message').fill('Prépare une demande pour la boîte de 12.');await page.locator('#send').click();await page.getByRole('button',{name:'Confirmer et envoyer'}).waitFor();
    assert.match(await page.locator('.ph-inline').textContent(),/Brouillon — non envoyé/);
    await page.getByRole('button',{name:'Confirmer et envoyer'}).click();await page.locator('.ph-inline').filter({hasText:'En attente de réponse sur la disponibilité'}).waitFor();
    const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert.ok(size.scroll<=size.width,JSON.stringify(size));
    await screenshot(page,'mobile-request');assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('human takes over the same patient chat, dashboard is scoped, then assistant resumes',async()=>{
  const patient=await browser.newContext({viewport:{width:1100,height:940}}),pharmacist=await browser.newContext({viewport:{width:1440,height:1100}});
  const page=await patient.newPage(),desk=await pharmacist.newPage(),errors=[];
  for(const p of [page,desk])p.on('pageerror',e=>errors.push(e.message));
  try{
    await desk.goto(origin+'/pharmacien');await desk.locator('#access-code').fill(token);await desk.getByRole('button',{name:'Se connecter'}).click();await desk.locator('#workspace').waitFor();
    assert.equal(await desk.locator('#kpis .desk-kpi').count(),4);await screenshot(desk,'pharmacist-dashboard');
    await page.goto(origin);await page.locator('#message').fill('Prépare une demande pour la boîte de 12.');await page.locator('#send').click();await page.getByRole('button',{name:'Confirmer et envoyer'}).waitFor();
    assert.equal(await page.getByLabel(/Continuer avec le pharmacien/).isChecked(),true);
    await page.getByRole('button',{name:'Confirmer et envoyer'}).click();await page.locator('#patient-relay .relay-status').filter({hasText:'En attente du pharmacien'}).waitFor();
    assert.equal(await page.locator('#upload-photo').isDisabled(),true);
    const assistantCount=await page.locator('.assistant').count();
    await page.locator('#message').fill('واش كاينة العلبة ديال 12؟');await page.locator('#send').click();await page.locator('#patient-relay .relay-patient').filter({hasText:'واش كاينة'}).waitFor();
    assert.equal(await page.locator('.assistant').count(),assistantCount);
    await desk.getByRole('button',{name:'Conversations',exact:true}).click();await desk.locator('#refresh').click();
    const relayRequest=await page.evaluate(async()=>{const r=await fetch('/api/v1/availability/handoffs',{headers:{Authorization:'Bearer '+sessionStorage.getItem('pharmacy_patient_token')}});return (await r.json()).handoffs[0].request_id;});
    await desk.locator(`[data-availability-id="${relayRequest}"]`).getByRole('button',{name:'Ouvrir la demande'}).click();
    await desk.getByRole('button',{name:'Prendre le relais'}).click();await desk.locator('#thread-host .relay-status').filter({hasText:'a pris le relais'}).waitFor();
    await desk.getByLabel('Message au patient').fill('Bonjour. Oui la boîte de 12 est disponible. <img src=x onerror=alert(1)>');await desk.getByRole('button',{name:'Envoyer au patient'}).click();
    await page.locator('#patient-relay .relay-pharmacist').filter({hasText:'Oui la boîte de 12'}).waitFor();
    assert.equal(await page.locator('#patient-relay img').count(),0);
    await screenshot(page,'patient-human-relay');await screenshot(desk,'pharmacist-live-conversation');
    await page.reload();await page.locator('#patient-relay .relay-pharmacist').filter({hasText:'Oui la boîte de 12'}).waitFor();
    await page.getByRole('button',{name:'Revenir à l’assistant'}).click();await page.locator('#patient-relay .relay-status').filter({hasText:'Échange terminé'}).waitFor();
    assert.equal(await page.locator('#upload-photo').isDisabled(),false);
    await page.locator('#message').fill('Merci');await page.locator('#send').click();await page.locator('.assistant').waitFor();
    await desk.getByRole('button',{name:'Vue d’ensemble',exact:true}).click();await desk.locator('#refresh').click();
    assert.match(await desk.locator('#dashboard-note').textContent(),/7 derniers jours/);
    await screenshot(desk,'pharmacist-dashboard-after-relay');
    assert.deepEqual(errors,[]);
  }finally{await patient.close();await pharmacist.close();}
});

test('pharmacist dashboard fits a mobile viewport and navigation works',async()=>{
  const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin+'/pharmacien');await page.locator('#access-code').fill(token);await page.getByRole('button',{name:'Se connecter'}).click();await page.locator('#workspace').waitFor();
    const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert.ok(size.scroll<=size.width,JSON.stringify(size));
    await screenshot(page,'pharmacist-dashboard-mobile');
    await page.getByRole('button',{name:'Ouverture et gardes',exact:true}).click();await page.locator('#duty-form').waitFor();
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('navigation and manual refresh remain usable after a dashboard API failure and blocked storage',async()=>{
  const context=await browser.newContext({viewport:{width:1200,height:940}}),page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{Storage.prototype.getItem=function(){throw new DOMException('Blocked','SecurityError');};Storage.prototype.setItem=function(){throw new DOMException('Blocked','SecurityError');};});
  try{
    await page.goto(origin+'/pharmacien');await page.locator('#access-code').fill(token);await page.getByRole('button',{name:'Se connecter'}).click();await page.locator('#workspace').waitFor();
    await page.route('**/api/v1/pharmacist/dashboard',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Panne simulée des statistiques'})}));
    await page.locator('#refresh').click();await page.locator('#desk-sync-status').filter({hasText:'Échec'}).waitFor();
    assert.equal(await page.locator('#refresh').isDisabled(),false);
    await page.getByRole('button',{name:'Conversations',exact:true}).click();await page.locator('[data-screen=conversations]').waitFor();
    await page.getByRole('button',{name:'Ouverture et gardes',exact:true}).click();await page.locator('#duty-form').waitFor();
    await page.unroute('**/api/v1/pharmacist/dashboard');await page.locator('#refresh').click();await page.locator('#desk-sync-status').filter({hasText:'Actualisé à'}).waitFor();
    await page.getByRole('button',{name:'Vue d’ensemble',exact:true}).click();await page.locator('#kpis .desk-kpi').first().waitFor();
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('assistant prepares availability for nearest partner with one confirmation and no product form',async()=>{
  const context=await browser.newContext({viewport:{width:1100,height:950},permissions:['geolocation'],geolocation:{latitude:34.0015,longitude:-6.8537}}),page=await context.newPage(),errors=[];
  const deskContext=await browser.newContext(),desk=await deskContext.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.locator('#message').fill('Vérifie la disponibilité du médicament de test boîte de 12.');await page.locator('#send').click();await page.getByRole('button',{name:'Partager ma position',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Confirmer et envoyer'}).count(),0);
    await page.getByRole('button',{name:'Partager ma position',exact:true}).click();await page.getByRole('button',{name:'Confirmer et envoyer'}).waitFor();
    const draft=page.locator('.ph-inline .ph-card').filter({hasText:'Brouillon — non envoyé'});
    assert.match(await draft.textContent(),/Jardin/);assert.match(await draft.textContent(),/BOITE DE 12/);
    assert.equal(await draft.locator('select,input[type=number],input[type=text]').count(),0);
    await page.getByRole('button',{name:'Confirmer et envoyer'}).click();await page.locator('#patient-relay .relay-status').filter({hasText:'En attente du pharmacien'}).waitFor();
    await desk.goto(origin+'/pharmacien');await desk.locator('#access-code').fill(tokenAgdal);await desk.getByRole('button',{name:'Se connecter'}).click();await desk.locator('#workspace').waitFor();
    assert.match(await desk.locator('#pharmacy-name').textContent(),/Jardin/);
    await desk.getByRole('button',{name:'Conversations',exact:true}).click();await desk.getByRole('button',{name:'Prendre le relais'}).click();
    await desk.getByLabel('Message au patient').fill('Bonjour, disponibilité vérifiée par la pharmacie Jardin.');await desk.getByRole('button',{name:'Envoyer au patient'}).click();
    await page.locator('#patient-relay .relay-pharmacist').filter({hasText:'pharmacie Jardin'}).waitFor();
    await screenshot(page,'automatic-nearest-request');assert.deepEqual(errors,[]);
  }finally{await context.close();await deskContext.close();}
});

test('availability click without a selected presentation asks the assistant and does not silently fail',async()=>{
  const context=await browser.newContext(),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.locator('#pharmacies-open').click();await page.locator('#ph-city').fill('Rabat');await page.getByRole('button',{name:'Rechercher',exact:true}).click();
    await page.getByRole('button',{name:'Vérifier avec le pharmacien',exact:true}).first().click();
    await page.getByRole('button',{name:'Partager ma position',exact:true}).waitFor();
    assert.match(await page.locator('.user').textContent(),/vérifier la disponibilité/);
    assert.equal(await page.locator('#pharmacies-panel').evaluate(n=>n.open),false);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});
