#!/usr/bin/env node
// Renders fika_reel.html frame by frame (exact timing) and encodes a 1080x1920 reel.
// Usage: node render_fika_reel.js MUSIC.wav output/fika_reel.mp4 [--preview t1,t2,...]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const FPS = 24, DUR = 25;
const [music, out] = process.argv.slice(2);
const previewArg = process.argv.indexOf('--preview');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve(__dirname, 'fika_reel.html'));
  await page.evaluate(() => window.ready);

  if (previewArg > 0) {
    for (const t of process.argv[previewArg + 1].split(',').map(Number)) {
      await page.evaluate(t => window.render(t), t);
      await page.screenshot({ path: `${out}_${t}.png` });
    }
    await browser.close();
    return;
  }

  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-i', music,
    '-filter:a', `afade=t=out:st=${DUR - 1.5}:d=1.5`,
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p',
    '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
    '-c:a', 'aac', '-b:a', '256k', '-t', String(DUR), '-movflags', '+faststart', out],
    { stdio: ['pipe', 'inherit', 'inherit'] });

  const N = FPS * DUR;
  for (let i = 0; i < N; i++) {
    await page.evaluate(t => window.render(t), i / FPS);
    const buf = await page.screenshot({ type: 'png' });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 48 === 0) process.stdout.write(`frame ${i}/${N}\n`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  await browser.close();
  console.log('wrote', out);
})();
