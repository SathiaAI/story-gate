// node render.js <src.html> <out.png> [scale]
const { chromium } = require('playwright');
(async () => {
  const [src, out, sc] = process.argv.slice(2);
  const b = await chromium.launch({executablePath: process.env.CHROMIUM || undefined});
  const p = await b.newPage({ viewport: { width: parseInt(process.argv[5]||'2000'), height: 800 }, deviceScaleFactor: parseFloat(sc||'1.2') });
  await p.goto('file://' + require('path').resolve(src));
  await p.evaluate(() => document.fonts.ready);
  await p.waitForTimeout(300);
  await p.screenshot({ path: out, fullPage: true });
  await b.close();
})();
