"""Offline wire-shape check: native Codex, fake auth, loopback mock only.
No real model executes; synthetic usage is not a cache measurement.
"""
import argparse, http.server, json, os, selectors, subprocess, tempfile, threading, time
from pathlib import Path
NATIVE = '/opt/homebrew/bin/codex'
ROOT = Path.home() / '.local/share/jev-codex-router'
CATALOG = ROOT / 'native-models.json'

class Handler(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.loads(raw)
        updates = [x.get('reasoning', {}).get('effort') for x in body.get('input', []) if isinstance(x, dict) and x.get('type') == 'configuration_update']
        self.requests.append({'model': body.get('model'), 'request_effort': body.get('reasoning', {}).get('effort'), 'updates': updates})
        response = {'id': 'resp_' + str(len(self.requests)), 'object': 'response', 'status': 'completed', 'output': [{'id': 'msg_' + str(len(self.requests)), 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}], 'usage': {'input_tokens': 100, 'output_tokens': 1, 'total_tokens': 101, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
        events = [('response.created', {'type': 'response.created', 'response': {'id': response['id'], 'status': 'in_progress', 'output': []}}), ('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0, 'item': response['output'][0]}), ('response.completed', {'type': 'response.completed', 'response': response})]
        data = ''.join(('event: ' + kind + '\ndata: ' + json.dumps(value) + '\n\n' for kind, value in events)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

def run(enabled):
    with tempfile.TemporaryDirectory(prefix='effortlane-wire-') as directory:
        home = Path(directory)
        cat = json.loads(CATALOG.read_text())
        model = next((x for x in cat['models'] if x['slug'] == 'gpt-6.1-sol'))
        if model.get('supports_reasoning_effort_updates') is not True:
            raise ValueError('catalog does not advertise reasoning effort updates for Sol6.1')
        model['supports_websockets'] = False
        (home / 'models.json').write_text(json.dumps({'models': [model]}))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        Handler.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (home / 'config.toml').write_text('model = "gpt-6.1-sol"\nmodel_catalog_json = ' + json.dumps(str(home / 'models.json')) + '\nopenai_base_url = "http://127.0.0.1:' + str(server.server_port) + '/v1"\n[features]\nreasoning_effort_override = ' + str(enabled).lower() + '\n')
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home), 'CODEX_HOME': str(home), 'OPENAI_API_KEY': 'fake-loopback-only-key', 'LANG': 'en_US.UTF-8'}
        p = subprocess.Popen([NATIVE, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=home, bufsize=0)
        selector = selectors.DefaultSelector()
        selector.register(p.stdout, selectors.EVENT_READ)

        def send(method, params=None, rid=None):
            item = {'method': method}
            if rid is not None:
                item['id'] = rid
            if params is not None:
                item['params'] = params
            p.stdin.write((json.dumps(item) + '\n').encode())
            p.stdin.flush()

        def receive(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not selector.select(0.2):
                    continue
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError('native exited')
                msg = json.loads(line)
                if predicate(msg):
                    return msg
            raise TimeoutError('native RPC timeout')
        try:
            send('initialize', {'clientInfo': {'name': 'effortlane_wire_validation', 'version': '1'}}, 1)
            receive(lambda m: m.get('id') == 1)
            send('initialized')
            send('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(home), 'approvalPolicy': 'never', 'sandbox': 'read-only', 'experimentalRawEvents': False}, 2)
            result = receive(lambda m: m.get('id') == 2)
            if 'error' in result:
                raise RuntimeError('thread/start rejected')
            tid = result['result']['thread']['id']
            for rid, effort in [(3, 'low'), (4, 'high')]:
                send('turn/start', {'threadId': tid, 'model': 'gpt-6.1-sol', 'effort': effort, 'input': [{'type': 'text', 'text': 'Reply OK; do not use tools.'}]}, rid)
                reply = receive(lambda m: m.get('id') == rid)
                if 'error' in reply:
                    raise RuntimeError('turn/start rejected')
                receive(lambda m: m.get('method') == 'turn/completed')
            before_restart = len(Handler.requests)
            p.terminate()
            p.wait(timeout=3)
            selector.close()
            text = (home / 'config.toml').read_text().replace('[features]', 'model_reasoning_effort = "high"\n[features]')
            (home / 'config.toml').write_text(text)
            p = subprocess.Popen([NATIVE, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=home, bufsize=0)
            selector = selectors.DefaultSelector()
            selector.register(p.stdout, selectors.EVENT_READ)
            send('initialize', {'clientInfo': {'name': 'effortlane_wire_validation', 'version': '1'}}, 10)
            receive(lambda m: m.get('id') == 10)
            send('initialized')
            send('thread/resume', {'threadId': tid, 'model': 'gpt-6.1-sol', 'reasoningEffort': 'high'}, 11)
            reply = receive(lambda m: m.get('id') == 11)
            if 'error' in reply:
                raise RuntimeError('resume rejected')
            send('turn/start', {'threadId': tid, 'model': 'gpt-6.1-sol', 'effort': 'medium', 'input': [{'type': 'text', 'text': 'Reply OK; do not use tools.'}]}, 12)
            receive(lambda m: m.get('id') == 12)
            receive(lambda m: m.get('method') == 'turn/completed')
            return {'feature_enabled': enabled, 'requests_before_restart': Handler.requests[:before_restart], 'requests_after_resume': Handler.requests[before_restart:], 'real_model_calls': 0, 'cache_measurement': False}
        finally:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            selector.close()
            server.shutdown()
            server.server_close()
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Effortlane: offline native effort wire probe, not a cache benchmark')
    parser.add_argument('--native', default=NATIVE)
    parser.add_argument('--catalog', type=Path, default=CATALOG)
    options = parser.parse_args()
    NATIVE = options.native
    CATALOG = options.catalog
    for enabled in (False, True):
        try:
            print(json.dumps(run(enabled)))
        except Exception as e:
            print(json.dumps({'feature_enabled': enabled, 'failure': type(e).__name__, 'message': str(e)}))
            raise SystemExit(1)
