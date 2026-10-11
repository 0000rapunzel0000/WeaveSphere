"""Records the WeaveSphere walkthrough (docs/demo/weavesphere-walkthrough.mp4): the real app, scripted clicks and typing,
Chrome screencast frames at 2560x1440. Waits on the AI are cut (frames dropped while paused).

    cd backend && DB_PATH=/tmp/rec.db python3 demo_setup.py                        # fresh data, every take
    cd backend && DB_PATH=/tmp/rec.db SIM_SPEED=60 python3 -m uvicorn app:app --port 8100
    cd frontend && API_TARGET=http://127.0.0.1:8100 npx vite --port 5190 --strictPort
    REC_DB=/tmp/rec.db python3 docs/demo/record_walkthrough.py /tmp/take          # frames, frames.txt, marks.json
    ffmpeg -f concat -safe 0 -i /tmp/take/frames.txt -vf fps=30,format=yuv420p -c:v libx264 -preset slow -crf 16 out.mp4
(run ffmpeg from inside /tmp/take: frames.txt paths are relative to it)."""
import asyncio, base64, json, os, sys, time
from playwright.async_api import async_playwright

OUT = sys.argv[1]
URL = "http://localhost:5190/"
W, H, DPR = 1280, 720, 2          # CSS 1280x720 at 2x -> 2560x1440 frames: laptop-sized UI, sharp text
PW = "everest-demo"

frames, marks = [], []
st = {"paused": False, "offset": 0.0, "pause_at": 0.0, "t0": None, "xy": (W / 2, H / 2)}

CURSOR = """
window.__mv=(x,y,ms)=>{const c=document.getElementById('rec-cursor');if(!c)return;c.style.transitionDuration=ms+'ms';c.style.transform=`translate(${x}px,${y}px)`};
addEventListener('DOMContentLoaded',()=>{const c=document.createElement('div');c.id='rec-cursor';
c.innerHTML='<svg width="20" height="24" viewBox="0 0 20 24"><path d="M2 1.5 L2 19 L6.6 14.8 L9.7 21.8 L12.6 20.6 L9.6 13.7 L15.8 13.7 Z" fill="#141414" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>';
Object.assign(c.style,{position:'fixed',left:'-2px',top:'-1px',zIndex:2147483647,pointerEvents:'none',transform:'translate(%dpx,%dpx)',transitionProperty:'transform',transitionTimingFunction:'cubic-bezier(.45,0,.25,1)'});
document.documentElement.appendChild(c)});
"""


