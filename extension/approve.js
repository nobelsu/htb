// Approval page: the user grants access to the one site, then we capture and send its cookies.
const $ = (id) => document.getElementById(id);
const id = location.hash.slice(1);

function show(text, kind) {
  $("result").textContent = text;
  $("result").className = kind || "";
}

async function main() {
  const stored = (await chrome.storage.session.get(id))[id];
  if (!stored) {
    show("This request has expired. Start again from the connect page.", "bad");
    $("allow").disabled = true;
    return;
  }
  const { gateway, token, domain, baseUrl, origins } = stored;
  const hosts = origins.map((o) => o.replace(/^\*:\/\//, "").replace(/\/\*$/, "")).join(", ");
  $("domain").textContent = domain;
  $("domain2").textContent = domain;
  $("hosts").textContent = hosts;
  $("gateway").textContent = new URL(gateway).host;

  $("cancel").addEventListener("click", () => window.close());
  $("allow").addEventListener("click", async () => {
    $("allow").disabled = true;
    try {
      // Must run in this click handler: Chrome only shows its permission prompt on a user gesture.
      const granted = await chrome.permissions.request({ origins });
      if (!granted) {
        show(`Access to ${hosts} was not granted, so nothing was sent.`, "bad");
        $("allow").disabled = false;
        return;
      }
      const cookies = await chrome.cookies.getAll({ url: baseUrl });
      show("Checking your login…");
      const resp = await fetch(`${gateway}/connect/${encodeURIComponent(token)}/cookies`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ cookies: cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path })) }),
      });
      const body = await resp.json().catch(() => ({}));
      if (resp.ok) {
        show(`Connected to ${domain}. You can close this tab.`, "ok");
        await chrome.storage.session.remove(id);
        setTimeout(() => window.close(), 1500);
      } else {
        show(body.error || `Connecting failed (HTTP ${resp.status}).`, "bad");
        $("allow").disabled = false;
      }
    } catch (err) {
      show(`Connecting failed: ${err.message}`, "bad");
      $("allow").disabled = false;
    }
  });
}

main();
