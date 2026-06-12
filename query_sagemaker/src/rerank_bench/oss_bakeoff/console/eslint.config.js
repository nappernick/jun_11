import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    // ui.tsx intentionally co-locates the shared theme constants with the UI
    // primitives per the build contract. Allow the THEME export alongside the
    // components rather than suppressing the rule outright.
    files: ['src/lib/ui.tsx'],
    rules: {
      'react-refresh/only-export-components': ['error', { allowExportNames: ['THEME'] }],
    },
  },
])
