"""Rebuild assets/demo.gif from the two committed benchmark answers.

Both panels reveal the same number of characters per frame, so the only
difference on screen is how much there is to read. Requires rsvg-convert
and ffmpeg:

    python3 scripts/make_demo_gif.py

Palette matches assets/icon.png, assets/benchmark.svg and assets/pipeline.svg.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIF = os.path.join(ROOT, "assets", "demo.gif")

BG = "#0f1728"; CARD = "#131d31"; BORD = "#2c3b57"; MUT = "#c2b49a"
GOLD = "#e0a72e"; CREAM = "#f2e6cc"; DIM = "#8a8168"

LEFT_RAW = """**Security Review Findings**

**Finding 1: Broken Object-Level Authorization (IDOR) - GET /api/orders/:orderId**
**Location:** routes/orders.js:52
**Class:** CWE-639 (Authorization Bypass Through User-Controlled Key) / OWASP API1:2023 - Broken Object Level Authorization

**Description:**
The endpoint authenticates the request (valid session required) but does not authorize it - it never verifies that the fetched order.userId matches req.session.userId. Any authenticated user can enumerate or guess orderId values (sequential IDs, UUIDs leaked elsewhere, etc.) and retrieve other users' order data.

**Impact:**
- Horizontal privilege escalation: disclosure of other customers' order contents, shipping addresses, pricing, and any PII embedded in the order record.
- If order IDs are sequential/predictable, mass scraping of all orders in the system is trivial.

**Severity:** High

**Finding 2: Broken Object-Level Authorization (IDOR) - PATCH /api/orders/:orderId/status**
**Location:** routes/orders.js:80
**Class:** CWE-639 / OWASP API1:2023 - Broken Object Level Authorization

**Description:**
Same missing ownership check as Finding 1, but on a state-mutating endpoint. Any authenticated user can modify the status field of any other user's order by supplying its orderId.

**Impact:**
- Unauthorized write access: an attacker can cancel, mark-shipped, mark-delivered, or otherwise tamper with other users' orders.
- Business-logic abuse (e.g., prematurely marking orders "delivered" to trigger downstream automation, fraud, or dispute leverage).

**Severity:** High

**Finding 3: Missing Input Validation on status - PATCH /api/orders/:orderId/status**
**Location:** routes/orders.js:80
**Class:** CWE-20 (Improper Input Validation) / lack of allow-list enforcement for state transitions

**Description:**
The handler writes the incoming request value directly into order.status without validating it against an allow-list of legal states (e.g., pending, paid, shipped, cancelled, delivered) or enforcing valid state-transition rules.

**Impact:**
- Data integrity corruption: arbitrary/malformed strings can be persisted as order status, breaking downstream logic (fulfillment, reporting, notifications) that assumes a fixed enum.
- Illegal state transitions (e.g., jumping from pending directly to delivered, or reverting a cancelled order) may bypass business rules and any invariants other code paths depend on.
- Depending on how status is later used (rendered in UI/emails, used in DB queries, passed to downstream services), an unvalidated string could contribute to secondary issues (stored XSS if reflected unescaped, log injection, etc.) - worth checking consumers of this field.

**Severity:** Medium (compounds with Finding 2 into High when combined - unauthorized *and* unvalidated writes)

**Summary / Remediation Guidance**
Both endpoints need an ownership check (order.userId === req.session.userId, returning 403/404) before returning or mutating the resource. The PATCH endpoint additionally needs status validated against a fixed enum/allow-list and, ideally, a state-transition guard rather than an unconstrained overwrite."""

RIGHT_RAW = """**routes/orders.js:52 - GET /api/orders/:orderId**
- IDOR: authenticated user can read any order by ID (no order.userId === req.session.userId check).
- Fix: after fetch, if order.userId !== req.session.userId, return 403/404.

