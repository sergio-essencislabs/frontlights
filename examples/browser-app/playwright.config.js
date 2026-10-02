// Configuração mínima do Playwright para o app de exemplo (só Chromium, sem servidor próprio: quem sobe o app
// é o `serve`). A pasta de resultados vem de FRONTLIGHTS_RESULTS_DIR, que o run_spec.py cria fora do repositório.
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  testMatch: 'login.spec.js',
  outputDir: process.env.FRONTLIGHTS_RESULTS_DIR || 'test-results',
  retries: 0,
  workers: 1,
  use: { baseURL: process.env.FRONTLIGHTS_BASE_URL, trace: 'off' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
