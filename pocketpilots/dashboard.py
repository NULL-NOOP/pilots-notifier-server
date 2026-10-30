"""HTML dashboard and browser-based API test tools."""

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from pocketpilots import config
from pocketpilots.routes import get_status


async def get_dashboard(request: Request):
    """Render the interactive pairing dashboard or its JSON health response."""
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(await get_status(request=request))

    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pocket Pilots Notification Hub</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1220; --panel:#121e31; --border:#263750; --text:#edf5ff; --muted:#9aabc1; --accent:#55c7f3; --green:#41d6a3; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(ellipse at 15% 0%,#17334b 0,transparent 42%),var(--bg); color:var(--text); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
    main { max-width:980px; margin:0 auto; padding:40px 20px 70px; }
    header { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:30px; }
    h1 { margin:0; font-size:clamp(1.6rem,4vw,2.4rem); letter-spacing:-.04em; }
    h2 { margin:0 0 10px; font-size:1.15rem; }
    p { color:var(--muted); margin:0 0 16px; }
    .brand { display:flex; align-items:center; gap:15px; }
    .plane { display:grid; place-items:center; width:52px; height:52px; border-radius:16px; background:#16344b; font-size:26px; }
    .badge { border:1px solid var(--border); border-radius:999px; padding:7px 12px; color:var(--green); white-space:nowrap; font-size:.85rem; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
    .card { padding:22px; background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--border); border-radius:18px; box-shadow:0 14px 40px #02081744; }
    .wide { grid-column:1/-1; }
    .row { display:flex; flex-wrap:wrap; gap:10px; }
    input,select,button { min-height:42px; border-radius:10px; font:inherit; }
    input,select { flex:1; min-width:140px; padding:8px 12px; color:var(--text); background:#0b1423; border:1px solid var(--border); }
    button { border:0; padding:8px 14px; cursor:pointer; color:#062034; font-weight:700; background:var(--accent); }
    button.secondary { color:var(--text); background:#263750; }
    button:hover { filter:brightness(1.1); }
    pre { max-height:250px; overflow:auto; margin:14px 0 0; padding:14px; border-radius:11px; background:#08101d; border:1px solid var(--border); color:#aee6ff; white-space:pre-wrap; overflow-wrap:anywhere; }
    details { margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
    summary { cursor:pointer; font-weight:650; }
    code { color:#b6e9ff; }
    @media(max-width:600px) { main{padding:24px 14px 50px} header{align-items:flex-start; flex-direction:column} }
  </style>
</head>
<body><main>
  <header>
    <div class="brand"><div class="plane">✈</div><div><h1>Pocket Pilots</h1><p style="margin:4px 0 0">Notification Hub · Pairing and flight telemetry</p></div></div>
    <div class="badge">● Service online</div>
  </header>
  <div class="grid">
    <section class="card wide">
      <h2>Pairing code</h2><p>Create a link code or inspect an existing session. Keep your code private.</p>
      <div class="row"><button onclick="generateCode()">Generate new code</button><input id="targetCode" placeholder="Enter pairing code" maxlength="19"><button class="secondary" onclick="inspectCode()">Check status</button><button class="secondary" onclick="sendTestPush()">Send test push</button></div>
      <pre id="dash-log">Results will appear here.</pre>
    </section>
    <section class="card wide">
      <h2>Flight telemetry simulator</h2><p>Send a sample flight update to the selected pairing code.</p>
      <div class="row"><input id="simOrigin" value="JFK" aria-label="Origin"><input id="simDest" value="LAX" aria-label="Destination"><input id="simPlane" value="Boeing 737-800" aria-label="Aircraft"><select id="simStage" aria-label="Flight stage"><option value="1">Takeoff</option><option value="2" selected>Ascent</option><option value="3">Cruise</option><option value="4">Descent</option><option value="5">Landed</option></select><button onclick="sendSimulatedFlight()">Dispatch webhook</button></div>
      <pre id="sim-log">Telemetry response will appear here.</pre>
    </section>
    <section class="card">
      <h2>Connect the extension</h2><p>Save the private pairing code in extension storage and send it as the <code>X-Pair-Code</code> header with each <code>/webhook/flight</code> request. Avoid query parameters, which can be recorded in server and proxy logs.</p>
      <details><summary>Example request</summary><pre>POST /webhook/flight
    X-Pair-Code: YOUR_CODE
Content-Type: application/json

{"id":"FL100","t0":1730000000,"now":1730000500,"origin":"JFK","dest":"LAX","planeType":"B738","trk":[0,0,0,0]}</pre></details>
    </section>
    <section class="card">
      <h2>Connect the mobile app</h2><p>Register the device’s Firebase Cloud Messaging token with <code>POST /register-token</code>. The response includes the pairing code to share with the extension.</p>
      <details><summary>Example body</summary><pre>{"token":"FCM_TOKEN","device_id":"Pixel_8","pair_code":"YOUR_CODE"}</pre></details>
    </section>
    <section class="card wide">
      <h2>API quick reference</h2><p><code>POST /register-token</code> · <code>POST /unregister-token</code> · <code>POST /webhook/flight</code> · <code>POST /pair/new</code> · <code>GET /pair/{code}</code> · <code>POST /pair/{code}/test</code> · <code>GET /status</code></p>
    </section>
  </div>
</main>
<script>
async function requestJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}
async function generateCode() {
  const log = document.getElementById('dash-log'); log.textContent = 'Generating code…';
  try { const data = await requestJson('/pair/new', {method:'POST'}); document.getElementById('targetCode').value = data.pair_code; log.textContent = JSON.stringify(data,null,2); }
  catch (error) { log.textContent = 'Error: ' + error.message; }
}
async function inspectCode() {
  const code = document.getElementById('targetCode').value.trim(); const log = document.getElementById('dash-log');
  if (!code) { log.textContent = 'Enter a pairing code first.'; return; }
  try { log.textContent = JSON.stringify(await requestJson('/pair/'+encodeURIComponent(code)),null,2); }
  catch (error) { log.textContent = 'Error: ' + error.message; }
}
async function sendTestPush() {
  const code = document.getElementById('targetCode').value.trim(); const log = document.getElementById('dash-log');
  if (!code) { log.textContent = 'Enter a pairing code first.'; return; }
  try { log.textContent = JSON.stringify(await requestJson('/pair/'+encodeURIComponent(code)+'/test',{method:'POST'}),null,2); }
  catch (error) { log.textContent = 'Error: ' + error.message; }
}
async function sendSimulatedFlight() {
  const code = document.getElementById('targetCode').value.trim(); const log = document.getElementById('sim-log');
  const stage = Number(document.getElementById('simStage').value); const now = Date.now();
  const elapsed = [0,15,300,1800,3300,3600][stage]; const t0 = now-elapsed*1000;
  const payload = {id:'SIM_'+Math.floor(now/1000),t0,now,origin:document.getElementById('simOrigin').value.trim()||'JFK',dest:document.getElementById('simDest').value.trim()||'LAX',planeType:document.getElementById('simPlane').value.trim()||'B738',trk:[0,40.64,-73.77,0,900,41,-78,35000,2700,36,-115,35000,3600,33.94,-118.4,0]};
  if (code) payload.pair_code = code;
  try { log.textContent = JSON.stringify(await requestJson('/webhook/flight'+(code?'?code='+encodeURIComponent(code):''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),null,2); }
  catch (error) { log.textContent = 'Error: ' + error.message; }
}
</script></body></html>"""
    status_text = (
      "Initialized (Live FCM)"
      if config.FIREBASE_INITIALIZED
      else "Simulation Mode (No Key)"
    )
    return HTMLResponse(content=page.replace("● Service online", f"● {status_text}"))
