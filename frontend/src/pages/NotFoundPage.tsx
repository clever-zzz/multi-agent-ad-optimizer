import { useI18n } from "@/i18n";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

export function NotFoundPage() {
  const { t } = useI18n();
  return (
    <div className="grid min-h-[70vh] place-items-center px-6">
      <div className="max-w-md text-center">
        <span className="mx-auto grid size-14 place-items-center rounded-2xl border border-line-strong bg-surface-1 text-ink-3">
          <Icon name="search" size={24} />
        </span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight text-ink-1">{t("Page not found")}</h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-3">
          {t("That route does not exist in the console. It may have been renamed, or the link you followed is out of date.")}
        </p>
        <div className="mt-6 flex items-center justify-center gap-2">
          <Link to="/dashboard">
            <Button variant="primary" icon="grid" size="md">
              {t("Back to dashboard")}
            </Button>
          </Link>
          <Link to="/campaigns">
            <Button variant="secondary" icon="megaphone" size="md">
              {t("Campaigns")}
            </Button>
          </Link>
        </div>
      </div>
    </div>
  );
}