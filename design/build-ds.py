#!/usr/bin/env python3
"""Generate Claude Design preview cards from the ompire mockup stylesheet."""
import os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ompire-ui.html")
OUT = os.path.join(HERE, "ds-bundle", "previews")
os.makedirs(OUT, exist_ok=True)

html = open(SRC).read()
m = re.search(r"<style>\n(.*?)\n</style>", html, re.S)
assert m, "stylesheet not found"
TOKENS = m.group(1)

EXTRA = '''
.ds-pad { padding: 18px; display: flex; flex-direction: column; gap: 16px; align-items: flex-start; }
.ds-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.ds-label { font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--faint); }
.ds-note { font-size: 11.5px; color: var(--muted); max-width: 62ch; }
.sw-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; width: 100%; }
.sw { border: 1px solid var(--line); border-radius: 7px; overflow: hidden; background: var(--surface); }
.sw .swatch { height: 42px; }
.sw .swm { padding: 7px 9px; font-family: var(--mono); font-size: 10.5px; color: var(--muted); }
.sw .swm b { display: block; color: var(--text); font-size: 11px; }
'''

RING_JS = '''document.querySelectorAll('.ring .arc').forEach(arc => {
  const pct = parseFloat(arc.dataset.pct) || 0;
  const c = 2 * Math.PI * 8;
  arc.setAttribute('stroke-dasharray', (c * pct / 100).toFixed(2) + ' ' + c.toFixed(2));
});'''

def emit(fname, group, title, body, js=""):
    script = f"<script>{js}</script>\n" if js else ""
    doc = (f'<!-- @dsCard group="{group}" -->\n'
           '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           f'<title>{title}</title>\n'
           f'<style>\n{TOKENS}\n</style>\n<style>{EXTRA}</style>\n'
           '</head>\n<body>\n'
           f'<div class="ds-pad">\n{body}\n</div>\n{script}'
           '</body>\n</html>\n')
    open(os.path.join(OUT, fname), "w").write(doc)
    print("wrote", fname)

def sw(var, name, use):
    return (f'<div class="sw"><div class="swatch" style="background:var(--{var})"></div>'
            f'<div class="swm"><b>--{var}</b>{name} — {use}</div></div>')

# ---------- Colors ----------
body = f'''
<div class="ds-label">Semantic hues — hue carries meaning, never attention level</div>
<div class="sw-grid">
{sw("red", "red", "dead: failed")}
{sw("amber", "amber", "blocked / suspect: waiting-approval, stalled")}
{sw("cyan", "cyan", "question: waiting-input, gate")}
{sw("violet", "violet", "reviewing; session tags")}
{sw("green", "green", "ok / shipped / done steps")}
</div>
<div class="ds-label">Interactive accent — outside the semantic set</div>
<div class="sw-grid">
{sw("accent", "accent", "actions, focus, links, current workflow step")}
</div>
<div class="ds-label">Neutrals</div>
<div class="sw-grid">
{sw("bg", "bg", "page ground")}
{sw("surface", "surface", "cards, panels")}
{sw("surface2", "surface2", "inputs, tool cards")}
{sw("line", "line", "borders")}
{sw("line-soft", "line-soft", "inner dividers")}
{sw("text", "text", "primary text")}
{sw("muted", "muted", "secondary text")}
{sw("faint", "faint", "metadata, marks")}
</div>
<div class="ds-note">Attention is encoded on two axes that never mix: <b>tier is structural</b> (fill, spine, tint — see State pills), <b>hue is semantic</b>. Tokens are theme-aware: dark values swap in via <code>prefers-color-scheme</code> and <code>data-theme</code>.</div>
'''
emit("colors.html", "Colors", "ompire — semantic hues & neutrals", body)

