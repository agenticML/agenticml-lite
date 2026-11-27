// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightBlog from 'starlight-blog';
import starlightLinksValidator from 'starlight-links-validator';

// Served from GitHub Pages at https://agenticml.xyz/ (custom domain, root path).
// There is no base: links inside pages start with / and carry no prefix.
export default defineConfig({
	site: 'https://agenticml.xyz',
	integrations: [
		starlight({
			plugins: [
				starlightBlog({
					title: 'Blog',
					authors: { team: { name: 'agenticML team', url: 'https://github.com/agenticML' } },
				}),
				starlightLinksValidator({ errorOnLocalLinks: false }),
			],
			title: 'agenticml-lite',
			description: 'Experiment tracking for machine learning: local files, a self-hosted server and a hosted service preview.',
			logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', alt: 'agenticML', replacesTitle: true },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/agenticML/agenticml-lite' },
			],
			editLink: {
				baseUrl: 'https://github.com/agenticML/agenticml-lite/edit/main/website/',
			},
			routeMiddleware: './src/routeData.ts',
			components: {
				SocialIcons: './src/components/SocialIcons.astro',
				PageFrame: './src/components/PageFrame.astro',
			},
			lastUpdated: true,
			customCss: ['./src/styles/custom.css'],
			sidebar: [
				{ label: 'Getting started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Hosted service', items: [{ autogenerate: { directory: 'hosted' } }] },
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'On-prem server', items: [{ autogenerate: { directory: 'server' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
				{ label: 'Releases', slug: 'releases' },
				{ label: 'Legal', items: [{ autogenerate: { directory: 'legal' } }] },
			],
		}),
	],
});
