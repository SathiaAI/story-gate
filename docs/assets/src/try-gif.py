"""Builds docs/assets/story-gate-try.gif: the `story-gate try` run, then an end card. Run from the repo root:
    python3 docs/assets/src/try-gif.py   (needs: pip install playwright pillow; a Chromium; set CHROMIUM to its path)
"""
import asyncio, html
from pathlib import Path
from playwright.async_api import async_playwright
from PIL import Image
import os
V=".story-gate/vendor/"
import base64
F1=base64.b64encode(open(V+"ClashDisplay-600.woff2","rb").read()).decode()
F2=base64.b64encode(open(V+"Switzer-400.woff2","rb").read()).decode()
CSS=f"""
@font-face{{font-family:Clash;src:url(data:font/woff2;base64,{F1})}}
@font-face{{font-family:Switzer;src:url(data:font/woff2;base64,{F2})}}
*{{margin:0;box-sizing:border-box}}
body{{width:960px;height:540px;background:#1F2327;color:#F1F5F2;font-family:'DejaVu Sans Mono',monospace;padding:34px 40px}}
.bar{{display:flex;gap:8px;margin-bottom:26px;align-items:center}}
.dot{{width:12px;height:12px;border-radius:50%;background:#3A4046}}
.title{{margin-left:14px;font-family:Switzer;color:#B9C0C6;font-size:15px}}
pre{{font-family:inherit;font-size:19px;line-height:1.55;white-space:pre-wrap}}
.p{{color:#FFD84D}} .m{{color:#B9C0C6}} .ok{{color:#F1F5F2}}
.fail{{background:#FFD84D;color:#1F2327;font-weight:bold;padding:0 6px;border-radius:4px}}
.cur{{display:inline-block;width:11px;height:22px;background:#FFD84D;vertical-align:-4px}}
.card{{position:absolute;inset:0;background:#FFF8F2;color:#1F2327;display:flex;flex-direction:column;justify-content:center;padding:70px}}
.card h1{{font-family:Clash;font-size:54px;line-height:1.08;letter-spacing:-.01em}}
.card p{{font-family:Switzer;font-size:24px;color:#5B6168;margin-top:22px;line-height:1.45}}
.card .y{{background:#FFD84D;padding:0 10px;border-radius:6px}}
"""
CMD="story-gate try"
L1='story-gate try: a throwaway project with one story (TRY-1: bulk discount) and a real bug'
A1='  AC-1 The total is quantity times price ........... <span class="ok">PASSED</span>'
A2='  AC-2 10 or more items get 10% off ................ <span class="fail">FAILED</span>\n       <span class="m">10 x 2.00 printed 20.00, expected 18.00</span>'
END="\nstory-gate ran each goal for real and <span class=\"p\">caught the bug before anyone said 'done'.</span>"
def term(body, cursor=True):
    return f'<div class="bar"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="title">Terminal</div></div><pre>{body}{"<span class=cur></span>" if cursor else ""}</pre>'
frames=[]  # (html, ms)
frames.append((term('<span class="p">$</span> '), 600))
for i in range(1,len(CMD)+1):
    frames.append((term('<span class="p">$</span> '+CMD[:i]), 70))
frames.append((term('<span class="p">$</span> '+CMD), 450))
base='<span class="p">$</span> '+CMD+'\n'+html.escape(L1)+'\n\n'
frames.append((term(base, False), 900))
frames.append((term(base+A1, False), 900))
frames.append((term(base+A1+'\n'+A2, False), 1500))
frames.append((term(base+A1+'\n'+A2+'\n'+END, False), 2600))
card='<div class="card"><h1>Your AI said <span class="y">done</span>.<br>story-gate checked.</h1><p>Every goal runs for real before a pull request can merge.<br>github.com/SathiaAI/story-gate</p></div>'
frames.append((card, 2800))
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        pg=await b.new_page(viewport={"width":960,"height":540}, device_scale_factor=1)
        imgs=[]
        for i,(h,ms) in enumerate(frames):
            await pg.set_content(f"<html><head><style>{CSS}</style></head><body>{h}</body></html>")
            await pg.evaluate("document.fonts.ready"); await pg.wait_for_timeout(60)
            f=f"/tmp/sg-gif-{i:03d}.png"; await pg.screenshot(path=f); imgs.append((f,ms))
        await b.close()
    ims=[Image.open(f).convert("RGB") for f,_ in imgs]
    both=Image.new("RGB",(960,1080)); both.paste(ims[-2],(0,0)); both.paste(ims[-1],(0,540))
    pal=both.quantize(colors=96, method=Image.Quantize.MEDIANCUT)
    q=[im.quantize(palette=pal, dither=Image.Dither.NONE) for im in ims]
    q[0].save("docs/assets/story-gate-try.gif", save_all=True, append_images=q[1:], duration=[ms for _,ms in imgs], loop=0, optimize=True)
asyncio.run(main())
