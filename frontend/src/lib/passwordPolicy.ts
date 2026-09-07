// Mirrors AuthService.validate_password_strength in backend/services/auth.py.
// SECURITY__PASSWORD_MIN_LENGTH defaults to 10 and is bounded at >= 8 server side.
export const PASSWORD_MIN_LENGTH = 10;
export const PASSWORD_SYMBOLS = "!@#$%^&*()-_=+[]{};:,.<>/?";

export function passwordProblems(password: string, minLength = PASSWORD_MIN_LENGTH): string[] {
  const problems: string[] = [];
  if (password.length === 0) return problems;
  if (password.length < minLength) problems.push(`at least ${minLength} characters`);
  if (!/[0-9]/.test(password)) problems.push("a digit");
  if (!/[A-Za-z]/.test(password)) problems.push("a letter");
  if (![...PASSWORD_SYMBOLS].some((symbol) => password.includes(symbol))) {
    problems.push("a symbol");
  }
  return problems;
}

export function passwordProblemText(password: string, minLength = PASSWORD_MIN_LENGTH): string | undefined {
  const problems = passwordProblems(password, minLength);
  return problems.length > 0 ? `Password needs ${problems.join(", ")}` : undefined;
}