# ---------- Type ----------
body = '''
<div class="ds-label">Identity — terminal-native, 13px base, monospace carries identity</div>
<div class="logo" style="font-size:16px"><span class="glyph">»</span>ompire</div>
<div class="ds-label">Mono roles</div>
<div class="slug" style="font-size:14px">bjornt/fix-dhcp-races</div>
<div class="card-meta" style="border:none;padding:0;margin:0"><span class="m">2.1M · $6.40</span><span class="m">1h 12m</span><span class="m">fable-5 · think:high</span></div>
<div class="activity">bash: pytest src/maasserver/tests/test_racks.py -x</div>
<div class="wf-trail"><span class="wf-name">bugfix</span><span class="ws done">reproduce ✓</span>→<span class="ws cur">fix</span>→<span class="ws">validate</span>→<span class="ws">ship</span></div>
<div class="eyebrow" style="width:100%;margin:0"><span>Needs you</span><span class="count">6</span><span class="rule"></span></div>
<div class="ds-label">Body — sans, 13px / 1.5</div>
<div class="t-text" style="font-size:13px"><p>The race: <code>configure_dhcp()</code> takes the subnet config lock and then talks OMAPI, while the rack-notify path did OMAPI I/O first and locked second — classic lock-order inversion.</p></div>
<div class="ds-note">Sans (system-ui) for prose and labels; mono (ui-monospace) for anything the operator greps: slugs, branches, costs, commands, reasons, workflow trails.</div>
'''
emit("type.html", "Type", "ompire — typography", body)

# ---------- State pills ----------
body = '''
<div class="ds-label">Tier 1 · interrupt — solid fill, pulsing dot (blocked or dead)</div>
<div class="ds-row">
  <span class="pill t-interrupt hue-red"><span class="d"></span>failed</span>
  <span class="pill t-interrupt hue-amber"><span class="d"></span>waiting-approval</span>
</div>
<div class="ds-label">Tier 2 · notify — hue outline, steady dot</div>
<div class="ds-row">
  <span class="pill t-notify hue-cyan"><span class="d"></span>waiting-input</span>
  <span class="pill t-notify hue-cyan"><span class="d"></span>gate</span>
  <span class="pill t-notify hue-violet"><span class="d"></span>reviewing</span>
  <span class="pill t-notify hue-amber"><span class="d"></span>stalled</span>
</div>
<div class="ds-label">Tier 3 · badge — neutral chip, hollow dot</div>
<div class="ds-row">
  <span class="pill t-badge"><span class="d"></span>idle</span>
  <span class="pill t-badge"><span class="d"></span>retrying</span>
  <span class="pill t-badge hue-green" style="color:var(--green)"><span class="d"></span>shipped</span>
</div>
<div class="ds-label">Tier 4 · silent — text only, breathing dot</div>
<div class="ds-row">
  <span class="pill t-silent anim"><span class="d"></span>working</span>
  <span class="pill t-silent anim"><span class="d"></span>starting</span>
</div>
<div class="ds-label">Workflow step prefix (Decision 8) — task state = current step + session state</div>
<div class="ds-row">
  <span class="pill t-notify hue-cyan"><span class="d"></span><span class="step">fix:</span>waiting-input</span>
  <span class="pill t-silent anim"><span class="d"></span><span class="step">validate:</span>working</span>
</div>
<div class="ds-note">Tier is structural, hue is semantic — stalled and waiting-approval share amber; the tier structure disambiguates. Single-step tasks show no prefix.</div>
'''
emit("pills.html", "Components", "ompire — state pills (tier grammar)", body)

