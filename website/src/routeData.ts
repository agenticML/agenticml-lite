import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

// Site-wide banner. A page's own `banner` frontmatter wins.
const BANNER = 'Hosted service in public preview.';

export const onRequest = defineRouteMiddleware((context) => {
	const { entry } = context.locals.starlightRoute;
	entry.data.banner ??= { content: BANNER };
});
