/** Home Assistant panel: exchange HA auth for an HttpOnly resource session. */
import { panelMessages } from "./translations.js";

class BetterProxyPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --bp-safe-top: max(0px, var(--safe-area-inset-top, env(safe-area-inset-top, 0px)) - var(--bp-outer-padding-top, 0px));
          --bp-safe-left: max(0px, var(--safe-area-content-inset-left, var(--safe-area-inset-left, env(safe-area-inset-left, 0px))) - var(--bp-outer-padding-left, 0px));
          --bp-safe-right: max(0px, var(--safe-area-content-inset-right, var(--safe-area-inset-right, env(safe-area-inset-right, 0px))) - var(--bp-outer-padding-right, 0px));
          display: flex; flex-direction: column; box-sizing: border-box;
          height: calc(var(--bp-viewport-height, 100vh) - var(--bp-offset-top, 0px));
          min-width: 0; min-height: 0; overflow: hidden;
          padding: var(--bp-safe-top) var(--bp-safe-right) 0 var(--bp-safe-left);
          margin-bottom: calc(-1 * var(--bp-outer-padding-bottom, 0px));
          background: var(--primary-background-color); color: var(--primary-text-color);
        }
        header {
          display: flex; align-items: center; flex-shrink: 0; box-sizing: border-box;
          height: var(--header-height, 56px); padding: 0 12px;
          background: var(--app-header-background-color, var(--primary-background-color));
          color: var(--app-header-text-color, var(--primary-text-color));
          border-bottom: var(--app-header-border-bottom, 1px solid var(--divider-color));
        }
        h1 {
          flex: 1; min-width: 0; margin: 0; padding-inline: 24px 8px;
          font-family: var(--mdc-typography-font-family, Roboto, sans-serif);
          font-size: var(--ha-font-size-xl, 20px); font-weight: var(--ha-font-weight-normal, 400);
          line-height: 28px; letter-spacing: 0.0125em;
          overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
        }
        ha-icon-button { flex-shrink: 0; color: inherit; }
        main { position: relative; display: flex; flex-direction: column; flex: 1 1 0; min-height: 0; background: var(--primary-background-color); }
        iframe { display: block; width: 100%; height: 0; flex: 1 1 0; min-height: 0; border: 0; background: var(--primary-background-color); }
        main[data-loading] iframe { visibility: hidden; }
        @media (max-width: 599px) {
          header { padding-inline: 4px; }
        }
        [hidden] { display: none !important; }
        #status { position: absolute; inset: 0; padding: 24px; background: var(--primary-background-color); }
      </style>
      <header><ha-menu-button></ha-menu-button><h1></h1><ha-icon-button id="reload"></ha-icon-button></header>
      <main data-loading>
        <iframe referrerpolicy="same-origin" allow="fullscreen; autoplay; clipboard-write" allowfullscreen></iframe>
        <div id="status" role="status" aria-live="polite"></div>
      </main>`;
    const reload = this.shadowRoot.querySelector("#reload");
    reload.path = "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z";
    reload.addEventListener("click", () => this.open(true));
    this.shadowRoot.querySelector("iframe").addEventListener("load", () => this.finishLoading());
    this.syncLayout = () => this.updateLayout();
  }

  set hass(value) { this._hass = value; this.update(); }
  set panel(value) { this._panel = value; this.update(); }
  set narrow(value) {
    this.shadowRoot.querySelector("ha-menu-button").narrow = value;
  }
  connectedCallback() {
    this.updateLayout();
    window.addEventListener("resize", this.syncLayout);
    this.layoutObserver = new ResizeObserver(this.syncLayout);
    if (this.parentElement) this.layoutObserver.observe(this.parentElement);
    this.update();
  }
  disconnectedCallback() {
    window.removeEventListener("resize", this.syncLayout);
    this.layoutObserver?.disconnect();
    clearInterval(this.timer);
    clearInterval(this.readyTimer);
    this.loading = false;
    this.previousDocument = undefined;
    this.timer = undefined;
    this.started = false;
    this.generation = (this.generation || 0) + 1;
    this.shadowRoot.querySelector("iframe").src = "about:blank";
  }

  finishLoading() {
    if (!this.loading || !this.isConnected) return;
    const document = this.shadowRoot.querySelector("iframe").contentDocument;
    // Ignore the initial blank document and the previous page during reload.
    if (document && (document.URL === "about:blank" || document === this.previousDocument || document.readyState === "loading")) return;
    clearInterval(this.readyTimer);
    this.loading = false;
    this.previousDocument = undefined;
    this.shadowRoot.querySelector("main").removeAttribute("data-loading");
    this.shadowRoot.querySelector("#status").hidden = true;
    this.statusKey = undefined;
  }
  text(key) {
    let language = (this._hass?.language || "en").replace(/_/g, "-").toLowerCase();
    language = ({ no: "nb", zh: "zh-hans", "zh-cn": "zh-hans", "zh-tw": "zh-hant" })[language] || language;
    const messages = panelMessages[language] || panelMessages[language.split("-")[0]] || panelMessages.en;
    return messages[key] || panelMessages.en[key];
  }

  updateLayout() {
    if (!this.isConnected) return;
    // HA 2026.9 wraps custom panels in safe-area padding. Earlier versions do
    // not. Consume the remaining top/side insets even with the header off.
    // Extend the app to the bottom edge; its own bottom controls own that inset.
    const outer = this.parentElement ? getComputedStyle(this.parentElement) : null;
    for (const side of ["top", "bottom", "left", "right"]) {
      this.style.setProperty(`--bp-outer-padding-${side}`, outer?.getPropertyValue(`padding-${side}`) || "0px");
    }
    // Use layout coordinates, not visualViewport or a transformed bounding rect:
    // rubber-band scrolling must move the panel without resizing its iframe.
    // A definite height is needed because HA's custom-panel wrapper has height:auto.
    let top = 0;
    for (let element = this; element; element = element.offsetParent) {
      top += element.offsetTop;
    }
    this.style.setProperty("--bp-viewport-height", `${window.innerHeight}px`);
    this.style.setProperty("--bp-offset-top", `${Math.max(0, top)}px`);
  }

  update() {
    if (!this._hass || !this._panel || !this.isConnected) return;
    this.shadowRoot.querySelector("header").hidden = this._panel.config.show_header === false;
    this.shadowRoot.querySelector("h1").textContent = this._panel.config.title;
    this.shadowRoot.querySelector("iframe").title = this._panel.config.title;
    this.shadowRoot.querySelector("ha-menu-button").hass = this._hass;
    const reload = this.shadowRoot.querySelector("#reload");
    reload.label = this.text("reload");
    reload.title = reload.label;
    if (this.statusKey) this.shadowRoot.querySelector("#status").textContent = this.text(this.statusKey);
    if (!this.started) {
      this.started = true;
      this.open();
      this.timer = setInterval(() => this.open(false, true), 60000);
    }
  }

  async open(reload = false, renew = false) {
    if (this.pending) return;
    this.pending = true;
    const generation = this.generation || 0;
    const frame = this.shadowRoot.querySelector("iframe");
    const status = this.shadowRoot.querySelector("#status");
    if (!renew) {
      clearInterval(this.readyTimer);
      this.loading = false;
      this.shadowRoot.querySelector("main").setAttribute("data-loading", "");
      status.hidden = false;
      this.statusKey = "connecting";
      status.textContent = this.text(this.statusKey);
    }
    try {
      const id = encodeURIComponent(this._panel.config.entry_id);
      const result = await this._hass.callApi("POST", `better_proxy/proxy/${id}/__better_proxy_session`);
      if (!this.isConnected || generation !== (this.generation || 0)) return;
      frame.hidden = false;
      if (reload || !renew || frame.getAttribute("src") === "about:blank") {
        this.shadowRoot.querySelector("main").setAttribute("data-loading", "");
        this.loading = true;
        this.previousDocument = frame.contentDocument;
        frame.src = result.url;
        clearInterval(this.readyTimer);
        // Camera streams and slow images can keep the load event pending.
        // Reveal the parsed document without waiting for every resource.
        this.readyTimer = setInterval(() => {
          const document = frame.contentDocument;
          if (document && (document.readyState === "complete" ||
            frame.contentWindow.performance.getEntriesByType("navigation")[0]?.domContentLoadedEventEnd > 0)) {
            this.finishLoading();
          }
        }, 100);
      }
    } catch (error) {
      if (!this.isConnected || generation !== (this.generation || 0)) return;
      clearInterval(this.readyTimer);
      this.loading = false;
      this.previousDocument = undefined;
      frame.hidden = true;
      frame.src = "about:blank";
      status.hidden = false;
      this.statusKey = error.status_code === 403 ? "denied" : "failed";
      status.textContent = this.text(this.statusKey);
    } finally { this.pending = false; }
  }
}
if (!customElements.get("better-proxy-panel")) customElements.define("better-proxy-panel", BetterProxyPanel);
