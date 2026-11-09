"""Scripted-driver scripts for capturing the fixture site's tasks (see harmap/capture/driver.py)."""

SEARCH = [
    {"type": "navigate", "url": "/search"},
    {"type": "input", "selector": "#q", "value": "{{params.query}}"},
    {"type": "click", "selector": "button[type=submit]"},
    {"type": "wait_for", "selector": "#summary:not(:empty)"},
    {"type": "mark"},
]

SEARCH_PAGE2 = [
    *SEARCH,
    {"type": "click", "selector": "#next"},
    {"type": "wait", "ms": 100},
    {"type": "mark"},
]

ADD_NOTE = [
    {"type": "navigate", "url": "/compose"},
    {"type": "input", "selector": "#text", "value": "{{params.text}}"},
    {"type": "click", "selector": "button[type=submit]"},
    {"type": "wait_for", "selector": "#status:not(:empty)"},
    {"type": "mark"},
]

LOGIN = [
    {"type": "navigate", "url": "/login"},
    {"type": "input", "selector": "input[name=username]", "value": "demo"},
    {"type": "input", "selector": "input[name=password]", "value": "{{params.password}}"},
    {"type": "click", "selector": "button[type=submit]"},
]
