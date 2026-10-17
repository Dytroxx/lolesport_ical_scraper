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

test('renders lazy decorative logos and handles broken images without losing selection', async () => {
  const view = await page({ directory: [
    { slug: 'lec', name: 'LEC', image: 'https://static.lolesports.com/leagues/lec.png' },
    { slug: 'lck', name: 'LCK' },
    { slug: 'unsafe', name: 'Unsafe', image: 'javascript:alert(1)' },
  ] });
  const grid = view.elements.get('leagueGrid');
  const [checkbox, icon, text] = grid.children[0].children;
  assert.equal(icon.src, 'https://static.lolesports.com/leagues/lec.png');
  assert.equal(icon.alt, '');
  assert.equal(icon.loading, 'lazy');
  assert.equal(icon.decoding, 'async');
  assert.equal(icon.referrerPolicy, 'no-referrer');
  assert.equal(icon.width, 72);
  assert.equal(icon.height, 72);
  assert.equal(text.children[0].textContent, 'LEC');
  icon.dispatch('error');
  assert.equal(icon.hidden, true);
  checkbox.checked = true;
  grid.dispatch('change', checkbox);
  assert.match(view.elements.get('urlList').children[0].children[1].textContent, /feed-lec\.ics$/);
  assert.equal(grid.children[1].children.length, 2);
  assert.equal(grid.children[2].children.length, 2);
});

test('region categories combine with search and preserve selections across language and filter changes', async () => {
  const view = await page({ directory: [
    { slug: 'lec', name: 'LEC', region: 'EMEA' },
    { slug: 'lfl', name: 'LFL', region: 'EMEA' },
    { slug: 'lck', name: 'LCK', region: 'KOREA' },
    { slug: 'lcs', name: 'LCS', region: 'AMERICAS' },
    { slug: 'retired', name: 'Retired', region: 'HIDDEN', hidden: true },
    { slug: 'other', name: 'Other' },
  ] });
  const filter = view.elements.get('regionFilter');
  const grid = view.elements.get('leagueGrid');
  assert.deepEqual(filter.children.map(option => option.value), ['', 'AMERICAS', 'EMEA', 'KOREA']);
  assert.equal(filter.children[0].textContent, 'Alle Regionen');
  filter.value = 'KOREA';
  filter.dispatch('change');
  assert.equal(grid.children.length, 1);
  const input = grid.children[0].children[0];
  assert.equal(input.value, 'lck');
  input.checked = true;
  grid.dispatch('change', input);
  filter.value = 'EMEA';
  filter.dispatch('change');
  assert.equal(grid.children.length, 2);
  const search = view.elements.get('leagueSearch');
  search.value = 'lec';
  search.dispatch('input');
  assert.equal(grid.children.length, 1);
  assert.equal(grid.children[0].children[0].value, 'lec');
  const language = view.elements.get('language');
  language.value = 'en';
  language.dispatch('change');
  assert.equal(filter.value, 'EMEA');
  assert.equal(search.value, 'lec');
  assert.equal(filter.children[0].textContent, 'All regions');
  assert.match(view.elements.get('urlList').children[0].children[1].textContent, /feed-lck\.ics$/);
  filter.value = '';
  filter.dispatch('change');
  search.value = '';
  search.dispatch('input');
  assert.equal(grid.children.length, 5);
  assert.equal(grid.children.find(item => item.children[0].value === 'lck').children[0].checked, true);
});

test('region with no search matches shows an empty-state message and can be reset', async () => {
  const view = await page({ directory: [
    { slug: 'lec', name: 'LEC', region: 'EMEA' },
    { slug: 'lck', name: 'LCK', region: 'KOREA' },
  ] });
  const filter = view.elements.get('regionFilter');
  filter.value = 'EMEA';
  filter.dispatch('change');
  const search = view.elements.get('leagueSearch');
  search.value = 'lck';
  search.dispatch('input');
  assert.equal(view.elements.get('leagueGrid').children.length, 0);
  assert.equal(view.elements.get('leagueStatus').textContent, 'Keine passenden Ligen gefunden.');
  filter.value = '';
  filter.dispatch('change');
  assert.equal(view.elements.get('leagueGrid').children.length, 1);
  assert.equal(view.elements.get('leagueStatus').textContent, '');
});

test('restores the saved language preference', async () => {
  const view = await page({ savedLanguage: 'en' });
  assert.equal(view.document.documentElement.lang, 'en');
  assert.equal(view.elements.get('leagueSearch').placeholder, 'Search leagues or regions');
});

test('multiple tiles can be selected independently and deselected', async () => {
  const view = await page({ directory: [
    { slug: 'lec', name: 'LEC' },
    { slug: 'lck', name: 'LCK' },
    { slug: 'lpl', name: 'LPL' },
  ] });
  const grid = view.elements.get('leagueGrid');
  const first = grid.children[0].children[0];
  const second = grid.children[1].children[0];
  for (const input of [first, second]) {
    input.checked = true;
    grid.dispatch('change', input);
  }
  const urls = view.elements.get('urlList');
  assert.equal(urls.children.length, 2);
  assert.match(urls.children[0].children[1].textContent, /feed-lec\.ics$/);
  assert.match(urls.children[1].children[1].textContent, /feed-lck\.ics$/);
  first.checked = false;
  grid.dispatch('change', first);
  assert.equal(urls.children.length, 1);
  assert.match(urls.children[0].children[1].textContent, /feed-lck\.ics$/);
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
