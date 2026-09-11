export type Locale = "en" | "zh-CN";

export const SUPPORTED_LOCALES: Locale[] = ["en", "zh-CN"];

export const LOCALE_LABELS: Record<Locale, string> = {
  en: "English",
  "zh-CN": "简体中文",
};

const STORAGE_KEY = "adoptimizer.locale";

export function detectLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "en" || stored === "zh-CN") return stored;
  } catch {
    // storage can be unavailable (private mode); fall through to detection
  }
  const nav = typeof navigator === "undefined" ? "en" : navigator.language;
  return nav.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

export function persistLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    // ignore: the preference simply will not survive a reload
  }
}