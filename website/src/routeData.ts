import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

// Site-wide banner. A page's own `banner` frontmatter wins.
// 29.9: shorten this once 3.0 is on PyPI.
const BANNER = 'Hosted service in public preview. 3.0 installs from GitHub until it is on PyPI.';

export const onRequest = defineRouteMiddleware((context) => {
	const { entry } = context.locals.starlightRoute;
	entry.data.banner ??= { content: BANNER };
});
