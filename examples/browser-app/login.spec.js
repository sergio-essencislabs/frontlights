// Spec Playwright do app de exemplo: login com a conta fictícia do config.json.
// O run_spec.py entrega a URL e a conta nas variáveis FRONTLIGHTS_BASE_URL, FRONTLIGHTS_LOGIN e FRONTLIGHTS_PASSWORD.
const { test, expect } = require('@playwright/test');

const login = process.env.FRONTLIGHTS_LOGIN;
const password = process.env.FRONTLIGHTS_PASSWORD;

test('entra com a conta fictícia e vê o painel', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Login').fill(login);
  await page.getByLabel('Senha').fill(password);
  await page.getByRole('button', { name: 'Entrar' }).click();
  await expect(page.getByRole('heading', { name: 'Painel' })).toBeVisible();
  await expect(page.getByTestId('usuario')).toHaveText(login);
});

test('recusa a senha errada e continua no formulário', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Login').fill(login);
  await page.getByLabel('Senha').fill(password + '-errada');
  await page.getByRole('button', { name: 'Entrar' }).click();
  await expect(page.getByRole('alert')).toHaveText('Login ou senha inválidos.');
  await expect(page.getByRole('heading', { name: 'Entrar' })).toBeVisible();
});
