// Injected into every page by src/spider/harmap/capture/record.py (context.add_init_script).
// Reports UI events to the Python side through the exposed binding `__mapperLog`.
// Events: input (debounced to the final value), click, key (Enter only). Navigations are
// logged from Python. Password fields are always reported as "[redacted]".
(function () {
  if (window.__mapperLoggerInstalled) return;
  window.__mapperLoggerInstalled = true;

  const now = () => new Date().toISOString();
  const send = (event) => {
    try {
      if (typeof window.__mapperLog === "function") window.__mapperLog(event);
    } catch (e) {
      /* never break the page */
    }
  };

  const quote = (v) => '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  const unique = (sel) => {
    try {
      return document.querySelectorAll(sel).length === 1;
    } catch (e) {
      return false;
    }
  };

  function selectorFor(el) {
    if (!(el instanceof Element)) return null;
    const tag = el.tagName.toLowerCase();
    if (el.id && unique("#" + CSS.escape(el.id))) return "#" + CSS.escape(el.id);
    for (const attr of ["data-testid", "data-test", "name", "aria-label", "type", "placeholder"]) {
      const v = el.getAttribute(attr);
      if (v) {
        const sel = tag + "[" + attr + "=" + quote(v) + "]";
        if (unique(sel)) return sel;
      }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement) {
      if (node.id && unique("#" + CSS.escape(node.id))) {
        parts.unshift("#" + CSS.escape(node.id));
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  const isSecret = (el) =>
    el.type === "password" || /pass|secret|otp|cvv|cvc/i.test(el.name || "") ||
    (el.autocomplete || "").includes("password");

  const valueOf = (el) => (isSecret(el) ? "[redacted]" : el.isContentEditable ? el.innerText : el.value);

  // Pending (not yet reported) input values, keyed by element.
  const pending = new Map();
  const lastSent = new WeakMap();

  function flush(onlyEl) {
    for (const [el, rec] of Array.from(pending.entries())) {
      if (onlyEl && el !== onlyEl) continue;
      pending.delete(el);
      if (lastSent.get(el) === rec.value) continue;
      lastSent.set(el, rec.value);
      send({ t: rec.t, type: "input", selector: rec.selector, value: rec.value, url: location.href });
    }
  }
  window.__mapperFlush = () => flush();

  document.addEventListener(
    "input",
    (e) => {
      const el = e.target;
      if (!(el instanceof Element)) return;
      if (el.type === "checkbox" || el.type === "radio") return; // the click is logged instead
      pending.set(el, { t: now(), selector: selectorFor(el), value: valueOf(el) });
    },
    true
  );

  document.addEventListener(
    "change",
    (e) => {
      const el = e.target;
      if (el instanceof HTMLSelectElement) {
        pending.set(el, { t: now(), selector: selectorFor(el), value: el.value });
      }
      flush(el);
    },
    true
  );

  document.addEventListener("focusout", (e) => flush(e.target), true);
  document.addEventListener("submit", () => flush(), true);
  window.addEventListener("pagehide", () => flush());

  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      flush();
      send({ t: now(), type: "key", key: "Enter", selector: selectorFor(e.target), url: location.href });
    },
    true
  );

  document.addEventListener(
    "click",
    (e) => {
      if (!(e.target instanceof Element)) return;
      flush();
      const el =
        e.target.closest("a,button,input,select,textarea,label,summary,[role=button],[onclick]") ||
        e.target;
      const text = ((el.innerText || el.value || el.getAttribute("aria-label") || "") + "")
        .trim()
        .replace(/\s+/g, " ")
        .slice(0, 80);
      send({ t: now(), type: "click", selector: selectorFor(el), text: text, url: location.href });
    },
    true
  );
})();
