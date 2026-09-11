import { useI18n } from "@/i18n";
import { useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Toaster } from "@/components/ui/Toaster";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/stores/auth";
import { useReadiness } from "@/hooks/useAdmin";
import { toast } from "@/stores/toast";

const HIGHLIGHTS: Array<{ icon: IconName; title: string; body: string }> = [
  {
    icon: "layers",
    title: "Five agents, one supervisor",
    body: "Monitor, audience, creative, bidding and optimize run as a LangGraph loop with typed, accumulating state.",
  },
  {
    icon: "shield",
    title: "Approval before execution",
    body: "The optimizer only proposes. Budget, bid and creative changes are applied after a human approves, and every step is audited.",
  },
  {
    icon: "activity",
    title: "Live, resumable runs",
    body: "Progress streams over server-sent events with per-run sequence numbers, so a reconnect never loses the timeline.",
  },
  {
    icon: "wallet",
    title: "Model spend guardrails",
    body: "Retries, caching, provider fallback and a monthly token budget are enforced in the gateway, not left to chance.",
  },
];

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useAuth((state) => state.login);
  const status = useAuth((state) => state.status);
  const user = useAuth((state) => state.user);
  const { data: readiness } = useReadiness({ refetchMs: 60_000 });
  const { t, locale, setLocale } = useI18n();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | undefined>();

  const from = (location.state as { from?: string } | null)?.from ?? "/dashboard";

  useEffect(() => {
    if (status === "authenticated" && user) navigate(from, { replace: true });
  }, [status, user, navigate, from]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setFieldError(undefined);
    try {
      const account = await login(email.trim(), password);
      toast.success(t("Welcome back, {name}", { name: account.full_name || account.email }));
      navigate(from, { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setError(t("Those credentials were not accepted."));
        setFieldError(t("Check the email and password, then try again."));
      } else if (caught instanceof ApiError && caught.status === 429) {
        setError(t("Too many sign-in attempts. Wait a moment before retrying."));
      } else {
        setError(caught instanceof Error ? caught.message : t("Sign-in failed."));
      }
    }
  };

  const backendReady = readiness?.status === "ready";

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.05fr_minmax(24rem,0.85fr)]">
      <div className="relative hidden overflow-hidden border-r border-line bg-surface-1 lg:flex lg:flex-col lg:justify-between lg:p-10">
        <div
          className="pointer-events-none absolute inset-0 opacity-70"
          style={{
            backgroundImage:
              "radial-gradient(900px 420px at 12% -8%, rgba(59,130,246,0.22), transparent 60%), radial-gradient(700px 380px at 95% 110%, rgba(34,211,238,0.16), transparent 62%)",
          }}
        />
        <div className="relative">
          <div className="flex items-center gap-3">
            <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-accent-400 text-white shadow-lg shadow-brand-600/30">
              <Icon name="target" size={20} />
            </span>
            <div>
              <p className="text-base font-semibold text-ink-1">AdOptimizer</p>
              <p className="text-xs text-ink-3">{t("Multi-agent advertising optimization platform")}</p>
            </div>
          </div>

          <h1 className="mt-12 max-w-lg text-3xl leading-tight font-semibold tracking-tight text-ink-1">
            {t("A production control room for paid media, not a demo script.")}
          </h1>
          <p className="mt-3 max-w-lg text-sm leading-relaxed text-ink-2">
            {t("Deterministic statistics decide what is true about your delivery. Models only write copy and justify trade-offs. Nothing reaches an ad platform without an explicit approval.")}
          </p>

          <ul className="mt-9 grid max-w-xl gap-4 sm:grid-cols-2">
            {HIGHLIGHTS.map((item) => (
              <li key={item.title} className="card p-3.5">
                <span className="grid size-7 place-items-center rounded-lg border border-brand-600/30 bg-brand-600/12 text-brand-300">
                  <Icon name={item.icon} size={14} />
                </span>
                <p className="mt-2.5 text-[13px] font-semibold text-ink-1">{t(item.title)}</p>
                <p className="mt-1 text-xs leading-relaxed text-ink-3">{t(item.body)}</p>
              </li>
            ))}
          </ul>
        </div>

        <p className="relative mt-10 text-[11px] text-ink-3">
          {t("Wilson intervals and empirical-Bayes shrinkage keep low-volume campaigns from producing confident nonsense.")}
        </p>
      </div>

      <div className="flex items-center justify-center bg-surface-0 px-5 py-10 sm:px-10">
        <div className="w-full max-w-sm">
          <div className="mb-7 flex items-center gap-2.5 lg:hidden">
            <span className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-accent-400 text-white">
              <Icon name="target" size={18} />
            </span>
            <p className="text-sm font-semibold text-ink-1">AdOptimizer</p>
          </div>

          <div className="mb-3 flex justify-end">
            <button
              type="button"
              onClick={() => setLocale(locale === "zh-CN" ? "en" : "zh-CN")}
              className="rounded-md border border-line px-2 py-1 text-[11px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1"
            >
              {locale === "zh-CN" ? "English" : "简体中文"}
            </button>
          </div>

          <h2 className="text-xl font-semibold tracking-tight text-ink-1">{t("Sign in")}</h2>
          <p className="mt-1 text-[13px] text-ink-3">
            {t("Use the account your administrator provisioned.")}
          </p>

          <div className="mt-4 flex items-center gap-2 rounded-lg border border-line bg-surface-1 px-3 py-2">
            <span
              className={cn("size-2 shrink-0 rounded-full", backendReady ? "bg-pos" : "bg-warn")}
            />
            <span className="text-[11px] text-ink-3">
              {readiness
                ? backendReady
                  ? t("API ready · v{version} · {environment}", { version: readiness.version, environment: readiness.environment })
                  : t("API degraded · {count} dependencies checked", { count: Object.keys(readiness.dependencies).length })
                : t("Checking API reachability…")}
            </span>
          </div>

          <form className="mt-5 flex flex-col gap-3.5" onSubmit={(event) => void submit(event)}>
            {error && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-lg border border-neg/35 bg-neg/8 px-3 py-2.5"
              >
                <Icon name="warning" size={15} className="mt-0.5 shrink-0 text-neg" />
                <p className="text-xs text-ink-1">{error}</p>
              </div>
            )}

            <TextField
              label={t("Work email")}
              type="email"
              name="email"
              autoComplete="username"
              placeholder="you@company.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              autoFocus
            />
            <TextField
              label={t("Password")}
              type="password"
              name="password"
              autoComplete="current-password"
              placeholder="••••••••••••"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              error={fieldError}
              required
            />

            <Button
              type="submit"
              variant="primary"
              size="md"
              className="mt-1 w-full"
              loading={status === "authenticating"}
              disabled={!email || !password}
            >
              {t("Sign in")}
            </Button>
          </form>

          <div className="card mt-6 p-3.5">
            <p className="text-[11px] font-semibold tracking-wide text-ink-3 uppercase">
              {t("Local development")}
            </p>
            <p className="mt-1.5 text-xs leading-relaxed text-ink-2">
              {t("The bootstrap administrator is created on first start from")}{" "}
              <code className="rounded bg-surface-3 px-1 py-0.5 font-mono text-[11px] text-brand-300">
                SECURITY__BOOTSTRAP_ADMIN_EMAIL
              </code>
              {t(". Rotate it immediately after the first sign-in — the account is flagged must-change-password.")}
            </p>
            <button
              type="button"
              onClick={() => {
                setEmail("admin@adoptimizer.dev");
                setPassword("Adm1n!ChangeMe");
              }}
              className="mt-2.5 inline-flex items-center gap-1.5 text-[11px] font-medium text-brand-300 hover:text-brand-400"
            >
              <Icon name="plus" size={11} />
              {t("Fill the default bootstrap credentials")}
            </button>
          </div>
        </div>
      </div>
      <Toaster />
    </div>
  );
}