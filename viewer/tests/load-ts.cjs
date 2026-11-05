const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const ts = require('typescript');

module.exports = function makeLoader(overrides = {}) {
  const cache = new Map();
  function load(filename) {
    filename = path.resolve(__dirname, filename);
    if (cache.has(filename)) return cache.get(filename);
    const sourceRequire = createRequire(filename);
    const localRequire = name => {
      if (name in overrides) return overrides[name];
      if (name.startsWith('.')) return load(path.resolve(path.dirname(filename), name + '.ts'));
      return sourceRequire(name);
    };
    const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    }).outputText;
    const mod = {exports:{}};
    new Function('require', 'module', 'exports', code)(localRequire, mod, mod.exports);
    cache.set(filename, mod.exports);
    return mod.exports;
  }
  return load;
};
