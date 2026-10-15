// ESLint settings for the worker (ESLint "flat config").
const js = require('@eslint/js');
const globals = require('globals');

module.exports = [
  // ESLint's recommended rules: unused variables, typos, etc.
  js.configs.recommended,
  {
    files: ['**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'commonjs',
      globals: { ...globals.node },
    },
  },
  {
    // Test files may use Jest's describe, test, expect, jest
    files: ['**/*.test.js'],
    languageOptions: { globals: { ...globals.jest } },
  },
];
