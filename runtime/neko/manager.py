"""Neko login browsers: one short-lived Docker container per login, streamed to the user over WebRTC.

Each container runs Neko (https://github.com/m1k1o/neko) with Chromium opened in kiosk mode on the site's login
page. The user drives it through Neko's client, which the gateway proxies under /neko/{token}/. We read the
browser's cookies over Chrome DevTools (browser-level Storage.getCookies, so DevTools stays disabled for pages)
and stop the container once the login is captured or the link expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets

from runtime.identity import BrowserIdentity, accept_language_header
from runtime.proxy import ProxySpec, is_local

log = logging.getLogger("runtime.neko")

ASSETS = Path(__file__).resolve().parent
LABEL = "spider.neko"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

CHROMIUM_CONF = """[program:chromium]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s"
command=/usr/bin/chromium --no-sandbox --test-type --kiosk --window-position=0,0 --window-size={width},{height} --force-device-scale-factor={scale}
  --display=%(ENV_DISPLAY)s --user-data-dir=/home/neko/.config/chromium --no-first-run --disable-gpu
  --disable-software-rasterizer --disable-dev-shm-usage --remote-debugging-port=9222 --accept-lang={accept_lang}{proxy_flags}
  {start_url}
stopsignal=INT
autorestart=true
priority=800
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/chromium.log
redirect_stderr=true

[program:openbox]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s"
command=/usr/bin/openbox --config-file /etc/neko/openbox.xml
autorestart=true
priority=300
user=%(ENV_USER)s

