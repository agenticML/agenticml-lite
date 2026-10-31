// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Served from GitHub Pages at https://agenticml.github.io/agenticml-lite/.
// Links inside pages must include the /agenticml-lite base.
export default defineConfig({
	site: 'https://agenticml.github.io',
	base: '/agenticml-lite',
	integrations: [
		starlight({
			title: 'agenticml-lite',
			description: 'Local-first experiment tracking for machine learning.',
			logo: { src: './src/assets/logo.svg', replacesTitle: false },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/agenticML/agenticml-lite' },
			],
			editLink: {
				baseUrl: 'https://github.com/agenticML/agenticml-lite/edit/main/website/',
			},
			lastUpdated: true,
			customCss: ['./src/styles/custom.css'],
			sidebar: [
				{ label: 'Getting started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
			],
		}),
	],
});
