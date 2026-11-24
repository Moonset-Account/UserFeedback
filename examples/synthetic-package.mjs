/**
 * Local synthetic extension package used by tests and README examples.
 * No real extension, credential, network target, or external data is involved.
 */
export const syntheticPackage = {
  name: 'synthetic-clipper',
  version: '1.2.3',
  resources: {
    'shared/core.js': {
      features: ['core'],
      sites: ['https://docs.example.com/workspace/*'],
      permissions: [
        'storage',
        { id: 'identity', timing: 'install' },
      ],
    },
    'features/export.js': {
      optionalFeatures: [{ id: 'export-pdf' }],
      permissions: [
        { id: 'downloads', timing: 'optional', feature: 'export-pdf' },
        { id: 'clipboardWrite', timing: 'optional', feature: 'export-pdf' },
        { id: 'geolocation', timing: 'optional' },
      ],
      sites: [
        { pattern: 'https://files.example.com/export/july/*', timing: 'optional', feature: 'export-pdf' },
      ],
    },
    'features/ai.js': {
      feature: { id: 'ai-summary', optional: true },
      permissions: [
        { id: 'activeTab', timing: 'enable', feature: 'ai-summary' },
      ],
      sites: [
        { pattern: 'https://ai.example.com/api/summarize', timing: 'enable', feature: 'ai-summary' },
      ],
    },
    'features/legacy-sync.js': {
      optionalFeatures: [{ id: 'legacy-sync' }],
      permissions: [
        { id: 'identity', timing: 'optional', feature: 'legacy-sync' },
      ],
      sites: [
        { pattern: 'https://sync.example.com/legacy/*', timing: 'optional', feature: 'legacy-sync' },
      ],
    },
    'features/diagnostics.js': {
      features: ['diagnostics'],
      permissions: ['management'],
      sites: ['https://telemetry.example.com/v1/*'],
    },
  },
  variants: {
    chrome: {
      browser: 'chrome',
      version: '130.0.6723',
      flags: { ai: true },
      loads: [
        { resource: 'shared/core.js' },
        { resource: 'features/export.js', when: { browser: 'chrome', minVersion: '120' } },
        { resource: 'features/ai.js', when: { browser: 'chrome', flag: 'ai' } },
        { resource: 'features/legacy-sync.js', when: { browser: 'chrome', minVersion: '999' } },
        { resource: 'features/diagnostics.js', when: { browser: 'chrome' } },
      ],
      optionalFeatures: [
        {
          id: 'cloud-vault',
          permissions: [{ id: 'cloudVault.read', timing: 'optional', feature: 'cloud-vault' }],
          sites: [{ pattern: 'https://vault.example.com/*', timing: 'optional', feature: 'cloud-vault' }],
        },
      ],
      approval: {
        features: [
          { id: 'core', permissions: ['storage', 'identity'], sites: ['https://docs.example.com/*'] },
          { id: 'export-pdf', optional: true, permissions: ['downloads'], sites: ['https://files.example.com/export/*'] },
          { id: 'ai-summary', optional: true, permissions: ['activeTab'], sites: ['https://ai.example.com/api/*'] },
        ],
        installPermissions: ['storage', 'identity'],
        sites: ['https://docs.example.com/*'],
      },
    },
    firefox: {
      browser: 'firefox',
      version: '132.0',
      loads: [
        { resource: 'shared/core.js' },
        { resource: 'features/export.js', when: { browser: 'firefox', minVersion: '140' } },
        { resource: 'features/ai.js', when: { browser: 'firefox', minVersion: '120' } },
        { resource: 'features/legacy-sync.js', when: { browser: 'firefox', minVersion: '120' } },
      ],
      approval: {
        features: [
          { id: 'core', permissions: ['storage', 'identity'], sites: ['https://docs.example.com/*'] },
          { id: 'ai-summary', optional: true, permissions: ['activeTab'], sites: ['https://ai.example.com/api/*'] },
          { id: 'legacy-sync', optional: true, permissions: ['identity'], sites: ['https://sync.example.com/legacy/*'] },
        ],
        installPermissions: ['storage', 'identity'],
        sites: ['https://docs.example.com/*'],
      },
    },
  },
};

export function cloneSyntheticPackage() {
  return structuredClone(syntheticPackage);
}