# ---------- Chips & buttons ----------
body = '''
<div class="ds-label">Chrome chips — global indicators</div>
<div class="ds-row">
  <span class="chip chip-attn">6 need you</span>
  <span class="chip chip-ok"><span class="dot"></span>daemon</span>
  <span class="chip chip-gpg-cached"><span class="dot" style="background:var(--green)"></span>gpg 2h58m</span>
  <span class="chip chip-gpg-locked"><span class="dot"></span>gpg locked</span>
</div>
<div class="ds-label">Buttons</div>
<div class="ds-row">
  <button class="btn btn-primary">Spawn task</button>
  <button class="btn">Open</button>
  <button class="btn btn-quiet">Archive</button>
  <button class="btn btn-quiet btn-danger-quiet">Kill</button>
  <button class="btn btn-sm">Re-check key</button>
  <button class="btn btn-primary" disabled>Sign &amp; commit</button>
</div>
<div class="ds-label">Quick-answer options (dashboard ask card)</div>
<div class="ds-row">
  <button class="qa-opt rec">Yes, both loops</button>
  <button class="qa-opt">v4 only</button>
  <button class="qa-opt">Open for details…</button>
</div>
<div class="ds-note">The teal accent is interactive-only; semantic hues never appear on buttons except the quiet danger variant.</div>
'''
emit("chips-buttons.html", "Components", "ompire — chips & buttons", body)

# ---------- Task cards ----------
body = '''
<div class="card-grid" style="width:100%;max-width:420px;grid-template-columns:1fr">
  <article class="card t1 hue-red">
    <div class="spine"></div>
    <div class="card-top">
      <span class="proj">llmvet</span>
      <span class="spacer"></span>
      <span class="pill t-interrupt hue-red"><span class="d"></span>failed</span>
    </div>
    <div class="slug">bjornt/range-mode</div>
    <div class="reason"><strong>Process exited 1</strong> — retries exhausted (3/3) <span class="why">reason: fatal stderr</span></div>
    <div class="stderr"><b>error:</b> pi-auth-gateway 127.0.0.1:4000: connection refused</div>
    <div class="card-meta">
      <span class="m"><svg class="ring" viewBox="0 0 20 20"><circle class="track" cx="10" cy="10" r="8"/><circle class="arc" cx="10" cy="10" r="8" data-pct="34"/></svg>34%</span>
      <span class="m">612k · $1.87</span>
      <span class="m">47m</span>
      <span class="spacer"></span>
      <span class="card-actions"><button class="btn btn-sm">Open</button><button class="btn btn-sm">Respawn</button></span>
    </div>
  </article>

  <article class="card t2 hue-cyan">
    <div class="spine"></div>
    <div class="card-top">
      <span class="proj">maas</span>
      <span class="spacer"></span>
      <span class="pill t-notify hue-cyan"><span class="d"></span>gate</span>
    </div>
    <div class="slug">bjornt/subnet-dup-detect</div>
    <div class="wf-trail"><span class="wf-name">bugfix</span><span class="ws done">reproduce ✓</span>→<span class="ws done">fix ✓</span>→<span class="ws fail">validate ✗ ×3</span>→<span class="ws cur">gate</span></div>
    <div class="reason"><strong>Fix ↔ validate loop exhausted</strong> — repro still fails 4/200 <span class="why">loop bound 3 · escalated 8m ago</span></div>
    <div class="card-meta">
      <span class="m"><svg class="ring" viewBox="0 0 20 20"><circle class="track" cx="10" cy="10" r="8"/><circle class="arc" cx="10" cy="10" r="8" data-pct="71"/></svg>71%</span>
      <span class="m">1.8M · $5.62</span>
      <span class="m">1h 44m</span>
      <span class="spacer"></span>
      <span class="card-actions"><button class="btn btn-sm btn-primary">Open</button><button class="btn btn-sm">Retry loop</button></span>
    </div>
  </article>

  <article class="card t4">
    <div class="card-top">
      <span class="proj">maas</span>
      <span class="spacer"></span>
      <span class="pill t-silent anim"><span class="d"></span>working</span>
    </div>
    <div class="slug">bjornt/rack-controller-tests</div>
    <div class="activity">bash: pytest src/maasserver/tests/test_racks.py -x</div>
    <div class="shimmer"></div>
    <div class="card-meta">
      <span class="m"><svg class="ring" viewBox="0 0 20 20"><circle class="track" cx="10" cy="10" r="8"/><circle class="arc" cx="10" cy="10" r="8" data-pct="54"/></svg>54%</span>
      <span class="m">1.1M · $3.20</span>
      <span class="m">44m</span>
      <span class="spacer"></span>
      <span class="card-actions"><button class="btn btn-sm btn-quiet">Open</button></span>
    </div>
  </article>
</div>
<div class="ds-note">Tier is structural on the card too: t1 spine + tinted surface + primary action on the card; t2 spine only; t4 recessed. Multi-step tasks carry the workflow trail; single-step cards omit it.</div>
'''
emit("task-cards.html", "Components", "ompire — task cards", body, js=RING_JS)

