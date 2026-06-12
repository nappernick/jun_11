module.exports = {
  testMatch: ['**/**.test.ts'],
  transform: {
    '.(js|ts)': 'ts-jest',
  },
  transformIgnorePatterns: ['[/\\\\]node_modules[/\\\\].+\\.(js|jsx|ts|tsx|json)$', 'package.json'],
  collectCoverage: true,
  collectCoverageFrom: ['lib/**/*.{ts,js}'],
  coverageDirectory: '<rootDir>/build/brazil-documentation/coverage',
  coverageReporters: ['cobertura'],
};
