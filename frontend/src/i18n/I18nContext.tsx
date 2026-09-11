import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { detectLocale, persistLocale, type Locale } from "@/i18n/locale";
import { interpolate, setActiveLocale } from "@/i18n/translate";
import { zhCN } from "@/i18n/locales/zh";

export type Translate = (key: string, vars?: Record<string, string | number>) => string;

export interface I18nValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translate;
}

function translateFor(locale: Locale): Translate {
  return (key, vars) => interpolate(locale === "zh-CN" ? (zhCN[key] ?? key) : key, vars);
}

// Falling back to a usable default (instead of throwing) keeps standalone renders
// in tests and storybook-style harnesses working without a provider wrapper.
const defaultValue: I18nValue = {
  locale: "en",
  setLocale: () => undefined,
  t: translateFor("en"),
};

const I18nContext = createContext<I18nValue>(defaultValue);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(detectLocale);

  useEffect(() => {
    setActiveLocale(locale);
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    persistLocale(next);
    setLocaleState(next);
  }, []);

  const value = useMemo<I18nValue>(
    () => ({ locale, setLocale, t: translateFor(locale) }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  return useContext(I18nContext);
}