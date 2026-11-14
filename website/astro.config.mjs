// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

// Served from GitHub Pages at https://agenticml.github.io/agenticml-lite/.
// Links inside pages must include the /agenticml-lite base.
export default defineConfig({
	site: 'https://agenticml.github.io',
	base: '/agenticml-lite',
	integrations: [
		starlight({
			plugins: [starlightLinksValidator({ errorOnLocalLinks: false })],
			title: 'agenticml-lite',
			description: 'Experiment tracking for machine learning: local files, a self-hosted server and a hosted service preview.',
			logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', alt: 'agenticML', replacesTitle: true },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/agenticML/agenticml-lite' },
				{ icon: 'external', label: 'Sign in', href: 'https://app.agenticml.xyz' },
			],
			editLink: {
				baseUrl: 'https://github.com/agenticML/agenticml-lite/edit/dev/website/',
			},
			routeMiddleware: './src/routeData.ts',
			lastUpdated: true,
			customCss: ['./src/styles/custom.css'],
			sidebar: [
				{ label: 'Getting started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Hosted service', items: [{ autogenerate: { directory: 'hosted' } }] },
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'Server', items: [{ autogenerate: { directory: 'server' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
				{ label: 'Releases', slug: 'releases' },
			],
		}),
	],
});
