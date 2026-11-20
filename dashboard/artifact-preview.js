/* Render reports as inert documents. The dashboard handles local file links. */
(() => {
  "use strict";
  const elements = new Set((
    "a article aside b blockquote br caption code dd details div dl dt em figcaption figure footer " +
    "h1 h2 h3 h4 h5 h6 header hr i img kbd li main mark nav ol p pre s section small span " +
    "strong style sub summary sup table tbody td th thead time tr u ul"
  ).split(" "));
  const attributes = new Set([
    "alt", "class", "colspan", "datetime", "height", "id", "open", "role", "rowspan", "style", "title", "width",
  ]);
  const policy = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'";
  const tokenKey = "ringer-browser-session";
  let browserToken = "";
  if (window.location.protocol === "http:") {
    const launch = /^#ringer-session=([a-f0-9]{64})$/.exec(window.location.hash);
    if (launch) {
      browserToken = launch[1];
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    try {
      if (browserToken) localStorage.setItem(tokenKey, browserToken);
      else browserToken = localStorage.getItem(tokenKey) || "";
    } catch (_) {
      // The current page can still use the launcher in profiles with storage disabled.
    }
  }

  function browserHeaders() {
    return browserToken ? { "X-Ringer-Token": browserToken } : {};
  }

  function browserFetch(path, options = {}) {
    return fetch(path, {
      ...options, cache: "no-store", redirect: "error", mode: "same-origin",
      credentials: "omit", headers: browserHeaders(),
    });
  }

  function viewerHref(path) {
    return `/view#${encodeURIComponent(path)}`;
  }

  function openBrowserLink(url) {
    window.open(viewerHref(url.pathname + url.search + url.hash), "_blank", "noopener,noreferrer");
  }

  function localArtifactLink(value, source) {
    if (!value || value.startsWith("#")) return null;
    try {
      const url = new URL(value, source);
      if (url.protocol === "file:" && source.protocol === "file:" && !url.host) {
        return url;
      }
      if (source.protocol === "http:" && url.origin === source.origin && url.pathname.startsWith("/artifacts/")) {
        return url;
      }
    } catch (_) {
      // A malformed or external link has no action in a local preview.
    }
    return null;
  }

  function prepare(raw, source) {
    // Template parsing is inert, including resource loads. Keep only report
    // markup and replace link destinations before the document is rendered.
    const template = document.createElement("template");
    template.innerHTML = raw;
    const links = [];
    for (const node of template.content.querySelectorAll("*")) {
      const tag = node.localName;
      if (node.namespaceURI !== "http://www.w3.org/1999/xhtml" || !elements.has(tag)) {
        node.remove();
        continue;
      }
      const href = tag === "a" ? node.getAttribute("href") : null;
      const src = tag === "img" ? node.getAttribute("src") : null;
      for (const attr of node.getAttributeNames()) {
        if (!attributes.has(attr) && !attr.startsWith("aria-")) node.removeAttribute(attr);
      }
      if (href?.startsWith("#")) node.setAttribute("href", href);
      const link = localArtifactLink(href, source);
      if (link) {
        node.setAttribute("href", "#");
        node.dataset.ringerLink = String(links.push(link) - 1);
      }
      if (src?.startsWith("data:image/")) node.setAttribute("src", src);
    }
    return { html: template.innerHTML, links };
  }

  function render(frame, raw, source, open) {
    const { html, links } = prepare(raw, source);
    // Parent access is only for clicks on the converted links. Scripts,
    // forms, popups and top-level navigation stay sandboxed.
    frame.setAttribute("sandbox", "allow-same-origin");
    const scroll = frame.contentWindow?.scrollY || 0;
    frame.onload = () => {
      if (scroll) frame.contentWindow.scrollTo(0, scroll);
      frame.contentDocument?.addEventListener("click", (event) => {
        const anchor = event.target.closest("a[href]");
        if (!anchor) return;
        event.preventDefault();
        if (anchor.dataset.ringerLink !== undefined) {
          const link = links[Number(anchor.dataset.ringerLink)];
          if (link) open(link);
        } else {
          const id = anchor.getAttribute("href").slice(1);
          frame.contentDocument.getElementById(id)?.scrollIntoView();
        }
      });
    };
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"></head><body>${html}</body></html>`;
  }

  function fileURL(path) {
    return new URL("file://" + path.split("/").map(encodeURIComponent).join("/"));
  }

  async function downloadFile(readFile, url) {
    const path = decodeURIComponent(url.pathname);
    const data = await readFile(path);
    const bytes = Array.isArray(data) ? Uint8Array.from(data) : data;
    const link = downloadLink(new Blob([bytes], { type: "application/octet-stream" }), path.split("/").pop());
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
  }

  function downloadLink(blob, name) {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    link.textContent = `Download ${name}`;
    return link;
  }

  async function viewLocalOutput(viewer) {
    try {
      const path = decodeURIComponent(window.location.hash.slice(1));
      const url = new URL(path, window.location.origin);
      if (url.origin !== window.location.origin || !/^\/(artifacts|logs)\//.test(url.pathname)) {
        throw new Error("Invalid output path");
      }
      const result = await browserFetch(url.pathname + url.search);
      if (!result.ok) {
        throw new Error(result.status === 401 ? "Open Ringside from Ringer to view this output" : `HTTP ${result.status}`);
      }
      const type = result.headers.get("Content-Type") || "";
      const name = decodeURIComponent(url.pathname.split("/").pop());
      if (result.headers.has("Content-Disposition")) {
        const link = downloadLink(await result.blob(), name);
        viewer.replaceChildren(link);
        link.click();
      } else if (type.startsWith("text/html")) {
        const iframe = document.createElement("iframe");
        iframe.title = name;
        render(iframe, await result.text(), url, openBrowserLink);
        viewer.replaceChildren(iframe);
      } else if (type.startsWith("image/")) {
        const img = document.createElement("img");
        img.alt = name;
        img.src = URL.createObjectURL(await result.blob());
        viewer.replaceChildren(img);
      } else {
        const text = document.createElement("pre");
        text.textContent = await result.text();
        viewer.replaceChildren(text);
      }
    } catch (error) {
      viewer.textContent = `Could not open this output. ${error.message}`;
    }
  }

  window.ringerArtifactPreview = { browserFetch, browserHeaders, downloadFile, fileURL, openBrowserLink, render, viewerHref };
  const viewer = document.getElementById("artifact-viewer");
  if (viewer) viewLocalOutput(viewer);
})();
