import type { Translate } from "@/i18n/I18nContext";

// Select options are declared as module-level constants so their "value" stays a
// literal union member. Labels are English source keys and are resolved through
// the dictionary at render time, which keeps the constants locale-independent.
export interface LabeledOption<V extends string = string> {
  value: V;
  label: string;
  disabled?: boolean;
}

export function localizeOptions<V extends string>(
  t: Translate,
  options: ReadonlyArray<LabeledOption<V>>,
): Array<LabeledOption<V>> {
  return options.map((option) => ({ ...option, label: t(option.label) }));
}