def now():
    return time.time() - st["offset"]


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(args=[f"--force-device-scale-factor={DPR}"])  # else headless screencast is 1x
        ctx = await b.new_context(viewport={"width": W, "height": H}, device_scale_factor=DPR)
        pg = await ctx.new_page()
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await ctx.add_init_script(CURSOR % (W / 2, H / 2))
        cdp = await ctx.new_cdp_session(pg)

        async def on_frame(e):
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]})
            except Exception:
                pass
            if st["paused"]:
                return
            t = e["metadata"]["timestamp"] - st["offset"]
            if st["t0"] is None:
                st["t0"] = t
            path = f"{OUT}/f{len(frames):06d}.jpg"
            with open(path, "wb") as f:
                f.write(base64.b64decode(e["data"]))
            frames.append((t, path))

        cdp.on("Page.screencastFrame", lambda e: asyncio.ensure_future(on_frame(e)))

        async def start_cast():
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W * DPR, "maxHeight": H * DPR, "everyNthFrame": 1})

        def mark(label):
            marks.append((label, now()))
            print(f"[{now() - (st['t0'] or now()):6.1f}s] {label}", flush=True)

        async def wait(s):
            await pg.wait_for_timeout(int(s * 1000))

        async def nudge():  # repaint so the screencast sends the current screen after a cut
            x, y = st["xy"]
            await pg.evaluate("([x,y])=>__mv(x,y,0)", [x + 0.5, y])
            await wait(0.05)
            await pg.evaluate("([x,y])=>__mv(x,y,0)", [x, y])

        async def cut(coro):
            """Drop the frames while waiting on the AI: the video jumps straight to the answer."""
            st["paused"], st["pause_at"] = True, time.time()
            try:
                await coro
            finally:
                await wait(0.4)
                st["offset"] += time.time() - st["pause_at"]
                st["paused"] = False
                await nudge()

        async def scroll_to(sel, top=76):
            log("scroll_to", sel)
            loc = pg.locator(sel).first
            await loc.wait_for(state="attached")
            y = await loc.evaluate("(el,top)=>el.getBoundingClientRect().top+scrollY-top", top)
            await pg.evaluate("y=>scrollTo({top:Math.max(0,y),behavior:'smooth'})", y)
            await wait(1.1)

        async def top():
            await pg.evaluate("scrollTo({top:0,behavior:'smooth'})")
            await wait(0.9)

        async def into_view(loc):
            box = await loc.bounding_box()
            if not box or box["y"] < 70 or box["y"] + box["height"] > H - 20:
                y = await loc.evaluate("el=>el.getBoundingClientRect().top+scrollY-innerHeight/2+el.getBoundingClientRect().height/2")
                await pg.evaluate("y=>scrollTo({top:Math.max(0,y),behavior:'smooth'})", y)
                await wait(0.9)

        async def move(loc, ms=650):
            await into_view(loc)
            box = await loc.bounding_box()
            x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
            await pg.evaluate("([x,y,ms])=>__mv(x,y,ms)", [x, y, ms])
            await pg.mouse.move(x, y)
            st["xy"] = (x, y)
            await wait(ms / 1000 + 0.15)

        def log(*a):
            print(f"  {time.strftime('%H:%M:%S')} {now() - (st['t0'] or now()):6.1f}", *a, flush=True)

        def L(sel):
            return pg.locator(sel).first

        async def click(sel, after=0.6):
            log("click", sel)
            loc = L(sel) if isinstance(sel, str) else sel
            if await pg.locator("#modal[open]").count() and not await loc.evaluate("el=>!!el.closest('#modal')"):
                log("closing a leftover popup")
                await pg.keyboard.press("Escape")
                await wait(0.6)
            await move(loc)
            await loc.click()
            await wait(after)

        async def typ(sel, text, delay=45):
            log("typ", sel)
            loc = L(sel)
            await move(loc)
            await loc.click()
            await loc.fill("")
            await pg.keyboard.type(text, delay=delay)
            await wait(0.3)

        async def choose(sel, label=None, index=None, has=None):
            log("choose", sel)
            loc = L(sel)
            await move(loc, 500)
            if has is not None:
                v = await loc.evaluate("(s,t)=>[...s.options].find(o=>o.text.includes(t)).value", has)
                await loc.select_option(value=v)
            elif index is not None:
                await loc.select_option(index=index)
            else:
                await loc.select_option(label=label)
            await wait(0.5)

        async def go(name):
            await click(f"#sideNav [data-page={name}]", after=0)
            await pg.wait_for_function("!document.body.classList.contains('busy')")
            await wait(1.0)

        async def settled():
            await pg.wait_for_function("!document.body.classList.contains('busy')")

        # ------------------------------------------------------------------ the film
        await pg.goto(URL)
        await wait(4.5)  # the opening finishes before recording starts
        await start_cast()
        await wait(0.5)
        await nudge()

        mark("01 Landing")
        await wait(3)
        for _ in range(2):
            await pg.evaluate("scrollBy({top:innerHeight*0.9,behavior:'smooth'})")
            await wait(2.6)
        await top()
        await click("button:has-text('SIGN IN ↗') >> visible=true", after=1.2)

        mark("02 Sign in")
        await typ("#loginIdentity", "owner")
        await typ("#loginPassword", PW, delay=35)
        await click(".login-submit", after=0)
        await pg.wait_for_selector("#sideNav [data-page=Inbox]")
        await cut(pg.wait_for_function("document.querySelector('#digest') && !/Building|Rebuilding/.test(document.querySelector('#digest').textContent)", timeout=90000))

        mark("03 Dashboard")
        await wait(7)
        await scroll_to("section.panel:has(h2:text-is('Machine status board'))")
        await wait(6)
        await scroll_to(".dashboard-grid")
        await wait(3.5)

        mark("04 Orders")
        await go("Orders")
        await choose("form[data-form=order] [name=buyer_id]", "Meridian Apparel")
        await choose("form[data-form=order] [name=fabric_type_id]", "Organic poplin")
        await typ("form[data-form=order] [name=colour]", "White")
        await typ("form[data-form=order] [name=qty_m]", "500")
        await move(L("form[data-form=order] [name=deadline]"))
        await L("form[data-form=order] [name=deadline]").fill("2026-12-20")
        await typ("form[data-form=order] [name=price_per_m]", "480")
        await click("form[data-form=order] button.primary", after=0)
        await cut(settled())
        await top()
        await wait(4)
        await scroll_to("section.panel:has(h2:text-is('Order book'))")
        await wait(1.5)
        await click("tr:has(td:text-is('#2')) [data-act=risk]", after=0)
        await cut(pg.wait_for_function("!/Asking Jev/.test(document.querySelector('#feasibilityResult').textContent)", timeout=90000))
        await scroll_to("#feasibilityResult", top=150)
        await wait(5)

        mark("05 Inventory")
        await go("Inventory")
        await scroll_to("section.panel:has(h2:text-is('Live yarn stock'))")
        await wait(3)
        await scroll_to("section.panel:has(h2:text-is('Purchase orders'))")
        await wait(4.5)
        await click("[data-act=po-approve]", after=0)
        await cut(settled())
        await wait(1)
        await top()
        await wait(1.5)
        await choose("form[data-form=grn] [name=po_line_id]", index=1)
        await typ("form[data-form=grn] [name=challan_no]", "CH-4471")
        await click("form[data-form=grn] button.primary", after=0)
        await cut(settled())
        await top()
        await wait(3.5)

        mark("06 Production")
        await go("Production")
        await wait(1.5)
        await scroll_to("section.panel:has(h2:text-is('Machine status board'))")
        await wait(6)
        await scroll_to("form[data-form=breakdown]", top=160)
        await choose("form[data-form=breakdown] [name=machine_id]", "Loom 04")
        await typ("form[data-form=breakdown] [name=note]", "Belt snapped on the take-up roller, loom stopped")
        await click("form[data-form=breakdown] button.primary", after=0)
        await cut(settled())
        await top()
        await wait(4.5)
        await scroll_to("form[data-form=production]")
        await choose("form[data-form=production] [name=machine_id]", "Loom 01 · weaving")
        await choose("form[data-form=production] [name=order_id]", has="#2 ")
        await choose("form[data-form=production] [name=operator_id]", "Meena Devi")
        await choose("form[data-form=production] [name=shift_id]", "Day")
        await typ("form[data-form=production] [name=output]", "160")
        await click("form[data-form=production] [name=make_roll]", after=0.4)
        await click("form[data-form=production] button.primary", after=0)
        await cut(settled())
        if await pg.locator("#modal[open]").count():  # the new roll's QR label
            await wait(5)
            await pg.keyboard.press("Escape")
            await wait(0.8)
        await top()
        await wait(4)

        mark("07 Quality")
        await go("Quality")
        await wait(2.5)
        await click("tr:has(td:text-is('ETM-2004')) [data-act=qc-pass]", after=0)
        await cut(settled())
        await wait(2)
        await typ("form[data-form=qc] [name=qr]", "ETM-2003")
        await choose("form[data-form=qc] [name=result]", "Reject")
        await typ("form[data-form=qc] [name=notes]", "Small holes near the selvedge every few metres")
        await click("form[data-form=qc] button.primary", after=0)
        await cut(settled())
        await top()
        await wait(5)
        await scroll_to("section.panel:has(h2:text-is('Defect hotspots'))")
        await wait(4.5)
        await scroll_to("section.panel:has(h2:text-is('Where defects come from'))")
        await wait(3)

        mark("08 Traceability")
        await go("Traceability")
        await typ("form[data-form=trace] [name=qr]", "ETM-1003")
        await click("form[data-form=trace] button.primary", after=0)
        await settled()
        await wait(5)
        await scroll_to("section.panel:has(h2:text-is('Yarn issued to the order'))", top=120)
        await wait(4.5)
        await click("[data-act=roll-qr]", after=0)
        await pg.wait_for_selector("#modal[open]")
        await wait(4)
        await pg.keyboard.press("Escape")
        await wait(1)

        mark("09 Dispatch")
        await go("Dispatch")
        await scroll_to("section.panel:has(h2:text-is('Finished goods'))")
        await wait(4)
        await top()
        await choose("form[data-form=dispatch] [name=order_id]", has="#1 ")
        await typ("form[data-form=dispatch] [name=vehicle_no]", "TN 38 AB 1234")
        await typ("form[data-form=dispatch] [name=driver]", "Selvam")
        await click("form[data-form=dispatch] button.primary", after=0)
        await cut(settled())
        await scroll_to("form[data-form=scan]", top=140)
        await click("[data-act=load-roll][data-qr='ETM-1001']", after=0)
        await settled()
        await wait(1)
        await click("[data-act=load-roll][data-qr='ETM-1002']", after=0)
        await settled()
        await wait(1)
        await typ("form[data-form=scan] [name=qr]", "ETM-9001")
        await click("form[data-form=scan] button.primary", after=0)
        await settled()
        await wait(3.5)
        await scroll_to("form[data-form=scan]", top=140)
        await wait(1)
        await click("[data-act=close-dispatch]", after=0)
        await cut(settled())
        if await pg.locator("#modal[open]").count():  # the invoice it raised
            await wait(6)
            await pg.keyboard.press("Escape")
            await wait(0.8)
        await top()
        await wait(4)

        mark("10 Accounts")
        await go("Accounts")
        await wait(4)
        await scroll_to("section.panel:has(h2:text-is('Cost per order'))")
        await wait(6)
        await scroll_to("section.panel:has(h2:text-is('Buyer receivables'))")
        await wait(4)

        mark("11 Inbox")
        await go("Inbox")
        await choose("form[data-form=message] [name=buyer_id]", "North Loom Studio")
        await choose("form[data-form=message] [name=order_id]", has="#2 ")
        await typ("form[data-form=message] [name=text]", "Hi, when will the navy twill be ready? Our cutting starts on 1 November.", delay=30)
        await click("form[data-form=message] button.primary", after=0)
        await cut(settled())
        await scroll_to("section.panel:has(h2:text-is('Messages'))")
        await wait(5)
        await choose("section.panel:has(h2:text-is('Messages')) tbody tr >> nth=0 >> select", "TA")
        await click(pg.locator("section.panel:has(h2:text-is('Messages')) tbody tr").first.locator("[data-act=draft]"), after=0)
        await cut(pg.wait_for_selector("#modal[open]", timeout=90000))
        await wait(7)
        await pg.keyboard.press("Escape")
        await wait(0.8)

        mark("12 Reviews")
        await go("Reviews")
        await wait(4)
        await click("[data-act=review-save] >> nth=0", after=0)
        await cut(settled())
        await wait(3.5)

        mark("13 Assistant")
        await go("Assistant")
        await typ("#chatInput", "set #5 status to running, also assign Arun Kumar instead of Lakshmi, free Arun Kumar if he has other jobs", delay=28)
        await click("form[data-form=assistant] button.primary", after=0)
        await cut(pg.wait_for_function("!document.querySelector(\'.chat-msg.typing\') && !document.body.classList.contains(\'busy\')", timeout=180000))
        await wait(9)
        last = await pg.locator(".chat-msg.bot").last.text_content()
        if "yes" in (last or "").lower():
            await typ("#chatInput", "yes")
            await click("form[data-form=assistant] button.primary", after=0)
            await cut(pg.wait_for_function("!document.querySelector(\'.chat-msg.typing\') && !document.body.classList.contains(\'busy\')", timeout=180000))
            await wait(7)
        await typ("#chatInput", "how much polyester is left?", delay=30)
        await click("form[data-form=assistant] button.primary", after=0)
        await cut(pg.wait_for_function("!document.querySelector(\'.chat-msg.typing\') && !document.body.classList.contains(\'busy\')", timeout=180000))
        await wait(6)
        await scroll_to("section.panel:has(h2:text-is('What the assistant did'))")
        await wait(6)

        mark("14 Workforce")
        await go("Workforce")
        await wait(2)
        await scroll_to(".register-wrap", top=140)
        await wait(4)
        await scroll_to("form[data-form=user]")
        await typ("form[data-form=user] [name=username]", "ravi")
        await typ("form[data-form=user] [name=password]", "store-ravi-2026", delay=30)
        await choose("form[data-form=user] [name=role]", "Store Keeper")
        await typ("form[data-form=user] [name=email]", "ravi@everestmills.in", delay=30)
        await typ("form[data-form=user] [name=phone]", "+91 98400 55123", delay=30)
        await click("form[data-form=user] button.primary", after=0)
        await settled()
        await scroll_to("section.panel:has(h2:text-is('Staff accounts'))")
        await wait(4)

        mark("15 Buyer page")
        import sqlite3
        token = sqlite3.connect(os.environ["REC_DB"]).execute("SELECT token FROM orders WHERE id = 2").fetchone()[0]
        app_url = pg.url
        await pg.goto(URL + "track.html#track=" + token)
        await pg.wait_for_load_state("networkidle")
        await pg.evaluate("([x,y])=>__mv(x,y,0)", list(st["xy"]))
        await wait(5)
        await move(L("#call"), 900)
        await wait(6)
        await pg.evaluate("scrollBy({top:innerHeight*0.7,behavior:'smooth'})")
        await wait(4)

        mark("16 Roles")
        await pg.goto(app_url)
        await pg.wait_for_selector("#sideNav [data-page=Inbox]")
        await pg.evaluate("([x,y])=>__mv(x,y,0)", list(st["xy"]))
        await settled()
        await wait(1)
        await click("#roleSwitch", after=1)
        await typ("#loginIdentity", "qc")
        await typ("#loginPassword", PW, delay=35)
        await click(".login-submit", after=0)
        await pg.wait_for_selector("#sideNav [data-page=Quality]")
        await settled()
        await wait(6)
        mark("END")
        await wait(1.5)
        await cdp.send("Page.stopScreencast")
        await b.close()

    t0 = frames[0][0]
    with open(f"{OUT}/frames.txt", "w") as f:
        for i, (t, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 1.5
            f.write(f"file '{os.path.basename(path)}'\nduration {max(nxt - t, 0.001):.4f}\n")
        f.write(f"file '{os.path.basename(frames[-1][1])}'\n")
    json.dump([(l, round(t - t0, 2)) for l, t in marks], open(f"{OUT}/marks.json", "w"), indent=1)
    print("frames", len(frames), "length", round(frames[-1][0] - t0, 1))


asyncio.run(main())
