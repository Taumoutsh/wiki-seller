"""Faux serveur de l'API Telegram (getUpdates, sendMessage, sendDocument, deleteMessage)
pour les tests."""

import email.parser
import email.policy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123:abc"
CHAT_ID = "42"


class FakeTelegram:
    def __init__(self):
        self.updates = []  # messages en attente, comme les renvoie getUpdates
        self.sent = []  # textes envoyés par le bot
        self.chats = []  # conversation de chacun
        self.parse_modes = []  # mise en forme demandée pour chacun (None ou "HTML")
        self.reject_html = False  # refuser la mise en forme, comme une balise mal formée
        self.deleted = []  # identifiants des messages supprimés
        self.documents = []  # fichiers envoyés : {"chat_id", "caption", "name", "content"}
        self.menus = []  # menus de commandes publiés (setMyCommands)
        self._next_id = 1

    def say(self, text, chat_id=CHAT_ID):
        """Message reçu par le bot (comme si on lui écrivait dans Telegram)."""
        message_id = self._next_id
        self.updates.append({"update_id": 1000 + message_id,
                             "message": {"message_id": message_id, "chat": {"id": int(chat_id)}, "text": text}})
        self._next_id += 1
        return message_id

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                token, _, method = self.path.removeprefix("/bot").partition("/")
                if method == "sendDocument":
                    form = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                        f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw)
                    document = {}
                    for part in form.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if part.get_filename():
                            document.update(name=part.get_filename(), content=part.get_payload(decode=True))
                        else:
                            document[name] = part.get_payload(decode=True).decode()
                    fake.documents.append(document)
                    return self._send({"ok": True, "result": {"message_id": 0}})
                body = json.loads(raw or b"{}")
                if token != TOKEN:
                    return self._send({"ok": False, "description": "Unauthorized"}, 401)
                if method == "getUpdates":
                    offset = body.get("offset", 0)
                    if offset == -1:
                        return self._send({"ok": True, "result": fake.updates[-1:]})
                    pending = [u for u in fake.updates if u["update_id"] >= offset]
                    if not pending:
                        time.sleep(0.05)
                    return self._send({"ok": True, "result": pending[:1]})
                if method == "sendMessage":
                    if fake.reject_html and body.get("parse_mode"):
                        return self._send({"ok": False, "description": "Bad Request: can't parse entities"}, 400)
                    fake.sent.append(body["text"])
                    fake.chats.append(str(body["chat_id"]))
                    fake.parse_modes.append(body.get("parse_mode"))
                    return self._send({"ok": True, "result": {"message_id": 0}})
                if method == "setMyCommands":
                    fake.menus.append(body)
                    return self._send({"ok": True, "result": True})
                if method == "deleteMessage":
                    fake.deleted.append(body["message_id"])
                    return self._send({"ok": True, "result": True})
                return self._send({"ok": False, "description": "Not Found"}, 404)

            def _send(self, data, status=200):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
