const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');
const { join } = require('node:path');

const html = readFileSync(join(__dirname, '../index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = '';
    this.textContent = '';
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
  dispatch(name, target = this) { return this.listeners[name]({ target }); }
}

async function page({ directory, failed = false, storageBlocked = false, savedLanguage = null } = {}) {
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const translated = [...html.matchAll(/data-i18n="([^"]+)"/g)].map(match => {
    const element = new Element();
    element.dataset.i18n = match[1];
    return element;
  });
  const document = {
    documentElement: {},
    getElementById: id => elements.get(id),
    createElement: () => new Element(),
    querySelectorAll: () => translated,
  };
  let copied;
  const navigator = { language: 'de-DE', clipboard: { writeText: async text => { copied = text; } } };
  const requests = [];
  runInNewContext(script, {
    document, navigator, URL,
    window: { location: { href: 'https://example.org/calendars/index.html' } },
    localStorage: {
      getItem: () => { if (storageBlocked) throw new Error('Blocked'); return savedLanguage; },
      setItem: () => { if (storageBlocked) throw new Error('Blocked'); },
    },
    fetch: async url => {
      requests.push(url.href);
      return { ok: !failed, json: async () => directory || [
        { slug: 'new-league', name: 'New League', region: 'EMEA' },
        { slug: 'lec', name: 'LEC', region: 'EMEA' },
      ] };
    },
    setTimeout: () => {},
  });
  await new Promise(resolve => setImmediate(resolve));
  return { document, elements, requests, navigator, copied: () => copied };
}

test('loads dynamic leagues, selects using native checkbox change and copies the correct URL', async () => {
  const view = await page();
  assert.deepEqual(view.requests, ['https://example.org/calendars/leagues.json']);
  const grid = view.elements.get('leagueGrid');
  assert.equal(grid.children.length, 2);
  const input = grid.children[0].children[0];
  input.checked = true;
  grid.dispatch('change', input);
  const row = view.elements.get('urlList').children[0];
  assert.equal(row.children[1].textContent, 'https://example.org/calendars/feed-new-league.ics');
  await row.children[2].dispatch('click');
  assert.equal(view.copied(), row.children[1].textContent);
  assert.equal(row.children[2].textContent, 'Kopiert!');
});

test('language switch preserves selection and search; blocked storage is harmless', async () => {
  const view = await page({ storageBlocked: true });
  const grid = view.elements.get('leagueGrid');
  const input = grid.children[0].children[0];
  input.checked = true;
  grid.dispatch('change', input);
  const search = view.elements.get('leagueSearch');
  search.value = 'new';
  search.dispatch('input');
  const language = view.elements.get('language');
  language.value = 'en';
  language.dispatch('change');
  assert.equal(view.document.documentElement.lang, 'en');
  assert.equal(grid.children.length, 1);
  assert.equal(grid.children[0].children[0].checked, true);
  assert.equal(view.elements.get('urlList').children[0].children[2].textContent, 'Copy');
});

test('restores the saved language preference', async () => {
  const view = await page({ savedLanguage: 'en' });
  assert.equal(view.document.documentElement.lang, 'en');
  assert.equal(view.elements.get('leagueSearch').placeholder, 'Search leagues or regions');
});

test('selecting all leagues yields the combined feed', async () => {
  const view = await page();
  const grid = view.elements.get('leagueGrid');
  for (const item of grid.children) {
    const input = item.children[0];
    input.checked = true;
    grid.dispatch('change', input);
  }
  assert.equal(view.elements.get('urlList').children.length, 1);
  assert.equal(view.elements.get('urlList').children[0].children[1].textContent, 'https://example.org/calendars/feed.ics');
});

test('failed directory request is visible and invalid slugs are not rendered', async () => {
  const failed = await page({ failed: true });
  assert.match(failed.elements.get('leagueStatus').textContent, /nicht verfügbar/);
  const view = await page({ directory: [
    { slug: '../escape', name: 'Unsafe' },
    { slug: 'retired', name: 'Retired League', hidden: true },
    { slug: 'new', name: '<img src=x onerror=alert(1)>' },
  ] });
  const grid = view.elements.get('leagueGrid');
  assert.equal(grid.children.length, 1);
  assert.equal(grid.children[0].children[1].children[0].textContent, '<img src=x onerror=alert(1)>');
});

test('clipboard errors are handled without losing the URL', async () => {
  const view = await page();
  view.navigator.clipboard = undefined;
  const grid = view.elements.get('leagueGrid');
  const input = grid.children[0].children[0];
  input.checked = true;
  grid.dispatch('change', input);
  const row = view.elements.get('urlList').children[0];
  await row.children[2].dispatch('click');
  assert.match(row.children[2].textContent, /manuell kopieren/);
  assert.match(row.children[1].textContent, /feed-new-league\.ics$/);
});
