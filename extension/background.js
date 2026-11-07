// HTB Connect: the connect page (on our gateway) asks us to capture one site's cookies.
// We open our own approval page, where the user grants access to that site and confirms.
// Only pages listed in manifest "externally_connectable" (our gateway) can message us.

chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  const gateway = sender.origin; // trust the browser's view of who is asking, never the message body
  if (!message || typeof message !== "object") return;

  if (message.type === "ping") {
    sendResponse({ ok: true, version: chrome.runtime.getManifest().version });
    return;
  }

  if (message.type === "connect") {
    const { token, domain, baseUrl, origins } = message;
    const valid =
      typeof token === "string" && /^[A-Za-z0-9_-]{16,64}$/.test(token) &&
      typeof domain === "string" && typeof baseUrl === "string" && /^https?:\/\//.test(baseUrl) &&
      Array.isArray(origins) && origins.length > 0 && origins.every((o) => typeof o === "string");
    if (!valid) {
      sendResponse({ ok: false, error: "bad request" });
      return;
    }
    const id = crypto.randomUUID();
    chrome.storage.session.set({ [id]: { gateway, token, domain, baseUrl, origins } }).then(() => {
      chrome.tabs.create({ url: chrome.runtime.getURL(`approve.html#${id}`) });
      sendResponse({ ok: true });
    });
    return true; // async sendResponse
  }
});
