# NGINX-BrowserMCP

An MCP server that drives a real headless Chromium over CDP, so an assistant can
**look at a page** instead of inferring it from CSS text. It renders, hovers,
clicks, switches device profiles, and returns actual PNG pixels as MCP image
content.

Works on any URL. For the sites this pool serves, `pool_sites` enumerates hosts
straight from nginx-pool's own `domain.map` / `user.map`.

## Why

Verifying a layout by reading `getComputedStyle` tells you a rule *applied*. It
cannot tell you the result looks wrong. Real defects found by rendering that
were invisible to property checks:

- a hovered button rendering white-on-white (the tertiary `:hover` outranked the
  filled variant's, which never re-declared `background`)
- a 2px pink bar under every field (an empty error container still painting its
  background and border)
- a menu divider inflating into a 2.4rem slab (`.admin-menu li`'s `min-height`
  beating a bare `.divider` class)
- action buttons overlapping the page title at 390px (a `nowrap` flex header
  squeezing the title box to 26px)

## Install

```sh
# from the repo
./bin/nginx-pool-mcp        # stdio server; nothing to configure
```

Register it with an MCP client:

```json
{
  "mcpServers": {
    "NGINX-BrowserMCP": { "command": "/var/www/nginx/bin/nginx-pool-mcp" }
  }
}
```

Requirements: `python3`, `chromium` (or Chrome) on `PATH`, and the `requests` +
`websocket-client` Python packages. An already-running Chromium with
`--remote-debugging-port=9222` is reused; otherwise a headless one is launched
against a scratch profile. Override the endpoint with `NGINX_POOL_CDP`.

## Tools

| Tool | Purpose |
|---|---|
| `browser_open` | Load a URL. Optional `device`, `basic_auth` (`user:pass`), `cookies_file` (Netscape jar) |
| `browser_goto` | Navigate the open page |
| `browser_device` | `phone`, `phone-sm`, `tablet`, `laptop`, `desktop`, or explicit `width`/`height`/`dpr`/`mobile` |
| `browser_screenshot` | Pixels: one `selector`, the `full` page, or the viewport |
| `browser_states` | One element at **rest / hover / active** + a computed-style diff across the three |
| `browser_hover` | Real mouse hover, returns computed style |
| `browser_click` | Real click; reports resulting URL and console errors |
| `browser_type` / `browser_key` | Type text / press keys as real input events |
| `browser_scroll` | Scroll to a position or bring an element into view |
| `browser_eval` | Evaluate JavaScript |
| `browser_measure` | Box-measure several selectors; flags **overlapping pairs** and horizontal overflow |
| `browser_console` | Console messages, exceptions, failed and 4xx/5xx requests |
| `pool_sites` | Hosts this pool serves, from the nginx maps |
| `browser_close` | Release the tab |

## Notes

- States are driven with real `Input.dispatchMouseEvent`, not by forcing
  pseudo-classes — so JS-driven hover/active behaviour is captured too.
- `browser_states` holds the mouse down for the active capture, then releases,
  so it never accidentally submits the form it is photographing.
- Screenshot scale backs off automatically when a clip would exceed Chromium's
  raster budget or the transport's payload limit.
- Tool errors come back as `isError` results with the exception text; the server
  keeps running.

## Caveats

- A capture is a static frame: transitions need deliberate state changes.
- `browser_click` performs real actions on real sites. Point it at staging, not
  production, unless you mean it.
