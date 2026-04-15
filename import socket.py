import socket
import signal
import sys
import threading
import asyncio
import urllib.request
import urllib.error
import json
import time
from urllib.parse import parse_qs
import websockets
import logging

# ========== KONFIGURASI ==========
HOST = 'localhost'
HTTP_PORT = 8080
WS_PORT = 8081
server_running = True

# ========== DATA STORAGE (IN-MEMORY) ==========
items = []
next_id = 1

def create_item(title, url, scraped_title=None):
    global next_id
    item = {
        "id": next_id,
        "title": title,
        "url": url,
        "scraped_title": scraped_title if scraped_title else "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    items.append(item)
    next_id += 1
    asyncio.create_task(broadcast_crud_update("create", item))
    return item

def get_item(item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    return None

def update_item(item_id, title=None, url=None, scraped_title=None):
    item = get_item(item_id)
    if not item:
        return None
    if title is not None:
        item["title"] = title
    if url is not None:
        item["url"] = url
    if scraped_title is not None:
        item["scraped_title"] = scraped_title
    asyncio.create_task(broadcast_crud_update("update", item))
    return item

def delete_item(item_id):
    global items
    item = get_item(item_id)
    if not item:
        return False
    items = [i for i in items if i["id"] != item_id]
    asyncio.create_task(broadcast_crud_update("delete", item))
    return True

# ========== WEB SCRAPING ==========
def scrape_title(url):
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (CRUDScraper)'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            start = html.find('<title>')
            end = html.find('</title>')
            if start != -1 and end != -1:
                return html[start+7:end].strip()
            return "No title tag found"
    except Exception as e:
        return f"Error: {str(e)}"

# ========== WEBSOCKET BROADCAST & HANDLER ==========
connected_websockets = set()

async def broadcast_crud_update(action, data):
    if connected_websockets:
        message = json.dumps({"action": action, "data": data})
        await asyncio.gather(*[ws.send(message) for ws in connected_websockets])

async def ws_handler(websocket):
    print(f"WebSocket client connected from {websocket.remote_address}")
    connected_websockets.add(websocket)
    try:
        async for message in websocket:
            print(f"WebSocket message: {message}")
            try:
                data = json.loads(message)
                url = data.get('url')
                if url:
                    title = scrape_title(url)
                    response = {"url": url, "title": title, "status": "success"}
                    await websocket.send(json.dumps(response))
                else:
                    await websocket.send(json.dumps({"error": "No URL provided"}))
            except:
                if message.startswith(('http://', 'https://')):
                    title = scrape_title(message)
                    await websocket.send(json.dumps({"url": message, "title": title}))
                else:
                    await websocket.send(json.dumps({"error": "Unknown format"}))
    except websockets.exceptions.ConnectionClosed:
        print(f"WebSocket client {websocket.remote_address} disconnected")
    finally:
        connected_websockets.remove(websocket)

async def start_websocket_server():
    async with websockets.serve(ws_handler, HOST, WS_PORT):
        print(f"WebSocket server running at ws://{HOST}:{WS_PORT}")
        await asyncio.Future()

# ========== HTTP REQUEST PARSER ==========
def parse_http_request(request_data):
    header_end = request_data.find('\r\n\r\n')
    if header_end == -1:
        return None, None
    headers_raw = request_data[:header_end]
    body = request_data[header_end+4:]
    headers = {}
    for line in headers_raw.split('\r\n'):
        if ': ' in line:
            key, val = line.split(': ', 1)
            headers[key.lower()] = val
    return headers, body

def get_content_type(headers):
    return headers.get('content-type', '')

def parse_body(body, content_type):
    if 'application/json' in content_type:
        try:
            return json.loads(body)
        except:
            return {}
    elif 'application/x-www-form-urlencoded' in content_type:
        return parse_qs(body.decode('utf-8', errors='ignore'))
    else:
        return {}

# ========== HTTP HANDLER ==========
def handle_http_request(request_data):
    request_lines = request_data.split('\r\n')
    if not request_lines:
        return b''
    request_line = request_lines[0]
    parts = request_line.split(' ')
    if len(parts) < 2:
        method = "GET"
        path = "/"
    else:
        method = parts[0]
        path = parts[1]

    headers, body = parse_http_request(request_data)
    content_type = get_content_type(headers) if headers else ''
    body_data = parse_body(body, content_type) if body else {}

    print(f"HTTP {method} {path}")

    # ---------- CRUD API ----------
    if method == "GET" and path == "/items":
        response_body = json.dumps(items, indent=2)
        status = "200 OK"
        ctype = "application/json"

    elif method == "GET" and path.startswith("/items/"):
        try:
            item_id = int(path.split('/')[2])
            item = get_item(item_id)
            if item:
                response_body = json.dumps(item)
                status = "200 OK"
            else:
                response_body = json.dumps({"error": "Item not found"})
                status = "404 Not Found"
            ctype = "application/json"
        except:
            response_body = json.dumps({"error": "Invalid ID"})
            status = "400 Bad Request"
            ctype = "application/json"

    elif method == "POST" and path == "/items":
        title = body_data.get('title', '')
        url = body_data.get('url', '')
        if not title or not url:
            response_body = json.dumps({"error": "title and url required"})
            status = "400 Bad Request"
        else:
            scraped = scrape_title(url)
            item = create_item(title, url, scraped)
            response_body = json.dumps(item)
            status = "201 Created"
        ctype = "application/json"

    elif method == "PUT" and path.startswith("/items/"):
        try:
            item_id = int(path.split('/')[2])
            title = body_data.get('title')
            url = body_data.get('url')
            if title is None and url is None:
                response_body = json.dumps({"error": "No field to update"})
                status = "400 Bad Request"
            else:
                scraped_title = scrape_title(url) if url else None
                updated = update_item(item_id, title, url, scraped_title)
                if updated:
                    response_body = json.dumps(updated)
                    status = "200 OK"
                else:
                    response_body = json.dumps({"error": "Item not found"})
                    status = "404 Not Found"
            ctype = "application/json"
        except:
            response_body = json.dumps({"error": "Invalid ID"})
            status = "400 Bad Request"
            ctype = "application/json"

    elif method == "DELETE" and path.startswith("/items/"):
        try:
            item_id = int(path.split('/')[2])
            if delete_item(item_id):
                response_body = json.dumps({"message": "Deleted"})
                status = "200 OK"
            else:
                response_body = json.dumps({"error": "Item not found"})
                status = "404 Not Found"
            ctype = "application/json"
        except:
            response_body = json.dumps({"error": "Invalid ID"})
            status = "400 Bad Request"
            ctype = "application/json"

    # ---------- WEB PAGES (NO EMOJI, PLAIN TEXT) ----------
    elif path == "/crud":
        html = """<!DOCTYPE html>
<html>
<head><title>CRUD Manager - Web Scraper</title>
<style>
    body { font-family: Arial; margin: 20px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #3498db; color: white; }
    form { margin: 20px 0; background: #f2f2f2; padding: 15px; border-radius: 5px; }
    input[type=text] { width: 300px; padding: 5px; margin: 5px; }
    button { padding: 6px 12px; background: #2ecc71; border: none; cursor: pointer; }
    .delete { background: #e74c3c; color: white; }
</style>
<script>
    async function loadItems() {
        const res = await fetch('/items');
        const items = await res.json();
        const tbody = document.querySelector('#itemsTable tbody');
        tbody.innerHTML = '';
        items.forEach(item => {
            const row = tbody.insertRow();
            row.insertCell(0).innerText = item.id;
            row.insertCell(1).innerText = item.title;
            row.insertCell(2).innerText = item.url;
            row.insertCell(3).innerText = item.scraped_title;
            row.insertCell(4).innerHTML = `
                <button onclick="editItem(${item.id})">Edit</button>
                <button class="delete" onclick="deleteItem(${item.id})">Delete</button>
            `;
        });
    }
    async function addItem() {
        const title = document.getElementById('title').value;
        const url = document.getElementById('url').value;
        const res = await fetch('/items', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title, url})
        });
        if (res.ok) {
            loadItems();
            document.getElementById('title').value = '';
            document.getElementById('url').value = '';
        } else alert('Add failed');
    }
    async function editItem(id) {
        const newTitle = prompt('Enter new title:');
        if (newTitle !== null) {
            await fetch(`/items/${id}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: newTitle})
            });
            loadItems();
        }
    }
    async function deleteItem(id) {
        if (confirm('Delete?')) {
            await fetch(`/items/${id}`, {method: 'DELETE'});
            loadItems();
        }
    }
    let ws;
    function connectWS() {
        ws = new WebSocket('ws://localhost:8081');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.action === 'create' || data.action === 'update' || data.action === 'delete') {
                loadItems();
                document.getElementById('wsMsg').innerText = 'Data changed via WebSocket';
                setTimeout(() => document.getElementById('wsMsg').innerText = '', 2000);
            }
        };
        ws.onopen = () => console.log('WebSocket connected');
    }
    window.onload = () => { loadItems(); connectWS(); };
</script>
</head>
<body>
    <h1>CRUD Manager - Data Scraping</h1>
    <div id="wsMsg" style="color:green"></div>
    <form onsubmit="event.preventDefault(); addItem()">
        <input type="text" id="title" placeholder="Item Title" required>
        <input type="text" id="url" placeholder="URL (https://...)" required>
        <button type="submit">Add Item (auto scrape)</button>
    </form>
    <table id="itemsTable">
        <thead><tr><th>ID</th><th>Title</th><th>URL</th><th>Scraped Title</th><th>Action</th></tr></thead>
        <tbody></tbody>
    </table>
    <p><a href="/">Back to Home</a> | <a href="/websocket-demo">WebSocket Live Scraper</a></p>
</body>
</html>"""
        response_body = html
        status = "200 OK"
        ctype = "text/html"

    elif path == "/":
        html = """<!DOCTYPE html>
<html>
<head><title>Home - Socket Server + CRUD</title>
<style>body { font-family: Arial; margin: 50px; text-align: center; }</style>
</head>
<body>
    <h1>Web Server with CRUD & WebSocket</h1>
    <p><a href="/crud">CRUD Management (Data Scraping)</a></p>
    <p><a href="/websocket-demo">WebSocket Live Scraper</a></p>
    <p><a href="/about">About</a></p>
    <hr><small>Press Ctrl+C to stop server</small>
</body>
</html>"""
        response_body = html
        status = "200 OK"
        ctype = "text/html"

    elif path == "/about":
        html = "<h1>About</h1><p>Server with CRUD + WebSocket + Web Scraping</p><a href='/'>Home</a>"
        response_body = html
        status = "200 OK"
        ctype = "text/html"

    elif path == "/websocket-demo":
        html = """<!DOCTYPE html>
<html><head><title>WebSocket Demo</title><script>
let ws;
function connect() {
    ws = new WebSocket("ws://localhost:8081");
    ws.onmessage = (e) => {
        const pre = document.createElement("pre");
        pre.textContent = e.data;
        document.getElementById("log").appendChild(pre);
    };
    ws.onopen = () => document.getElementById("status").innerText = "Connected";
}
function sendUrl() {
    const url = document.getElementById("url").value;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(url);
}
window.onload = connect;
</script></head>
<body><h2>WebSocket Scraper</h2><input id="url" size="50"><button onclick="sendUrl()">Scrape</button><div id="log"></div><a href="/">Back</a></body></html>"""
        response_body = html
        status = "200 OK"
        ctype = "text/html"

    else:
        response_body = f"<h1>404 Not Found</h1><p>Path '{path}' not found</p><a href='/'>Home</a>"
        status = "404 Not Found"
        ctype = "text/html"

    response = f"""\
HTTP/1.1 {status}
Content-Type: {ctype}
Content-Length: {len(response_body)}
Connection: close

{response_body}
"""
    return response.encode('utf-8')

# ========== HTTP SERVER THREAD ==========
def run_http_server():
    global server_running
    http_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    http_socket.bind((HOST, HTTP_PORT))
    http_socket.listen(5)
    http_socket.settimeout(1.0)
    print(f"HTTP server running at http://{HOST}:{HTTP_PORT}")
    while server_running:
        try:
            client_socket, addr = http_socket.accept()
            print(f"HTTP client: {addr}")
            request = client_socket.recv(8192).decode('utf-8', errors='ignore')
            if request:
                response = handle_http_request(request)
                client_socket.sendall(response)
            client_socket.close()
        except socket.timeout:
            continue
        except Exception as e:
            print(f"HTTP error: {e}")
    http_socket.close()

# ========== MAIN ==========
def signal_handler(sig, frame):
    global server_running
    print("\nReceived stop signal...")
    server_running = False
    sys.exit(0)

if __name__ == "__main__":
    logging.getLogger("websockets.server").setLevel(logging.ERROR)
    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 60)
    print("SERVER WITH HTTP + WEBSOCKET + WEB SCRAPING + CRUD")
    print("=" * 60)
    print(f"HTTP      : http://{HOST}:{HTTP_PORT}")
    print(f"WebSocket : ws://{HOST}:{WS_PORT}")
    print("CRUD available at /crud")
    print("=" * 60)

    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    try:
        asyncio.run(start_websocket_server())
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    finally:
        server_running = False
        print("Server stopped.")