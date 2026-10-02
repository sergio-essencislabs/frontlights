#!/usr/bin/env python3
"""App de exemplo do teste de navegador: uma página de login com conta fictícia e um painel.

Só biblioteca padrão. A porta vem do primeiro argumento ou da variável PORT (o `serve` entrega as duas com
`port: "auto"`). Escuta somente em 127.0.0.1.

Rotas: GET /health (200 "ok"), GET / (formulário de login), POST /entrar (login e senha do formulário; certo:
cookie de sessão e 303 para /painel; errado: 401), GET /painel (mostra o login da sessão; sem sessão: 303 para /).
A conta é fictícia e fixa, igual à de `config.json`; nunca use uma conta real aqui.
"""

import html
import http.server
import os
import secrets
import sys
import urllib.parse

ACCOUNTS = {'usuario1@exemplo.test': 'senha-ficticia'}
COOKIE = 'sessao'
SESSIONS = {}

LOGIN_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Entrar</title></head>
<body><h1>Entrar</h1>{aviso}
<form method="post" action="/entrar">
<label>Login <input name="login" type="text" autocomplete="username"></label>
<label>Senha <input name="senha" type="password" autocomplete="current-password"></label>
<button type="submit">Entrar</button>
</form></body></html>"""

PANEL_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Painel</title></head>
<body><h1>Painel</h1><p>Sessão de <span data-testid="usuario">{login}</span></p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self, code, body='', headers=()):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def session_login(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE and value in SESSIONS:
                return SESSIONS[value]
        return None

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/health':
            self.answer(200, 'ok')
        elif path == '/':
            self.answer(200, LOGIN_PAGE.format(aviso=''))
        elif path == '/painel':
            login = self.session_login()
            if login is None:
                self.answer(303, headers=[('Location', '/')])
            else:
                self.answer(200, PANEL_PAGE.format(login=html.escape(login)))
        else:
            self.answer(404, 'não encontrado')

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != '/entrar':
            self.answer(404, 'não encontrado')
            return
        size = int(self.headers.get('Content-Length') or 0)
        fields = urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'))
        login = (fields.get('login') or [''])[0]
        password = (fields.get('senha') or [''])[0]
        expected = ACCOUNTS.get(login)
        if expected is None or not secrets.compare_digest(expected, password):
            self.answer(401, LOGIN_PAGE.format(aviso='<p role="alert">Login ou senha inválidos.</p>'))
            return
        token = secrets.token_urlsafe(16)
        SESSIONS[token] = login
        self.answer(303, headers=[('Location', '/painel'), ('Set-Cookie', f'{COOKIE}={token}; HttpOnly; Path=/')])

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ['PORT'])
    http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    main()
