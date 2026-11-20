/* Inert DOM fixtures for the shared dashboard preview and browser transport. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../dashboard/artifact-preview.js"), "utf8");
const endpoint = "http://127.0.0.1:8700";
const token = "d".repeat(64);

class Element {
  constructor(name, attributes = {}, text = "") {
    this.localName = name;
    this.namespaceURI = "http://www.w3.org/1999/xhtml";
    this.attributes = attributes;
    this.dataset = {};
    this.text = text;
  }
  getAttributeNames() { return Object.keys(this.attributes); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  remove() { this.removed = true; }
  closest() { return this; }
  toHTML() {
    const attrs = Object.entries(this.attributes).map(([key, value]) => ` ${key}="${value}"`).join("");
    return `<${this.localName}${attrs}>${this.text}</${this.localName}>`;
  }
}

function page(hash, storage, nodes) {
  const requests = [], opened = [], replacements = [], downloads = [];
  class FixtureURL extends URL {
    static createObjectURL(blob) {
      downloads.push({blob});
      return "blob:fixture";
    }
  }
  const window = {
    location: { protocol: "http:", origin: endpoint, pathname: "/", search: "", hash },
    history: { replaceState: (...args) => replacements.push(args) },
    open: (...args) => opened.push(args),
  };
  const document = {
    body: { appendChild() {} },
    getElementById: () => null,
    createElement: (tag) => {
      if (tag === "a") {
        return {
          click() { downloads.at(-1).name = this.download; },
          remove() {},
        };
      }
      assert.equal(tag, "template");
      return {
        content: { querySelectorAll: () => nodes },
        set innerHTML(value) { assert.equal(value, "inert fixture"); },
        get innerHTML() { return nodes.filter((node) => !node.removed).map((node) => node.toHTML()).join(""); },
      };
    },
  };
  vm.runInNewContext(script, {
    window, document, URL: FixtureURL, Blob,
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    fetch: async (...args) => requests.push(args),
    setTimeout() {},
  });
  return { preview: window.ringerArtifactPreview, requests, opened, replacements, downloads };
}

async function check() {
  const storage = new Map();
  const plain = page("", storage, []);
  await plain.preview.browserFetch("/api/runs");
  assert.equal(plain.requests[0][1].headers["X-Ringer-Token"], undefined);

  const elements = [
    new Element("h1", {}, "Saved report"),
    new Element("script", { type: "application/json" }, "{}"),
    new Element("iframe", { title: "Nested frame fixture" }),
    new Element("a", { href: "../deliverables/run/task/fixture.html", target: "_top", ping: "about:blank" }, "Local output"),
    new Element("a", { href: "https://fixture.invalid/" }, "External link"),
    new Element("img", { src: "data:image/png;base64,AA==", alt: "Thumbnail fixture" }),
  ];
  const client = page(`#ringer-session=${token}`, storage, elements);
  assert.equal(client.replacements[0][2], "/");
  await client.preview.browserFetch("/artifacts/library.json");
  assert.equal(client.requests[0][0], "/artifacts/library.json");
  assert.equal(client.requests[0][1].headers["X-Ringer-Token"], token);
  assert.equal(client.requests[0][1].mode, "same-origin");
  assert.equal(client.requests[0][1].redirect, "error");
  const second = page("", storage, []);
  assert.equal(second.preview.browserHeaders()["X-Ringer-Token"], token);

  const frame = new Element("iframe");
  let click;
  frame.contentDocument = { addEventListener: (event, handler) => { if (event === "click") click = handler; } };
  client.preview.render(frame, "inert fixture", new URL(`${endpoint}/artifacts/versions/legacy.html`), client.preview.openBrowserLink);
  assert.equal(frame.getAttribute("sandbox"), "allow-same-origin");
  assert.match(frame.srcdoc, /default-src 'none'/);
  assert.match(frame.srcdoc, /Saved report|Thumbnail fixture/);
  assert.doesNotMatch(frame.srcdoc, /<script|<iframe|target=|ping=|https:\/\//);
  frame.onload();
  click({ target: elements[3], preventDefault() {} });
  assert.deepEqual(client.opened[0], ["/view#%2Fartifacts%2Fdeliverables%2Frun%2Ftask%2Ffixture.html", "_blank", "noopener,noreferrer"]);

  const nativeLink = new Element("a", { href: "../deliverables/run/fixture%20file.html" });
  const native = page("", new Map(), [nativeLink]);
  const links = [];
  const nativeSource = native.preview.fileURL("/tmp/state/artifacts/versions/Legacy report.html");
  native.preview.render(frame, "inert fixture", nativeSource, (url) => links.push(url.href));
  frame.onload();
  click({ target: nativeLink, preventDefault() {} });
  assert.equal(links[0], "file:///tmp/state/artifacts/deliverables/run/fixture%20file.html");
  await native.preview.downloadFile(async (name) => {
    assert.equal(name, "/tmp/state/artifacts/deliverables/run/fixture file.html");
    return [65, 66, 67];
  }, new URL(links[0]));
  assert.equal(native.downloads[0].name, "fixture file.html");
  assert.equal(native.downloads[0].blob.type, "application/octet-stream");
  assert.equal(await native.downloads[0].blob.text(), "ABC");
}

check().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
