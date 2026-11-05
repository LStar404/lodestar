const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const ts = require("typescript");

// Execute the production TypeScript with real markdown-it/KaTeX. Only Vite's
// CSS imports are omitted because these tests verify rendered HTML, not fonts.
const cache = new Map();
function loadTs(filename) {
  if (cache.has(filename)) return cache.get(filename);
  const sourceRequire = createRequire(filename);
  const localRequire = (name) => {
    if (/\.css(?:\?raw)?$/.test(name)) return "";
    if (name.startsWith(".")) return loadTs(path.resolve(path.dirname(filename), name + ".ts"));
    return sourceRequire(name);
  };
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
                       esModuleInterop: true },
  }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", output)(localRequire, mod, mod.exports);
  cache.set(filename, mod.exports);
  return mod.exports;
}

const { renderSharedBlock, buildShareHtml } = loadTs(path.resolve(__dirname, "../src/lib/share.ts"));
const image = "data:image/png;base64,aA==";

test("legacy packages containing regex metacharacters cannot crash the viewer", () => {
  assert.match(renderSharedBlock("hello", { "[": image, "(a+)+": image }), /hello/);
});

test("every figure token is embedded while ordinary images retain their own URLs", () => {
  const html = renderSharedBlock(
    "![fig_00000001] ![fig_00000001] ![external](https://example.test/fig_00000001)",
    { fig_00000001: image },
  );
  assert.equal(html.split('src="' + image + '"').length - 1, 2);
  assert.ok(html.includes('src="https://example.test/fig_00000001"'));
});

test("image URLs cannot introduce HTML attributes", () => {
  const html = renderSharedBlock("![fig_00000001]", {
    fig_00000001: image + '" onload="alert(1)',
  });
  assert.ok(!html.includes(' onload="alert(1)'));
  assert.ok(html.includes("&quot;"));
});

test("HTML export embeds figures without rewriting literal URLs in code", async () => {
  const apiPath = "/api/v1/docs/test-book/figures/fig_00000001";
  const html = await buildShareHtml({
    doc_id: "test-book", title: "Share", filename: "share.md", markdown: "",
    figures: { fig_00000001: image },
    blocks: [{ id: "b", content: "`" + apiPath + "`\n\n![fig_00000001]", sources: [] }],
  });
  assert.ok(html.includes("<code>" + apiPath + "</code>"));
  assert.ok(html.includes('src="' + image + '"'));
});
