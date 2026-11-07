// Real Chromium + FastAPI + mocked upstream HTTP. No real API/account needed.
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
let browser,server,origin;
before(async()=>{
  server=spawn(process.env.TEST_PYTHON||'python',[fileURLToPath(new URL('pharmacies_server.py',import.meta.url)),'--google'],{stdio:['ignore','pipe','pipe']});
  server.stderr.on('data',d=>process.stderr.write(d));
  const line=await new Promise((resolve,reject)=>{const rl=createInterface({input:server.stdout});rl.once('line',line=>{rl.close();resolve(line);});server.once('error',reject);server.once('exit',code=>reject(new Error('Fixture exited '+code)));});
  ({origin}=JSON.parse(line));
  let ready=false;for(let i=0;i<100;i++){try{ready=(await fetch(origin+'/health')).ok;}catch{}if(ready)break;await new Promise(r=>setTimeout(r,100));}assert.ok(ready);
  const options={headless:true};if(process.env.CHROMIUM_PACKAGE_PATH){const loaded=require(process.env.CHROMIUM_PACKAGE_PATH),pkg=loaded.default||loaded;options.executablePath=await pkg.executablePath();options.args=['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader'];}
  browser=await chromium.launch(options);
});
after(async()=>{await browser?.close();if(server&&!server.killed)server.kill('SIGTERM');});
async function screenshot(page,name){if(process.env.UI_SCREENSHOT_DIR){await mkdir(process.env.UI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:resolve(process.env.UI_SCREENSHOT_DIR,name+'.png'),fullPage:true});}}

test('external cards: attribution, partner only, confirmation, no XSS or client API key',async()=>{
  const context=await browser.newContext({viewport:{width:1100,height:950}}),page=await context.newPage(),errors=[],traffic=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>traffic.push(r.url()));
  try{
    await page.goto(origin);await page.locator('#message').fill('Le médicament de test boite de 12');await page.locator('#send').click();await page.locator('.assistant').waitFor();
    await page.locator('#pharmacies-open').click();await page.locator('#ph-city').fill('Rabat');await page.getByRole('button',{name:'Rechercher',exact:true}).click();
    const partner=page.locator('#ph-results .ph-card').filter({hasText:'API SYNTHETIQUE PARTENAIRE'});
    const external=page.locator('#ph-results .ph-card').filter({hasText:'API SYNTHETIQUE EXTERNE'});
    await external.waitFor();assert.equal(await page.locator('.ph-google-content').count(),2);
    assert.equal(await external.getByRole('button',{name:'Vérifier avec le pharmacien'}).count(),0);
    assert.equal(await external.locator('img').count(),0);assert.equal(await external.locator('[href^="javascript:"]').count(),0);
    assert.equal(await external.locator('.ph-google-attribution').getAttribute('translate'),'no');
    assert.match(await external.textContent(),/Garde non confirmée/);
    await partner.scrollIntoViewIfNeeded();await screenshot(page,'api-pharmacies-cards');
    await partner.getByRole('button',{name:'Vérifier avec le pharmacien'}).click();
    await page.waitForFunction(()=>document.querySelector('.ph-inline [data-availability-id]')||document.querySelector('#pharmacies-panel .ph-error').textContent);
    assert.equal(await page.locator('#pharmacies-panel .ph-error').textContent(),'');
    await page.getByRole('button',{name:'Confirmer et envoyer'}).waitFor();
    await page.getByLabel(/Continuer avec le pharmacien/).uncheck();
    assert.match(await page.locator('.ph-inline').textContent(),/Brouillon — non envoyé/);
    assert.ok(!(await page.locator('.ph-inline').textContent()).includes('API SYNTHETIQUE'));
    await page.getByRole('button',{name:'Confirmer et envoyer'}).click();await page.locator('.ph-inline').filter({hasText:'En attente de réponse sur la disponibilité'}).waitFor();
    assert.ok(!traffic.some(url=>url.includes('places.googleapis.com')||url.includes('UI-FAKE-KEY')));
    assert.ok(!(await page.content()).includes('UI-FAKE-KEY'));
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});

test('chat cards on mobile and explicit partial results on API quota',async()=>{
  const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(origin);await page.locator('#message').fill('Une pharmacie proche de Rabat ?');await page.locator('#send').click();await page.locator('.ph-inline .ph-google-content').first().waitFor();
    const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert.ok(size.scroll<=size.width,JSON.stringify(size));
    await page.locator('.ph-inline .ph-google-content').first().scrollIntoViewIfNeeded();await screenshot(page,'api-pharmacies-mobile');
    await page.locator('#pharmacies-open').click();await page.locator('#ph-city').fill('Quota');await page.getByRole('button',{name:'Rechercher',exact:true}).click();
    await page.locator('#ph-results').filter({hasText:'Le quota Google Maps est atteint'}).waitFor();
    assert.equal(await page.locator('#ph-results .ph-google-content').count(),0);
    assert.match(await page.locator('#ph-results').textContent(),/Cela ne signifie pas/);
    await page.locator('#ph-city').fill('Rabat');await page.getByLabel('De garde maintenant').check();await page.getByRole('button',{name:'Rechercher',exact:true}).click();
    await page.locator('#ph-results .ph-card').waitFor();assert.equal(await page.locator('#ph-results .ph-card').count(),1);
    assert.equal(await page.locator('#ph-results .ph-google-content').count(),0);
    assert.match(await page.locator('#ph-results').textContent(),/Aucun flux externe de garde validé/);
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});
