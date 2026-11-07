"""The home page (GET /), in the Spine design: what this is, in three lines, and the way in."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from runtime.ui import PRODUCT_NAME, bar_link, esc, icon, page


def routes(signup_open: bool) -> list[BaseRoute]:
    async def home(request: Request) -> Response:
        steps = (("Create an account", "user"), ("Connect Claude", "link"), ("Sign in when asked", "lock"))
        rows = "".join(
            f'<div class="row"><span class="mid">{esc(text)}</span>{icon(name, 36, 1.8)}</div>' for text, name in steps
        )
        bar = bar_link("Create account", "/signup") if signup_open else bar_link("Sign in", "/account/login")
        body = (
            '<h1 class="headline">Your agent, signed in.</h1>'
            f'<div class="rule"></div>{rows}{bar}'
        )
        return page(title=PRODUCT_NAME, spine=PRODUCT_NAME, body=body, nav=['<a href="/account/login">Sign in</a>'])

    return [Route("/", home, methods=["GET"])]