# ---------- Workflow ----------
body = '''
<div class="ds-label">Workflow strip — task detail (Decision 8)</div>
<div class="wf-strip open" id="wfStrip" style="width:100%">
  <div class="wf-steps">
    <button class="wf-step done" onclick="document.getElementById('wfStrip').classList.toggle('open')">
      <span class="mark">✓</span>reproduce<span class="kind">agent</span><span class="sess">reproducer</span><span class="caret">▾ outcome</span>
    </button>
    <button class="wf-step cur"><span class="mark">2</span>fix<span class="kind">agent</span><span class="sess">coder</span></button>
    <button class="wf-step"><span class="mark">3</span>validate<span class="kind">agent</span><span class="sess">reproducer</span></button>
    <span class="wf-loop">⟲ fail → fix · max 3</span>
    <button class="wf-step"><span class="mark">4</span>ship<span class="kind">gate</span></button>
  </div>
  <div class="wf-outcome"><span class="ohead">.ompire/outcome.json — written by reproduce · read by the daemon on agent_end</span>
{ <span class="key">"status"</span>: <span class="str">"reproduced"</span>,
  <span class="key">"repro"</span>: <span class="str">"pytest src/tests/test_dhcp.py -k race -x --count 200"</span>,
  <span class="key">"expect"</span>: <span class="str">"≥1 failure on unfixed HEAD (observed 12/200)"</span> }</div>
</div>
<div class="ds-label">Session tabs — one transcript per named session</div>
<div class="session-tabs" style="margin-bottom:0">
  <button class="session-tab on"><span class="sd"></span>coder</button>
  <button class="session-tab s-idle"><span class="sd"></span>reproducer</button>
  <span class="st-note">2 sessions · one container · shared clone</span>
</div>
<div class="ds-label">Card trail — compact form on dashboard cards</div>
<div class="wf-trail"><span class="wf-name">bugfix</span><span class="ws done">reproduce ✓</span>→<span class="ws cur">fix</span>→<span class="ws">validate</span>→<span class="ws">ship</span></div>
<div class="ds-note">Steps show kind (agent / command / decision / gate) and their assigned named session; the current step uses the interactive accent, done steps green, sessions violet. Done agent steps expand to their outcome file.</div>
'''
emit("workflow.html", "Components", "ompire — workflow strip & session tabs", body)

