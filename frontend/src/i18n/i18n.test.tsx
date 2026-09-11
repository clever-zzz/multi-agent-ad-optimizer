import { beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { I18nProvider, useI18n } from "@/i18n";

function Probe() {
  const { t, locale, setLocale } = useI18n();
  return (
    <div>
      <span data-testid="label">{t("Sign in")}</span>
      <span data-testid="count">{t("{count} unresolved", { count: 3 })}</span>
      <span data-testid="missing">{t("Untranslated probe string")}</span>
      <span data-testid="locale">{locale}</span>
      <button type="button" onClick={() => setLocale(locale === "zh-CN" ? "en" : "zh-CN")}>
        toggle
      </button>
    </div>
  );
}

describe("i18n", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("renders the source string when the locale is English", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(screen.getByTestId("label")).toHaveTextContent("Sign in");
  });

  it("interpolates variables in the source locale", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(screen.getByTestId("count")).toHaveTextContent("3 unresolved");
  });

  it("falls back to the key for untranslated strings instead of rendering blank", async () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(screen.getByTestId("missing")).toHaveTextContent("Untranslated probe string");
  });

  it("switches to zh-CN, translates and persists the preference", async () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(screen.getByTestId("locale")).toHaveTextContent("zh-CN");
    expect(screen.getByTestId("label")).toHaveTextContent("登录");
    expect(screen.getByTestId("count")).toHaveTextContent("3 条未解决");
    expect(window.localStorage.getItem("adoptimizer.locale")).toBe("zh-CN");
  });
});