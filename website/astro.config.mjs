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
			description: 'Local-first experiment tracking for machine learning.',
			logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', alt: 'agenticML', replacesTitle: true },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/agenticML/agenticml-lite' },
			],
			editLink: {
				baseUrl: 'https://github.com/agenticML/agenticml-lite/edit/dev/website/',
			},
			lastUpdated: true,
			customCss: ['./src/styles/custom.css'],
			sidebar: [
				{ label: 'Getting started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Hosted service', items: [{ autogenerate: { directory: 'hosted' } }] },
				{ label: 'Releases', slug: 'releases' },
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'Server', items: [{ autogenerate: { directory: 'server' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
			],
		}),
	],
});