# ---------- Transcript ----------
body = '''
<div class="transcript" style="width:100%;max-width:640px">
  <div class="t-entry t-user">
    <div class="t-role">workflow · step 2 (fix) <span class="t-time">14:02</span></div>
    <div class="t-text"><p>Handoff from reproduce: <code>pytest -k race --count 200</code> fails 12/200 on HEAD. Fix the race; finish by writing <code>.ompire/outcome.json</code>.</p></div>
  </div>
  <div class="t-entry">
    <div class="t-role">omp <span class="t-time">14:05</span></div>
    <div class="thinking"><span class="t-label">thinking</span>Need to see which locks each side takes and in what order.</div>
    <div class="tool-card">
      <div class="tool-head" onclick="this.parentElement.classList.toggle('open')">
        <span class="tw">▶</span><span class="tname">grep</span><span class="targ">"omapi" src/maasserver/dhcp.py</span><span class="tstat">14 matches</span>
      </div>
      <div class="tool-body">src/maasserver/dhcp.py:412:    omapi_key = get_omapi_key(rack)</div>
    </div>
    <div class="tool-card open">
      <div class="tool-head" onclick="this.parentElement.classList.toggle('open')">
        <span class="tw">▶</span><span class="tname">edit</span><span class="targ">src/maasserver/dhcp.py</span><span class="tstat">+11 −3</span>
      </div>
      <div class="tool-body"><span class="del">-    def _update_hosts_via_omapi(server, key, hosts):</span>
<span class="add">+    with subnet_config_lock(server.subnet_id):</span></div>
    </div>
    <div class="ask-card">
      <div class="ask-head"><span class="d"></span>question — waiting on you · 2m</div>
      <div class="ask-q">Apply the same lock ordering to dhcpd6?</div>
      <div class="ask-opts">
        <button class="ask-opt rec"><span class="k">1</span><span><span class="lab">Yes, both loops</span><span class="desc">Same two-line change; parametrize the test over v4/v6.</span></span></button>
        <button class="ask-opt"><span class="k">2</span><span><span class="lab">v4 only</span><span class="desc">Keep this branch minimal.</span></span></button>
      </div>
      <div class="ask-other"><input type="text" placeholder="Other — free-text answer…"><button class="btn">Send</button></div>
    </div>
  </div>
</div>
<div class="composer" style="width:100%;max-width:640px">
  <textarea rows="2" placeholder="Steer the agent — delivered mid-turn without interrupting…"></textarea>
  <div class="composer-bar">
    <div class="mode-seg"><button class="on">steer</button><button>follow-up</button><button>interrupt + prompt</button></div>
    <span class="note">turn in flight — steer delivers now</span>
    <button class="btn btn-primary btn-sm">Send</button>
  </div>
</div>
<div class="ds-note">Tool cards collapse/expand; ask questions render as cyan question cards from the tool args and stay answerable while the turn is in flight.</div>
'''
emit("transcript.html", "Components", "ompire — transcript, ask card & composer", body)

# ---------- Pipeline ----------
body = '''
<div class="panel" style="width:100%;max-width:460px">
  <h2>Launching · maas/vlan-mtu-validation</h2>
  <div class="pipeline">
    <div class="pstep done">
      <span class="marker">✓</span>
      <div class="pbody">
        <div class="ptitle">Fetch + clone <span class="ptime">2.4s</span></div>
        <div class="pdetail mono" style="font-size:11px">git clone ~/proj/maas ~/tasks/maas/vlan-mtu-validation</div>
      </div>
    </div>
    <div class="pstep active">
      <span class="marker">2</span>
      <div class="pbody">
        <div class="ptitle">Workshop launch <span class="ptime">31s elapsed…</span></div>
        <div class="log-tail">launching container workshop-maas-vlan-mtu…
<span class="cur">installing omp SDK…</span></div>
      </div>
    </div>
    <div class="pstep pending">
      <span class="marker">3</span>
      <div class="pbody">
        <div class="ptitle">Session ready · <span class="mono">reproducer</span></div>
        <div class="pdetail">sessions spawn lazily — <span class="mono">coder</span> starts when the fix step first needs it</div>
      </div>
    </div>
    <div class="pstep pending">
      <span class="marker">4</span>
      <div class="pbody">
        <div class="ptitle">Step 1 prompted · reproduce</div>
        <div class="pdetail">template preamble + step prompt</div>
      </div>
    </div>
  </div>
</div>
<div class="ds-note">Same marker grammar as the ship stepper: green done, accent-pulsed active, dim pending, red failed (stderr expands inline).</div>
'''
emit("pipeline.html", "Components", "ompire — spawn pipeline", body)

# ---------- Full mockup as a Views card ----------
views = ('<!-- @dsCard group="Views" -->\n'
         '<!doctype html>\n<html>\n'
         '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>\n'
         '<body>\n' + html + '\n</body>\n</html>\n')
open(os.path.join(OUT, "views.html"), "w").write(views)
print("wrote views.html")

print("done:", len(os.listdir(OUT)), "files in", OUT)
