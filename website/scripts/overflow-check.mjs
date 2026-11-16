// Checks every built page for horizontal overflow at 375px, light and dark.
// Also checks that the home page and the Python SDK guide carry the banner
// text and a link to the hosted sign-in (package 29.8).
//
// Run after `npm run build`: npm run check:overflow
import { createServer } from 'node:http';
import { readFile, readdir, stat } from 'node:fs/promises';
import { dirname, extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const DIST = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
const BASE = '/agenticml-lite';
const SITE_ORIGIN = 'https://agenticml.github.io';
const WIDTH = 375;
const HEIGHT = 812;
const THEMES = ['light', 'dark'];
const BANNER_TEXT = 'Hosted service in public preview.';
const SIGN_IN_URL = 'https://app.agenticml.xyz';
const BANNER_PAGES = [`${BASE}/`, `${BASE}/guides/python-sdk/`];

const TYPES = {
	'.html': 'text/html; charset=utf-8',
	'.css': 'text/css; charset=utf-8',
	'.js': 'text/javascript; charset=utf-8',
	'.mjs': 'text/javascript; charset=utf-8',
	'.json': 'application/json; charset=utf-8',
	'.svg': 'image/svg+xml',
	'.xml': 'application/xml; charset=utf-8',
	'.woff2': 'font/woff2',
	'.png': 'image/png',
	'.webp': 'image/webp',
	'.ico': 'image/x-icon',
	'.wasm': 'application/wasm',
};

function fail(message) {
	console.error(`overflow-check: ${message}`);
	process.exit(1);
}

async function resolveFile(urlPath) {
	if (urlPath !== BASE && !urlPath.startsWith(`${BASE}/`)) return null;
	let rel;
	try {
		rel = decodeURIComponent(urlPath.slice(BASE.length));
	} catch {
		return null;
	}
	const target = normalize(join(DIST, rel));
	if (target !== DIST && !target.startsWith(DIST + sep)) return null;
	const candidates = rel.endsWith('/') || rel === '' ? [join(target, 'index.html')] : [target, join(target, 'index.html')];
	for (const candidate of candidates) {
		try {
			if ((await stat(candidate)).isFile()) return candidate;
		} catch {}
	}
	return null;
}

function startServer() {
	const server = createServer(async (req, res) => {
		const urlPath = new URL(req.url ?? '/', 'http://localhost').pathname;
		const file = await resolveFile(urlPath);
		if (!file) {
			res.writeHead(404, { 'content-type': 'text/plain' });
			res.end('not found');
			return;
		}
		res.writeHead(200, { 'content-type': TYPES[extname(file)] ?? 'application/octet-stream' });
		res.end(await readFile(file));
	});
	return new Promise((done) => server.listen(0, '127.0.0.1', () => done(server)));
}

async function sitemapPaths() {
	let names;
	try {
		names = (await readdir(DIST)).filter((n) => /^sitemap-\d+\.xml$/.test(n));
	} catch {
		fail(`${DIST} not found. Run "npm run build" first.`);
	}
	const paths = new Set();
	for (const name of names) {
		const xml = await readFile(join(DIST, name), 'utf8');
		for (const [, loc] of xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)) {
			if (!loc.startsWith(SITE_ORIGIN)) fail(`${name}: unexpected origin in ${loc}`);
			paths.add(loc.slice(SITE_ORIGIN.length));
		}
	}
	// The 404 page is not in the sitemap.
	paths.add(`${BASE}/404.html`);
	return [...paths].sort();
}

const server = await startServer();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const problems = [];
let checked = 0;
let pageCount = 0;

try {
	const paths = await sitemapPaths();
	if (paths.length <= 1) fail('no pages found in dist/sitemap-*.xml');
	pageCount = paths.length;

	for (const theme of THEMES) {
		const context = await browser.newContext({
			viewport: { width: WIDTH, height: HEIGHT },
			colorScheme: theme,
		});
		await context.addInitScript((t) => localStorage.setItem('starlight-theme', t), theme);
		const page = await context.newPage();
		for (const path of paths) {
			const response = await page.goto(origin + path, { waitUntil: 'load' });
			// The 404 page answers with 404 by design; the file is served as 200 here.
			if (!response || response.status() !== 200) {
				problems.push(`${path} (${theme}): HTTP ${response ? response.status() : 'no response'}`);
				continue;
			}
			const result = await page.evaluate(() => ({
				scrollWidth: document.documentElement.scrollWidth,
				theme: document.documentElement.dataset.theme,
			}));
			checked += 1;
			if (result.theme !== theme) {
				problems.push(`${path} (${theme}): theme did not switch (data-theme is "${result.theme}")`);
			}
			if (result.scrollWidth > WIDTH) {
				problems.push(`${path} (${theme}): scrollWidth ${result.scrollWidth} > ${WIDTH}`);
			}
			if (theme === THEMES[0] && BANNER_PAGES.includes(path)) {
				const banner = await page.evaluate(
					([text, url]) => ({
						text: document.body.innerText.includes(text),
						link: [...document.querySelectorAll('a[href]')].some((a) => a.href === url || a.href === `${url}/`),
					}),
					[BANNER_TEXT, SIGN_IN_URL],
				);
				if (!banner.text) problems.push(`${path}: banner text "${BANNER_TEXT}" missing`);
				if (!banner.link) problems.push(`${path}: link to ${SIGN_IN_URL} missing`);
			}
		}
		await context.close();
	}
} finally {
	await browser.close();
	server.close();
}

if (problems.length > 0) {
	console.error(`overflow-check: ${problems.length} problem(s):`);
	for (const p of problems) console.error(`  ${p}`);
	process.exit(1);
}
console.log(`overflow-check: ${pageCount} pages x ${THEMES.length} themes = ${checked} checks at ${WIDTH}px, 0 problems`);
