"""Disposable upstream for browser QA; listens on localhost only."""

import os

from aiohttp import WSMsgType, web

PAGE = """<!doctype html><html><head><title>Proxy verification</title><link rel="stylesheet" href="/style.css"></head><body>
<h1>Proxy verification</h1><p>Local test service behind Home Assistant</p>
<img srcset="/logo.svg 1x, /logo.svg 2x" src="/logo.svg" alt="Upstream graphic" width="64" height="64">
<button id="run">Run checks</button><ul id="results"></ul>
<form action="/submit" method="post"><label>Message <input name="message" value="Form works"></label><button type="submit">Submit form</button><button type="button" id="submit-native">Submit with requestSubmit()</button></form>
<a href="/download" download>Download test file</a>
<p><a href="/navigation">Test JavaScript navigation</a></p>
<script src="/app.js"></script></body></html>"""
NAVIGATION_PAGE = """<!doctype html><html><head><title>Navigation checks</title></head><body>
<h1>Navigation checks</h1>
<p id="result"></p>
<button id="root">Router login redirect</button>
<button id="assign">Assign URL</button>
<button id="replace">Replace URL</button>
<button id="hash">Hash navigation</button>
<a href="/">Back to demo</a>
<script>
document.getElementById('result').textContent = new URLSearchParams(location.search).get('check') || 'Ready';
document.getElementById('root').onclick = () => { location.href = '/'; };
document.getElementById('assign').onclick = () => location.assign('/navigation?check=assign#result');
document.getElementById('replace').onclick = () => location.replace('/navigation?check=replace#result');
document.getElementById('hash').onclick = () => { location.hash = 'hash-ok'; document.getElementById('result').textContent = 'Hash OK'; };
</script></body></html>"""
JS = """document.getElementById('submit-native').onclick = () => document.querySelector('form').requestSubmit();
document.getElementById('run').onclick = async () => {
  const results = document.getElementById('results'); results.textContent = '';
  const report = text => { const li = document.createElement('li'); li.textContent = text; results.append(li); };
  try {
    const response = await fetch('/api/check'); report('Fetch: ' + (await response.json()).status);
    document.cookie = 'script_cookie=works; Path=/';
    const cookies = await (await fetch('/api/cookies')).json();
    report('JavaScript cookie: ' + (cookies.script_cookie === 'works' && document.cookie.includes('script_cookie=works') ? 'OK' : 'FAIL'));
    report('HTTP cookie: ' + (cookies.server_cookie === 'works' ? 'OK' : 'FAIL'));
    const auth = await (await fetch('/api/auth', {headers: {Authorization: 'Bearer demo-application-token'}})).json();
    report('Fetch Bearer: ' + auth.status);
    const requestAuth = await (await fetch(new Request('/api/auth', {headers: {Authorization: 'Bearer demo-application-token'}}))).json();
    report('Request Bearer: ' + requestAuth.status);
    const upload = new FormData(); upload.append('message', 'Multipart works'); upload.append('file', new Blob(['upload contents']), 'test.txt');
    const uploaded = await (await fetch('/upload', {method: 'POST', body: upload})).json(); report('Multipart upload: ' + uploaded.status);
    const authXhr = new XMLHttpRequest(); authXhr.open('GET', '/api/auth'); authXhr.setRequestHeader('Authorization', 'Bearer demo-application-token'); authXhr.onload = () => report('XHR Bearer: ' + JSON.parse(authXhr.responseText).status); authXhr.send();
    const eventSource = new EventSource('/events'); eventSource.onmessage = event => { report('SSE: ' + event.data); eventSource.close(); };
    const dynamicImage = new Image(); dynamicImage.alt = 'Dynamic graphic'; dynamicImage.width = 32; dynamicImage.onload = () => report('Dynamic srcset: OK'); dynamicImage.onerror = () => report('Dynamic srcset: FAIL'); dynamicImage.srcset = '/logo.svg 1x, /logo.svg 2x'; results.append(dynamicImage);
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onopen = () => ws.send('OK'); ws.onmessage = event => { report('WebSocket: ' + event.data); ws.close(); };
    ws.onerror = () => report('WebSocket: FAIL');
    const xhr = new XMLHttpRequest(); xhr.open('GET', '/api/check'); xhr.onload = () => report('XHR: ' + JSON.parse(xhr.responseText).status); xhr.send();
  } catch (error) { report('FAIL: ' + error.message); }
};"""


async def handler(request):
    path = request.path
    if path == "/":
        response = web.Response(text=PAGE, content_type="text/html")
        response.set_cookie("server_cookie", "works", httponly=True)
        return response
    if path == "/app.js":
        return web.Response(text=JS, content_type="application/javascript")
    if path == "/navigation":
        return web.Response(text=NAVIGATION_PAGE, content_type="text/html")
    if path == "/style.css":
        return web.Response(
            text="body {font-family:system-ui;padding:24px;color:#243746;background:#f7fafb} button,input {font:inherit;padding:10px} li {margin:12px 0} form {margin:24px 0}",
            content_type="text/css",
        )
    if path == "/logo.svg":
        return web.Response(
            text='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#18bc9c"/><path d="m16 32 10 10 23-23" fill="none" stroke="white" stroke-width="5"/></svg>',
            content_type="image/svg+xml",
        )
    if path == "/api/check":
        return web.json_response({"status": "OK"})
    if path == "/api/cookies":
        return web.json_response(dict(request.cookies))
    if path == "/api/auth":
        valid = request.headers.get("Authorization") == "Bearer demo-application-token"
        return web.json_response(
            {"status": "OK" if valid else "FAIL"}, status=200 if valid else 401
        )
    if path == "/upload":
        data = await request.post()
        upload = data.get("file")
        valid = data.get("message") == "Multipart works" and upload is not None
        if valid:
            valid = upload.file.read() == b"upload contents"
        return web.json_response({"status": "OK" if valid else "FAIL"})
    if path == "/events":
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: OK\n\n")
        return response
    if path == "/submit":
        data = await request.post()
        return web.Response(text=f"Form submitted: {data.get('message')}")
    if path == "/download":
        return web.Response(
            body=b"Proxy download OK\n",
            headers={"Content-Disposition": 'attachment; filename="proxy-check.txt"'},
        )
    if path == "/ws":
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_str(message.data)
        return ws
    raise web.HTTPNotFound()


app = web.Application()
app.router.add_route("*", "/{path:.*}", handler)
web.run_app(app, host=os.environ.get("DEMO_HOST", "127.0.0.1"), port=18766)
