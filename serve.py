#!/usr/bin/env python3
# Mini servidor estático del dashboard de AeroIntel (local). Sirve output/ en :8200.
# Usa ruta absoluta (no os.getcwd) para evitar problemas de cwd en el sandbox.
import http.server, socketserver, functools

OUTPUT = "/Users/usuario/Desktop/Cloude Code/aerointel/output"
PORT = 8200

Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=OUTPUT)
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"AeroIntel dashboard en http://localhost:{PORT}/dashboard.html")
    httpd.serve_forever()