[program:forward]
command=/usr/bin/python3 /etc/neko/forward.py {forwards}
autorestart=true
priority=900
user=%(ENV_USER)s
"""


PROXY_PORT = 3128  # the in-container helper Chromium uses as its proxy (adds the residential proxy's credentials)
PROXY_CONF = """
[program:proxy_auth]
command=/usr/bin/python3 /etc/neko/proxy_auth.py {port} /etc/neko/upstream.json
autorestart=true
priority=700
stdout_logfile=/var/log/neko/proxy_auth.log
redirect_stderr=true
"""


def chromium_conf(
    cfg: NekoConfig, start_url: str, forwards: list[str], proxy: ProxySpec | None, workdir: Path
) -> str:
    """Supervisor config for the login browser. With a residential proxy, Chromium goes through the in-container
    helper (Chromium can't send proxy credentials); the credentials go only into a private file for the helper,
    which runs as root, never onto a command line Chromium (or the user) could see."""
    workdir.mkdir(parents=True, exist_ok=True)
    flags, extra = "", ""
    if proxy is not None and proxy.scheme == "http":
        upstream = {
            # A proxy on this machine (tests) is reached through Docker's host alias.
            "host": "host.docker.internal" if is_local(proxy.host) else proxy.host,
            "port": proxy.port,
            "username": proxy.username,
            "password": proxy.password,
        }
        fd = os.open(workdir / "upstream.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(upstream, f)
        flags = f" --proxy-server=http://127.0.0.1:{PROXY_PORT}"
        if proxy.local:
            flags += " " + _shell_quote("--proxy-bypass-list=<-loopback>")
        extra = PROXY_CONF.format(port=PROXY_PORT)
    elif proxy is not None:
        log.warning("the login browser supports http:// proxies only; signing in goes direct (%s)", proxy.redacted)
    return CHROMIUM_CONF.format(
        width=cfg.width,
        height=cfg.height,
        scale=cfg.scale,
        start_url=_conf_value(_shell_quote(start_url)),
        forwards=_conf_value(" ".join(forwards)),
        accept_lang=_conf_value(_shell_quote(cfg.accept_languages)),
        proxy_flags=_conf_value(flags),
    ) + extra


def _conf_value(text: str) -> str:
    """A value written into supervisord's config: ``%`` starts its own placeholders (``%(ENV_USER)s``), so a
    URL with ``%2F`` in it (LinkedIn's login page) would stop the login browser from starting."""
    return text.replace("%", "%%")


@dataclass
class NekoConfig:
    image: str = "ghcr.io/m1k1o/neko/chromium:latest"
    # The address users' browsers reach for WebRTC media (NAT1TO1). "auto" = this machine's network IP, detected
    # per login. Never advertise loopback by default: Firefox refuses WebRTC to 127.0.0.1.
    public_ip: str = "auto"
    udp_ports: tuple[int, int] = (52000, 52099)  # one per running login, published to the host (UDP and TCP)
    transports: tuple[str, ...] = ("udp", "tcp")  # TCP for browsers/networks that won't do WebRTC over UDP
    width: int = 1920  # streamed screen size, in device pixels
    height: int = 1200
    scale: float = 1.5  # Chrome's device scale factor: sites lay out at width/scale x height/scale (1280x800)
    fps: int = 30
    start_timeout_s: float = 60.0
    ice_servers: str = ""  # optional JSON list of STUN/TURN servers for users behind strict NATs
    accept_languages: str = "en-US,en"  # the login browser's languages; saved with the sign-in (device identity)
    docker: str = "docker"


@dataclass
class NekoSession:
    token: str
    container: str
    http_port: int
    cdp_port: int
    udp_port: int
    password: str
    workdir: Path

    @property
    def prefix(self) -> str:
        return f"/neko/{self.token}"

    @property
    def upstream(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def viewer_path(self) -> str:
        """Relative URL of the embedded viewer (logs in automatically, shows only the browser)."""
        return f"{self.prefix}/?usr=user&pwd={self.password}&embed=1"


class NekoUnavailable(RuntimeError):
    pass


def resolve_public_ip(setting: str) -> str:
    """`auto` -> the IP of the interface that routes to the internet (no packet is sent); else `setting`."""
    if setting != "auto":
        return setting
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: only selects a route
            ip = str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"  # offline: only same-machine Chromium-based browsers will connect
    return ip if not ip.startswith("127.") else "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NekoManager:
    def __init__(self, config: NekoConfig) -> None:
        self.config = config
        self._sessions: dict[str, NekoSession] = {}
        self._udp_in_use: set[int] = set()
        self._lock = asyncio.Lock()

    # ---- docker ------------------------------------------------------------

    async def _docker(self, *args: str, timeout: float = 60) -> str:
        """Run the docker CLI in a worker thread (no asyncio subprocess transports to outlive the loop)."""

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run([self.config.docker, *args], capture_output=True, text=True, timeout=timeout)

        try:
            proc = await asyncio.to_thread(run)
        except FileNotFoundError:
            raise NekoUnavailable("docker is not installed") from None
        except subprocess.TimeoutExpired:
            raise NekoUnavailable(f"docker {args[0]} timed out") from None
        if proc.returncode != 0:
            raise NekoUnavailable(f"docker {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    async def available(self) -> bool:
        try:
            await self._docker("image", "inspect", self.config.image, "--format", "{{.Id}}", timeout=15)
            return True
        except NekoUnavailable:
            return False

    async def cleanup_stale(self) -> int:
        """Remove login containers left behind by a previous run."""
        try:
            ids = (await self._docker("ps", "-aq", "--filter", f"label={LABEL}")).split()
            if ids:
                await self._docker("rm", "-f", *ids)
            return len(ids)
        except NekoUnavailable:
            return 0

    # ---- sessions ----------------------------------------------------------

    def get(self, token: str) -> NekoSession | None:
        return self._sessions.get(token)

    async def launch(
        self, token: str, start_url: str, site_urls: list[str], proxy: ProxySpec | None = None
    ) -> NekoSession:
        async with self._lock:
            existing = self._sessions.get(token)
            if existing is not None:
                return existing
            udp = next(
                (p for p in range(self.config.udp_ports[0], self.config.udp_ports[1] + 1) if p not in self._udp_in_use),
                None,
            )
            if udp is None:
                raise NekoUnavailable("too many logins in progress; try again shortly")
            self._udp_in_use.add(udp)
        try:
            return await self._launch(token, start_url, site_urls, udp, proxy)
        except BaseException:
            self._udp_in_use.discard(udp)
            raise

    async def _launch(
        self, token: str, start_url: str, site_urls: list[str], udp: int, proxy: ProxySpec | None = None
    ) -> NekoSession:
        cfg = self.config
        workdir = Path(tempfile.mkdtemp(prefix="spider-neko-"))
        # In local development the site may be on the Docker host's loopback; forward it into the container.
        forwards = ["0.0.0.0:9223=127.0.0.1:9222"]
        for url in {start_url, *site_urls}:
            parts = urlsplit(url)
            if parts.hostname in LOOPBACK and parts.port:
                spec = f"127.0.0.1:{parts.port}=host.docker.internal:{parts.port}"
                if spec not in forwards:
                    forwards.append(spec)
        (workdir / "chromium.conf").write_text(chromium_conf(cfg, start_url, forwards, proxy, workdir))
        http_port, cdp_port = _free_port(), _free_port()
        password = secrets.token_urlsafe(18)
        env = {
            "NEKO_DESKTOP_SCREEN": f"{cfg.width}x{cfg.height}@{cfg.fps}",
            "NEKO_SERVER_PATH_PREFIX": f"/neko/{token}",
            "NEKO_MEMBER_PROVIDER": "multiuser",
            "NEKO_MEMBER_MULTIUSER_USER_PASSWORD": password,
            "NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD": secrets.token_urlsafe(18),
            "NEKO_SESSION_IMPLICIT_HOSTING": "true",
            "NEKO_WEBRTC_UDPMUX": str(udp),
            "NEKO_WEBRTC_TCPMUX": str(udp),  # same port number, TCP: ICE-TCP fallback
            "NEKO_WEBRTC_NAT1TO1": resolve_public_ip(cfg.public_ip),
        }
        if cfg.ice_servers:
            env["NEKO_WEBRTC_ICESERVERS_FRONTEND"] = cfg.ice_servers
            env["NEKO_WEBRTC_ICESERVERS_BACKEND"] = cfg.ice_servers
        args = [
            "run", "-d", "--rm", "--shm-size=2g", "--memory=1g",
            "--label", f"{LABEL}={token[:12]}",
            "--add-host=host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{http_port}:8080", "-p", f"127.0.0.1:{cdp_port}:9223",
        ]
        for transport in cfg.transports:
            args += ["-p", f"{udp}:{udp}/{transport}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += [
            "-v", f"{workdir}/chromium.conf:/etc/neko/supervisord/chromium.conf:ro",
            "-v", f"{ASSETS}/policies.json:/etc/chromium/policies/managed/policies.json:ro",
            "-v", f"{ASSETS}/forward.py:/etc/neko/forward.py:ro",
        ]
        if (workdir / "upstream.json").exists():
            args += [
                "-v", f"{workdir}/upstream.json:/etc/neko/upstream.json:ro",
                "-v", f"{ASSETS}/proxy_auth.py:/etc/neko/proxy_auth.py:ro",
            ]
        args.append(cfg.image)
        container = await self._docker(*args)
        session = NekoSession(token, container, http_port, cdp_port, udp, password, workdir)
        self._sessions[token] = session
        try:
            await self._wait_ready(session)
        except BaseException:
            await self.stop(token)
            raise
        log.info("login browser %s up for %s", container[:12], token[:6])
        return session

    async def _wait_ready(self, session: NekoSession) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.start_timeout_s
        async with httpx.AsyncClient(timeout=2) as client:
            for url in (f"{session.upstream}{session.prefix}/", f"http://127.0.0.1:{session.cdp_port}/json/version"):
                while True:
                    try:
                        if (await client.get(url)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if asyncio.get_running_loop().time() > deadline:
                        raise NekoUnavailable("the login browser did not start in time")
                    await asyncio.sleep(0.3)

    async def cookies(self, token: str, base_url: str) -> dict[str, str]:
        """Cookies the login browser would send to `base_url` (name -> value).

        One browser-level DevTools command (Storage.getCookies) over Chrome's WebSocket; page-level DevTools
        stays disabled by policy.
        """
        session = self._sessions.get(token)
        if session is None:
            return {}
        async with httpx.AsyncClient(timeout=5) as client:
            info = (await client.get(f"http://127.0.0.1:{session.cdp_port}/json/version")).json()
        # Chrome reports its own in-container address; reach it through the published port instead.
        path = urlsplit(str(info["webSocketDebuggerUrl"])).path
        async with websockets.connect(f"ws://127.0.0.1:{session.cdp_port}{path}", max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply.get("id") == 1:
                    break
        found: list[dict[str, Any]] = reply.get("result", {}).get("cookies", [])
        return {c["name"]: c["value"] for c in found if _applies(c, base_url)}

    async def identity(self, token: str) -> BrowserIdentity | None:
        """The login browser's device identity (version, user-agent, platform, languages), saved with the sign-in."""
        session = self._sessions.get(token)
        if session is None:
            return None
        async with httpx.AsyncClient(timeout=5) as client:
            info = (await client.get(f"http://127.0.0.1:{session.cdp_port}/json/version")).json()
        brand = "Google Chrome" if "google-chrome" in self.config.image else None  # Neko's chromium image: none
        return BrowserIdentity.from_devtools(
            info, brand=brand, accept_language=accept_language_header(self.config.accept_languages)
        )

    async def stop(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session is None:
            return
        self._udp_in_use.discard(session.udp_port)
        try:
            await self._docker("rm", "-f", session.container, timeout=30)
        except NekoUnavailable as e:
            log.warning("could not remove login browser %s: %s", session.container[:12], e)
        shutil.rmtree(session.workdir, ignore_errors=True)  # its config, and the proxy credentials if any
        log.info("login browser %s stopped", session.container[:12])

    async def close(self) -> None:
        for token in list(self._sessions):
            await self.stop(token)


def _applies(cookie: dict[str, Any], url: str) -> bool:
    """Would the browser send this cookie to `url`? (domain and path match; secure only over https)"""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path or "/"
    domain = str(cookie.get("domain", "")).lower()
    if domain.startswith("."):
        domain_ok = host == domain[1:] or host.endswith(domain)
    else:
        domain_ok = host == domain
    cpath = str(cookie.get("path", "/")) or "/"
    path_ok = path == cpath or path.startswith(cpath.rstrip("/") + "/") or cpath == "/"
    secure_ok = not cookie.get("secure") or parts.scheme == "https"
    return domain_ok and path_ok and secure_ok


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
