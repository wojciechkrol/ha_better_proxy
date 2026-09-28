/* Executed in the trusted upstream document, before its own scripts. */
const { prefix, cookiePrefix } = config;
const upstream = new URL(config.upstream);
function mapURL(input) {
  const value = String(input);
  if (/^(data:|blob:|javascript:|mailto:|#)/i.test(value)) return value;
  let url;
  try { url = new URL(value, document.baseURI); } catch { return value; }
  if (url.host !== location.host && url.host !== upstream.host) return value;
  if (url.pathname.startsWith(prefix)) return url.href;
  const base = upstream.pathname.replace(/\/$/, "");
  let path = url.pathname;
  if (base && (path === base || path.startsWith(base + "/"))) path = path.slice(base.length);
  const protocol = /^(ws|wss):$/.test(url.protocol) ? (location.protocol === "https:" ? "wss:" : "ws:") : location.protocol;
  return `${protocol}//${location.host}${prefix}${path.replace(/^\//, "")}${url.search}${url.hash}`;
}
function isProxied(value) {
  const url = new URL(value, location.href);
  return url.origin === location.origin && url.pathname.startsWith(prefix);
}
function applicationHeaders(source) {
  const headers = new Headers(source);
  const auth = headers.get("Authorization");
  if (auth && /^(basic|bearer) /i.test(auth)) {
    headers.set("X-Better-Proxy-Authorization", auth);
    headers.delete("Authorization");
  }
  return headers;
}
const originalFetch = window.fetch;
window.fetch = function(input, options) {
  const mapped = mapURL(input instanceof Request ? input.url : input);
  if (input instanceof Request) input = new Request(mapped, input);
  else input = mapped;
  if (isProxied(mapped)) {
    options = { ...options, headers: applicationHeaders(options?.headers ?? (input instanceof Request ? input.headers : undefined)) };
  }
  return originalFetch.call(this, input, options);
};
const proxiedXHR = new WeakSet();
const originalOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(method, url, ...rest) {
  const mapped = mapURL(url);
  if (isProxied(mapped)) proxiedXHR.add(this); else proxiedXHR.delete(this);
  return originalOpen.call(this, method, mapped, ...rest);
};
const originalHeader = XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
  if (proxiedXHR.has(this) && name.toLowerCase() === "authorization" && /^(basic|bearer) /i.test(value)) name = "X-Better-Proxy-Authorization";
  return originalHeader.call(this, name, value);
};
for (const name of ["WebSocket", "EventSource"]) {
  const Original = window[name];
  if (Original) window[name] = new Proxy(Original, {
    construct(Target, args) { args[0] = mapURL(args[0]); return Reflect.construct(Target, args); }
  });
}
for (const method of ["pushState", "replaceState"]) {
  const original = history[method];
  history[method] = function(state, unused, url) {
    return original.call(this, state, unused, url == null ? url : mapURL(url));
  };
}
const originalWindowOpen = window.open;
window.open = function(url, ...rest) { return originalWindowOpen.call(this, url ? mapURL(url) : url, ...rest); };
// Location setters cannot be patched. Catch document navigation before it leaves
// the proxy instead (for example a router's location.href = "/" after login).
window.navigation?.addEventListener("navigate", event => {
  if (!event.cancelable || event.defaultPrevented || event.formData || event.downloadRequest != null) return;
  const destination = event.destination.url;
  const mapped = mapURL(destination);
  if (mapped === destination || !isProxied(mapped)) return;
  event.preventDefault();
  // Start a fresh document load, preserving assign versus replace semantics.
  // Wait until dispatch has finished so the new navigation cannot be cancelled
  // by completion of the original (now prevented) navigation.
  queueMicrotask(() => {
    if (event.navigationType === "replace") location.replace(mapped);
    else location.assign(mapped);
  });
});
// Namespace JavaScript-managed cookies just like HTTP Set-Cookie headers.
const cookieDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "cookie");
if (cookieDescriptor?.get && cookieDescriptor?.set) {
  Object.defineProperty(document, "cookie", {
    configurable: true,
    get() {
      return cookieDescriptor.get.call(document).split(";").map(v => v.trim())
        .filter(v => v.startsWith(cookiePrefix)).map(v => v.slice(cookiePrefix.length)).join("; ");
    },
    set(value) {
      const [pair, ...attributes] = String(value).split(";");
      let path;
      const kept = attributes.filter(attribute => {
        const [key, ...parts] = attribute.trim().split("=");
        if (key.toLowerCase() === "path") path = parts.join("=");
        return !["domain", "path"].includes(key.toLowerCase());
      });
      const mappedPath = path?.startsWith("/")
        ? new URL(mapURL(path), location.href).pathname
        : location.pathname.slice(0, location.pathname.lastIndexOf("/")) || prefix;
      cookieDescriptor.set.call(document, `${cookiePrefix}${pair};${kept.join(";")};Path=${mappedPath}`);
    }
  });
}
// Cover dynamically-created image/script/link/form elements and native setters.
const attributes = ["src", "href", "action", "formaction", "poster", "data", "background"];
function mapSrcset(value) {
  let remaining = String(value), output = "";
  while (remaining) {
    const match = remaining.match(/^([\s,]*)([^\s]+)/);
    if (!match) return output + remaining;
    remaining = remaining.slice(match[0].length);
    const candidate = match[2].replace(/,+$/, "");
    const suffix = match[2].slice(candidate.length);
    output += match[1] + mapURL(candidate) + suffix;
    if (!suffix) {
      const comma = remaining.indexOf(",");
      if (comma < 0) return output + remaining;
      output += remaining.slice(0, comma + 1);
      remaining = remaining.slice(comma + 1);
    }
  }
  return output;
}
function mapAttribute(name, value) {
  name = name.toLowerCase();
  return ["srcset", "imagesrcset"].includes(name) ? mapSrcset(value) : attributes.includes(name) ? mapURL(value) : value;
}
const originalSetAttribute = Element.prototype.setAttribute;
Element.prototype.setAttribute = function(name, value) {
  return originalSetAttribute.call(this, name, mapAttribute(name, value));
};
for (const [Type, names] of [
  [HTMLImageElement, ["src", "srcset"]], [HTMLSourceElement, ["src", "srcset"]],
  [HTMLInputElement, ["src", "formAction"]], [HTMLButtonElement, ["formAction"]], [HTMLScriptElement, ["src"]], [HTMLLinkElement, ["href", "imageSrcset"]],
  [HTMLAnchorElement, ["href"]], [HTMLFormElement, ["action"]],
  [HTMLMediaElement, ["src"]], [HTMLVideoElement, ["poster"]], [HTMLIFrameElement, ["src"]]
]) {
  for (const name of names) {
    const descriptor = Object.getOwnPropertyDescriptor(Type.prototype, name);
    if (descriptor?.set) Object.defineProperty(Type.prototype, name, {
      ...descriptor, set(value) { descriptor.set.call(this, mapAttribute(name, value)); }
    });
  }
}
// Disallow workers that could outlive the HA session or intercept HA itself.
if (navigator.serviceWorker) navigator.serviceWorker.register = () => Promise.reject(new Error("Service workers are unavailable through Better Proxy"));

