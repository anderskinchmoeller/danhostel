import { chromium } from '/Users/anderskinch/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
const out = new URL('./', import.meta.url);
const browser = await chromium.launch({headless: true});
const errors = [];
const results = [];
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({viewport:{width,height:900},
      httpCredentials:{username:process.env.RMS_USER,password:process.env.RMS_PASSWORD}});
    const page = await context.newPage();
    await page.emulateMedia({reducedMotion:'reduce'});
    page.on('pageerror', e => errors.push(e.message));
    const response = await page.goto('http://127.0.0.1:8000/resultater/oekonomi');
    if (response.status() !== 200) throw Error('Economics page failed');
    const details = page.locator('details').filter({hasText:'Se stresstest for'});
    await details.locator('summary').click();
    if (!(await details.getAttribute('open') !== null)) throw Error('Stress table did not open');
    const rows = await details.locator('tbody tr').count();
    if (rows < 2) throw Error('Missing stress results');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    if (overflow) {
      await page.screenshot({path:new URL(`overflow-${width}.png`,out).pathname});
      console.log(JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('body *')]
        .filter(e=>e.getBoundingClientRect().right>innerWidth+1)
        .filter(e=>{let p=e.parentElement;while(p){if(['auto','scroll','hidden','clip'].includes(getComputedStyle(p).overflowX))return false;p=p.parentElement;}return true;})
        .slice(0,15).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,
          text:e.textContent.slice(0,65)})))));
      throw Error('Page overflow at '+width);
    }
    await page.getByRole('heading',{name:'Hvor meget afhænger priserne af antagelserne?'}).evaluate(el=>el.scrollIntoView({block:'start',behavior:'instant'}));
    await page.screenshot({path:new URL(`economics-stress-${width}.png`,out).pathname});
    await details.locator('summary').click();
    await page.getByRole('heading',{name:/Seneste prognosekontrol/}).evaluate(el=>el.scrollIntoView({block:'start',behavior:'instant'}));
    await page.screenshot({path:new URL(`economics-monitor-${width}.png`,out).pathname});
    results.push({width,stress_rows:rows,no_horizontal_page_overflow:!overflow});
    await context.close();
  }
} finally { await browser.close(); }
if (errors.length) throw Error(errors.join('\n'));
await fs.writeFile(new URL('ui-verification.json',out), JSON.stringify({results,page_errors:errors},null,2));
console.log(JSON.stringify({results,page_errors:errors}));
