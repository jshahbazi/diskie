const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../Resources/chart.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor() { this.children = []; this.style = {}; this.attributes = {}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
}
const elements = {};
const document = {
  getElementById: id => elements[id] ??= new Element(),
  createElement: () => new Element(),
  createElementNS: () => new Element(),
};
const window = {};
vm.runInNewContext(script, { document, window });
const MiB = 1024 ** 2;
const data = {
  total: 1000 * MiB, free: 200 * MiB, errors: {}, date: 'test',
  tree: { name: 'Data volume', size: 800 * MiB, children: [
    { name: '<example>', size: 700 * MiB, children: [
      { name: 'large.bin', size: 650 * MiB, file: true },
    ] },
  ] },
};
window.showData(data);
assert.equal(elements.list.children.length, 2);
elements.list.children[0].onclick();
elements.list.children[0].onclick();
assert.equal(elements.crumbs.children.length, 5);
elements.list.children[0].onclick();
assert.equal(elements.list.children[0].children[1].textContent, 'large.bin (file)');
elements.crumbs.children[0].onclick();
assert.equal(elements.list.children.length, 2);
window.showData({ ...data, free: 300 * MiB });
assert.equal(elements.crumbs.children.length, 1);
console.log('Chart render, drill-down, breadcrumbs and rescan refresh passed.');
