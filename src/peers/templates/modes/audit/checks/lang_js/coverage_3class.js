#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const kinds = {
  happy: /(happy|ok|success|nominal|baseline)/i,
  edge: /(edge|boundary|empty|max|min|long|unicode)/i,
  sad: /(sad|fail|error|invalid|exception|timeout|broken)/i,
};

function walk(dir, suffixes, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    if (["node_modules", ".git", ".peers", "dist", "coverage"].includes(ent.name)) continue;
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(p, suffixes, out);
    else if (suffixes.some((s) => p.endsWith(s))) out.push(p);
  }
  return out;
}

const tests = walk("tests", [".test.js", ".test.ts", ".spec.js", ".spec.ts"]);
const seen = new Set();
for (const file of tests) {
  const text = fs.readFileSync(file, "utf8");
  for (const name of text.matchAll(/\b(?:it|test)\s*\(\s*["'`]([^"'`]+)/g)) {
    for (const [kind, rx] of Object.entries(kinds)) {
      if (rx.test(name[1])) seen.add(kind);
    }
  }
}
const missing = Object.keys(kinds).filter((kind) => !seen.has(kind));
if (missing.length) {
  console.log(`coverage_3class_js FAIL: missing ${missing.join(", ")} test class(es)`);
  process.exit(1);
}
console.log("coverage_3class_js: clean");
