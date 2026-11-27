// Checks every built page for horizontal overflow at 375px, light and dark.
// Also checks that the home page and the Python SDK guide carry the banner
// text and a link to the hosted sign-in (package 29.8).
// Also checks the Contact us button (package 37.1): every page has it, and on
// the home page and the Python SDK guide the open panel causes no overflow in
// either theme and its mailto: link carries the address and the page title.
//
// Also checks the blog (package 39.1): /blog/, every post and a tag page are in
// the built site (so they get the overflow and Contact us checks above) and
// /blog/rss.xml has one <item> per post, with links on the site origin. The
// header and the mobile menu show no RSS icon (a link to the raw XML), while
// the page <head> keeps the RSS alternate link for feed readers.
//
// Run after `npm run build`: npm run check:overflow
import { createServer } from 'node:http';
import { readFile, readdir, stat } from 'node:fs/promises';
import { dirname, extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const WEBSITE = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(WEBSITE, 'dist');
const BLOG_SRC = join(WEBSITE, 'src', 'content', 'docs', 'blog');
// The site is served at the root, so there is no base path.
const BASE = '';
const SITE_ORIGIN = 'https://agenticml.xyz';
const WIDTH = 375;
const HEIGHT = 812;
const THEMES = ['light', 'dark'];
const BANNER_TEXT = 'Hosted service in public preview.';
const SIGN_IN_URL = 'https://app.agenticml.xyz';
const BANNER_PAGES = [`${BASE}/`, `${BASE}/guides/python-sdk/`];
const RSS_PAGES = [`${BASE}/`, `${BASE}/blog/`];
const CONTACT_EMAIL = 'contact@agenticml.xyz';
const CONTACT_PAGES = [...BANNER_PAGES, `${BASE}/404.html`];

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

async function checkContactPanel(page, path, theme) {
	const button = page.locator('button#contact-button');
	const panel = page.locator('#contact-panel');
	await button.click();
	if (!(await panel.isVisible())) {
		problems.push(`${path} (${theme}): contact panel did not open`);
		return;
	}
	const open = await page.evaluate(() => {
		const box = document.getElementById('contact-panel').getBoundingClientRect();
		return { scrollWidth: document.documentElement.scrollWidth, left: box.left, right: box.right };
	});
	if (open.scrollWidth > WIDTH) {
		problems.push(`${path} (${theme}): contact panel open, scrollWidth ${open.scrollWidth} > ${WIDTH}`);
	}
	if (open.left < 0 || open.right > WIDTH) {
		problems.push(`${path} (${theme}): contact panel outside the viewport (${open.left}..${open.right})`);
	}
	if (theme === THEMES[0]) {
		const href = await page.locator('#contact-panel a[href^="mailto:"]').getAttribute('href');
		const url = new URL(href ?? 'mailto:');
		const title = await page.evaluate(() => document.querySelector('h1')?.textContent?.trim() ?? '');
		const subject = url.searchParams.get('subject') ?? '';
		const body = url.searchParams.get('body') ?? '';
		if (decodeURIComponent(url.pathname) !== CONTACT_EMAIL) {
			problems.push(`${path}: mailto address is "${url.pathname}", expected ${CONTACT_EMAIL}`);
		}
		if (!subject.startsWith('agenticml docs: ') || (title && !subject.includes(title))) {
			problems.push(`${path}: mailto subject "${subject}" lacks the page title "${title}"`);
		}
		// The 404 page fills in its Page line from location, so it names the local origin here.
		const origin = path.endsWith('/404.html') ? await page.evaluate(() => location.origin) : SITE_ORIGIN;
		if (!body.includes(`Page: ${origin}${path}`)) {
			problems.push(`${path}: mailto body lacks the page URL: "${body}"`);
		}
		for (const label of ['Write an email', 'Copy address', 'Ask on GitHub', 'Report a bug']) {
			if ((await panel.getByText(label, { exact: true }).count()) !== 1) {
				problems.push(`${path}: contact panel action "${label}" missing`);
			}
		}
	}
	await page.keyboard.press('Escape');
	if (await panel.isVisible()) problems.push(`${path} (${theme}): contact panel did not close on Escape`);
	const focused = await page.evaluate(() => document.activeElement?.id);
	if (focused !== 'contact-button') {
		problems.push(`${path} (${theme}): focus did not return to the Contact us button (on "${focused}")`);
	}
}

// One post per .md/.mdx file in src/content/docs/blog/ (slug = file name).
async function postSlugs() {
	const names = await readdir(BLOG_SRC);
	return names.filter((n) => /\.mdx?$/.test(n)).map((n) => n.replace(/\.mdx?$/, '')).sort();
}

async function checkBlog(paths) {
	const slugs = await postSlugs();
	if (slugs.length === 0) {
		problems.push(`no posts in ${BLOG_SRC}`);
		return;
	}
	const required = [`${BASE}/blog/`, ...slugs.map((slug) => `${BASE}/blog/${slug}/`)];
	for (const path of required) {
		if (!paths.includes(path)) problems.push(`${path}: blog page missing from the sitemap`);
	}
	if (!paths.some((path) => path.startsWith(`${BASE}/blog/tags/`))) {
		problems.push('no blog tag page in the sitemap');
	}
	let rss;
	try {
		rss = await readFile(join(DIST, 'blog', 'rss.xml'), 'utf8');
	} catch {
		problems.push('blog/rss.xml missing from dist/');
		return;
	}
	const items = rss.match(/<item>/g)?.length ?? 0;
	if (items !== slugs.length) problems.push(`blog/rss.xml has ${items} <item> element(s), expected ${slugs.length} (one per post)`);
	for (const slug of slugs) {
		if (!rss.includes(`<link>${SITE_ORIGIN}${BASE}/blog/${slug}/</link>`)) {
			problems.push(`blog/rss.xml has no item linking to ${SITE_ORIGIN}${BASE}/blog/${slug}/`);
		}
	}
	for (const [, link] of rss.matchAll(/<link>([^<]*)<\/link>/g)) {
		if (!link.startsWith(SITE_ORIGIN)) problems.push(`blog/rss.xml: link ${link} is not on ${SITE_ORIGIN}`);
	}
}

// The RSS icon that starlight-blog adds opens raw XML, so SocialIcons.astro leaves
// it out. The feed and the <link rel="alternate"> in <head> must stay.
async function checkRssLinks(page, path) {
	const result = await page.evaluate(() => {
		const isRss = (el) => new URL(el.getAttribute('href') ?? '', location.href).pathname === '/blog/rss.xml';
		// The social icons sit in the header and in the mobile menu footer. The
		// blog sidebar's own "RSS" text link is a separate plugin feature.
		const icons = [...document.querySelectorAll('header .social-icons a[href], .mobile-preferences .social-icons a[href]')];
		return {
			rss: icons.filter(isRss).map((a) => a.outerHTML.slice(0, 120)),
			github: icons.filter((a) => a.href.startsWith('https://github.com/')).length,
			alternate: [...document.head.querySelectorAll('link[rel="alternate"][type="application/rss+xml"]')].filter(isRss).length,
		};
	});
	for (const html of result.rss) problems.push(`${path}: RSS icon link in the header or mobile menu: ${html}`);
	if (result.github === 0) problems.push(`${path}: GitHub icon missing from the header`);
	if (result.alternate !== 1) {
		problems.push(`${path}: expected 1 RSS alternate <link> in <head> for /blog/rss.xml, found ${result.alternate}`);
	}
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
	await checkBlog(paths);

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
			if (theme === THEMES[0] && (await page.locator('button#contact-button').count()) !== 1) {
				problems.push(`${path}: Contact us button missing`);
			}
			if (theme === THEMES[0] && RSS_PAGES.includes(path)) await checkRssLinks(page, path);
			if (CONTACT_PAGES.includes(path)) await checkContactPanel(page, path, theme);
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
