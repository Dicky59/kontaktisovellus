"""Käynnistää Soittolista-sovelluksen ja avaa selaimen."""
import socket
import threading
import webbrowser

import uvicorn

from app import app

HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}"


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    if port_in_use():
        # Sovellus pyörii jo: avaa vain selain
        webbrowser.open(URL)
        raise SystemExit
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    print(f"Soittolista käynnissä osoitteessa {URL}  (sulje tämä ikkuna lopettaaksesi)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