**routes/orders.js:80 - PATCH /api/orders/:orderId/status**
- IDOR: same missing ownership check - any authenticated user can modify any other user's order status.
- Fix: same ownership check before update.
- Unvalidated input: status accepted as arbitrary string, written directly to order.status - allows invalid/unexpected state values (data integrity risk, possible logic bypass if downstream code branches on status).
- Fix: whitelist allowed status values (enum check) before update; reject with 400 otherwise."""

WRAP = 60
def prep(raw):
    lines = []
    for p in raw.split("\n"):
        bold = p.startswith("**")
        p = p.replace("**", "")
        if not p:
            lines.append(("", False)); continue
        for i, w in enumerate(textwrap.wrap(p, WRAP) or [""]):
            lines.append((("  " + w) if (i and p.startswith("-")) else w, bold))
    return lines

L, R = prep(LEFT_RAW), prep(RIGHT_RAW)
LCH = sum(len(t) + 1 for t, _ in L)
RCH = sum(len(t) + 1 for t, _ in R)
LTOK, RTOK = 1215, 276

W, H = 960, 540
PX, PY, PW = 24, 96, 448          # panel geometry
PH = 396
LH, FS = 13.0, 10.5
VIS = int((PH - 34) / LH)
CPF = 46                          # chars revealed per frame, identical in both panels
HOLD = 26

def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def body(lines, chars):
    """Return (visible_lines, done) revealing `chars` characters."""
    out, used = [], 0
    for t, b in lines:
        if used >= chars: break
        take = min(len(t), chars - used)
        out.append((t[:take], b))
        used += len(t) + 1
    return out, used >= sum(len(t) + 1 for t, _ in lines)

def panel(x, title, lines, chars, tok_total, done_color):
    vis, done = body(lines, chars)
    shown = int(tok_total * min(1.0, chars / (LCH if lines is L else RCH)))
    scroll = max(0, len(vis) - VIS)
    s = [f'<rect x="{x}" y="{PY}" width="{PW}" height="{PH}" rx="12" fill="{CARD}" stroke="{BORD}"/>']
    s.append(f'<text x="{x+18}" y="{PY+26}" class="t">{esc(title)}</text>')
    badge = f'{shown} tokens' if not done else f'{tok_total} tokens'
    col = done_color if done else MUT
    s.append(f'<text x="{x+PW-18}" y="{PY+26}" text-anchor="end" class="b" fill="{col}">{esc(badge)}</text>')
    s.append(f'<clipPath id="c{x}"><rect x="{x+1}" y="{PY+36}" width="{PW-2}" height="{PH-44}"/></clipPath>')
    s.append(f'<g clip-path="url(#c{x})">')
    y = PY + 52
    for i, (t, b) in enumerate(vis[scroll:]):
        s.append(f'<text x="{x+18}" y="{y+i*LH:.1f}" class="m" fill="{CREAM if b else MUT}"'
                 f'{" font-weight=\"600\"" if b else ""}>{esc(t)}</text>')
    if done:
        s.append(f'<text x="{x+18}" y="{y+len(vis[scroll:])*LH+6:.1f}" class="m" fill="{done_color}">done.</text>')
    s.append('</g>')
    return "\n".join(s), done

OUT = tempfile.mkdtemp(prefix="simple-man-demo-")
total_frames = -(-LCH // CPF) + HOLD
for f in range(total_frames):
    ch = min(LCH, (f + 1) * CPF)
    lp, ldone = panel(PX, "no policy", L, ch, LTOK, MUT)
    rp, rdone = panel(PX + PW + 16, "Simple Man", R, min(RCH, ch), RTOK, GOLD)
    foot = ""
    if rdone:
        foot = (f'<text x="{W/2}" y="{H-16}" text-anchor="middle" class="f" fill="{MUT}">'
                f'Simple Man finished {esc("−")}77% earlier {esc("·")} same two findings, same locations, same fixes</text>'
                if not ldone else
                f'<text x="{W/2}" y="{H-16}" text-anchor="middle" class="f" fill="{GOLD}">'
                f'1,215 {esc("→")} 276 tokens {esc("·")} zero facts lost</text>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<style>
text {{ font-family: "Menlo", "DejaVu Sans Mono", monospace; }}
.t {{ font-size: 13px; font-weight: 700; fill: {CREAM}; font-family: -apple-system, "Helvetica", sans-serif; }}
.b {{ font-size: 11px; font-weight: 600; font-family: -apple-system, "Helvetica", sans-serif; }}
.m {{ font-size: {FS}px; }}
.p {{ font-size: 13px; fill: {CREAM}; }}
.f {{ font-size: 12.5px; font-weight: 600; font-family: -apple-system, "Helvetica", sans-serif; }}
</style>
<rect width="{W}" height="{H}" fill="{BG}"/>
<text x="{PX}" y="40" class="p"><tspan fill="{GOLD}">$</tspan> claude "security review of routes/orders.js"</text>
<text x="{PX}" y="64" style="font-size:11px;fill:{DIM};font-family:-apple-system,sans-serif;">same model (claude-sonnet-5) {esc("·")} same prompt {esc("·")} same streaming rate</text>
{lp}
{rp}
{foot}
</svg>'''
    svg_path = f"{OUT}/f{f:04d}.svg"
    open(svg_path, "w").write(svg)
    subprocess.run(["rsvg-convert", "-w", str(W), "-h", str(H), svg_path,
                    "-o", f"{OUT}/f{f:04d}.png"], check=True)

pal = f"{OUT}/palette.png"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "12", "-i", f"{OUT}/f%04d.png",
                "-vf", "palettegen=max_colors=64", pal], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "12", "-i", f"{OUT}/f%04d.png",
                "-i", pal, "-lavfi", "paletteuse=dither=bayer:bayer_scale=3",
                "-loop", "0", GIF], check=True)
shutil.rmtree(OUT)
print(f"{GIF}: {total_frames} frames, {os.path.getsize(GIF) / 1e6:.1f} MB")
