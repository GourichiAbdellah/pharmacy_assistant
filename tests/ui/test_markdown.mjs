// Real Chromium checks, with a local static server and mocked chat responses.
import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {resolve, sep, extname} from 'node:path';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../../pharmacy_next/static/', import.meta.url));
let server, browser, origin;

before(async () => {
  server = createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
      const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/static\//, '');
      const path = resolve(root, relative);
      if (!path.startsWith(resolve(root) + sep)) throw new Error('outside static root');
      const body = await readFile(path);
      const type = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}[extname(path)];
      response.writeHead(200, {'Content-Type': (type || 'text/plain') + '; charset=utf-8'});
      response.end(body);
    } catch { response.writeHead(404); response.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  let launch = {headless: true};
  if (process.env.CHROMIUM_PACKAGE_PATH) {
    const loaded = require(process.env.CHROMIUM_PACKAGE_PATH);
    const packaged = loaded.default || loaded;
    launch = {...launch, executablePath: await packaged.executablePath(),
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader']};
  }
  browser = await chromium.launch(launch);
});

after(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

async function withPage(options, callback) {
  const page = await browser.newPage({viewport: options.mobile ? {width: 390, height: 844} : {width: 1100, height: 900}});
  const errors = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const url = route.request().url();
    if (!url.startsWith(origin + '/')) {external.push(url); return route.abort();}
    if (options.block && url.includes(options.block)) return route.abort();
    return route.continue();
  });
  try {
    await page.goto(origin);
    await callback(page);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, [], 'No model-supplied remote asset may be loaded');
  } finally {await page.close();}
}

const comparison = `Voici les **deux présentations** du médicament fictif.

| Dosage | Boîte | Prix public | Base | Estimation |
| --- | --- | --- | --- | --- |
| 20 mg | 20 comprimés | 37,40 DH | 37,40 DH | **26,18 DH** |
| 5 mg | 30 comprimés | 20,50 DH | 20,50 DH | **14,35 DH** |

- Montants fictifs pour tester l'affichage.
- Chiffres conservés tels quels.`;

test('chat renders a table, bold and lists without changing values', async () => {
  await withPage({}, async page => {
    await page.route('**/api/v1/chat', route => route.fulfill({json: {
      session_id: '00000000-0000-0000-0000-000000000001', answer: comparison, sources: []
    }}));
    await page.locator('#message').fill('Compare les deux boîtes');
    await page.locator('#send').click();
    await page.locator('.assistant table').waitFor();
    assert.equal(await page.locator('.assistant tbody tr').count(), 2);
    assert.equal(await page.locator('.assistant li').count(), 2);
    assert.deepEqual(await page.locator('.assistant td strong').allTextContents(), ['26,18 DH', '14,35 DH']);
    assert.equal(await page.locator('.assistant .table-wrap').getAttribute('tabindex'), '0');
    if (process.env.UI_SCREENSHOT_DIR) {
      await mkdir(process.env.UI_SCREENSHOT_DIR, {recursive: true});
      await page.screenshot({path: resolve(process.env.UI_SCREENSHOT_DIR, 'markdown-desktop.png'), fullPage: true});
    }
  });
});

test('mobile scroll stays inside the table and the page fits the screen', async () => {
  await withPage({mobile: true}, async page => {
    await page.evaluate(text => bubble('assistant', text), comparison);
    const dimensions = await page.locator('.table-wrap').evaluate(el => ({
      table: el.scrollWidth, box: el.clientWidth,
      page: document.documentElement.scrollWidth, screen: innerWidth
    }));
    assert.ok(dimensions.table > dimensions.box);
    assert.ok(dimensions.page <= dimensions.screen, JSON.stringify(dimensions));
    if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({path: resolve(process.env.UI_SCREENSHOT_DIR, 'markdown-mobile.png'), fullPage: true});
  });
});

test('Arabic direction and Latin dosage are preserved', async () => {
  await withPage({}, async page => {
    await page.evaluate(() => bubble('assistant', 'هاد **الدوا** فيه 500 mg.\n\n- معلومات من المصدر'));
    assert.equal(await page.locator('.assistant').getAttribute('dir'), 'auto');
    assert.equal(await page.locator('.assistant').evaluate(el => getComputedStyle(el).direction), 'rtl');
    assert.equal(await page.locator('.assistant strong').textContent(), 'الدوا');
    assert.match(await page.locator('.assistant').textContent(), /500 mg/);
  });
});

test('user, OCR and source text are never interpreted as Markdown or HTML', async () => {
  await withPage({}, async page => {
    const text = '**test** <img src=x onerror=alert(1)>';
    await page.evaluate(text => {
      bubble('user', text);
      imageReading({text});
      sources([{source: text, label: text, id: 1}]);
    }, text);
    assert.equal(await page.locator('.user').textContent(), text);
    assert.equal(await page.locator('.ocr-text').textContent(), text);
    assert.equal(await page.locator('.user img,.user strong,.sources img,.sources strong').count(), 0);
  });
});

test('HTML attacks, remote images and DOM clobbering are removed', async () => {
  await withPage({}, async page => {
    await page.evaluate(() => {
      window.attackExecuted = false;
      bubble('assistant', `Texte **lisible**\n\n<script>window.attackExecuted=true</script>
<img src="https://tracking.invalid/pixel" onerror="window.attackExecuted=true">
<svg onload="window.attackExecuted=true"></svg>
<iframe src="https://tracking.invalid/frame"></iframe>
<style>body{display:none}</style>
<form id="message"><input name="cookie"></form>
<p id="messages" onclick="window.attackExecuted=true" style="color:red">suite</p>
[image distante](javascript:alert%281%29)
![image](https://tracking.invalid/markdown.png)`);
    });
    assert.equal(await page.locator('.assistant script,.assistant img,.assistant iframe,.assistant svg,.assistant style,.assistant form,.assistant input').count(), 0);
    assert.equal(await page.locator('.assistant [onclick],.assistant [onerror],.assistant [style],.assistant [id]').count(), 0);
    assert.equal(await page.evaluate(() => window.attackExecuted), false);
    assert.equal(await page.locator('#message').count(), 1);
    assert.equal(await page.locator('.assistant strong').textContent(), 'lisible');
  });
});

test('only absolute HTTP(S) links remain clickable', async () => {
  await withPage({}, async page => {
    await page.evaluate(() => bubble('assistant', '[Source](https://asafar.net/)\n\n[local](/docs) [script](javascript:alert%281%29) [fichier](file:///etc/passwd) [relatif](//example.org)'));
    assert.equal(await page.locator('.assistant a').count(), 1);
    assert.equal(await page.locator('.assistant a').getAttribute('href'), 'https://asafar.net/');
    assert.equal(await page.locator('.assistant a').getAttribute('target'), '_blank');
    assert.equal(await page.locator('.assistant a').getAttribute('rel'), 'noopener noreferrer');
  });
});

for (const asset of ['purify.min.js', 'marked.umd.js', 'markdown.js']) {
  test(`safe text fallback when ${asset} cannot load`, async () => {
    await withPage({block: asset}, async page => {
      const text = '**Texte brut** <img src=x onerror=alert(1)>';
      await page.evaluate(text => bubble('assistant', text), text);
      assert.equal(await page.locator('.assistant').textContent(), text);
      assert.equal(await page.locator('.assistant img,.assistant strong').count(), 0);
    });
  });
}
