import { detectLocale, type Locale } from "@/i18n/locale";
import { zhCN } from "@/i18n/locales/zh";

// Module-level active locale so non-React code paths (class components, pure
// formatters) can localise too. The provider keeps it in sync on every change.
let activeLocale: Locale = detectLocale();

export function getActiveLocale(): Locale {
  return activeLocale;
}

export function setActiveLocale(locale: Locale): void {
  activeLocale = locale;
}

export function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return Object.entries(vars).reduce(
    (out, [name, value]) => out.split(`{${name}}`).join(String(value)),
    template,
  );
}

export function tStatic(key: string, vars?: Record<string, string | number>): string {
  return interpolate(activeLocale === "zh-CN" ? (zhCN[key] ?? key) : key, vars);
}