// Native form submission, including forms inserted with innerHTML and submit().
function mapForm(form, submitter) {
  if (form.hasAttribute("action")) form.setAttribute("action", form.getAttribute("action"));
  if (submitter?.hasAttribute("formaction")) submitter.setAttribute("formaction", submitter.getAttribute("formaction"));
}
document.addEventListener("submit", event => mapForm(event.target, event.submitter), true);
const originalSubmit = HTMLFormElement.prototype.submit;
HTMLFormElement.prototype.submit = function() { mapForm(this); return originalSubmit.call(this); };
// DOM insertion through innerHTML bypasses native URL setters.
const observedAttributes = [...attributes, "srcset", "imagesrcset"];
function mapElement(element) {
  for (const name of observedAttributes) {
    const value = element.getAttribute(name);
    if (value == null) continue;
    const mapped = mapAttribute(name, value);
    if (mapped !== value) originalSetAttribute.call(element, name, mapped);
  }
}
new MutationObserver(records => {
  for (const record of records) {
    if (record.type === "attributes") mapElement(record.target);
    for (const node of record.addedNodes) if (node instanceof Element) {
      mapElement(node);
      node.querySelectorAll("[src],[href],[action],[formaction],[poster],[data],[srcset],[imagesrcset]").forEach(mapElement);
    }
  }
}).observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: observedAttributes});
