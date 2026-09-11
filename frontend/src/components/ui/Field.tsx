import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { useId } from "react";
import { tStatic } from "@/i18n/translate";
import { cn } from "@/lib/cn";

export interface FieldShellProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
  children: (id: string) => ReactNode;
}

export function FieldShell({ label, hint, error, required, className, children }: FieldShellProps) {
  const id = useId();
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && (
        <label htmlFor={id} className="text-xs font-medium text-ink-2">
          {label}
          {required && <span className="ml-1 text-neg">*</span>}
        </label>
      )}
      {children(id)}
      {error ? (
        <p className="text-xs text-neg">{error}</p>
      ) : hint ? (
        <p className="text-xs text-ink-3">{hint}</p>
      ) : null}
    </div>
  );
}

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  wrapClassName?: string;
}

export function TextField({
  label,
  hint,
  error,
  wrapClassName,
  className,
  required,
  ...rest
}: TextFieldProps) {
  return (
    <FieldShell
      label={label}
      hint={hint}
      error={error}
      required={required}
      className={wrapClassName}
    >
      {(id) => (
        <input
          id={id}
          required={required}
          aria-invalid={error ? true : undefined}
          className={cn("field", error && "border-neg/60", className)}
          {...rest}
        />
      )}
    </FieldShell>
  );
}

export interface SelectFieldProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  wrapClassName?: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  placeholder?: string;
}

export function SelectField({
  label,
  hint,
  error,
  wrapClassName,
  className,
  options,
  placeholder,
  required,
  ...rest
}: SelectFieldProps) {
  return (
    <FieldShell
      label={label}
      hint={hint}
      error={error}
      required={required}
      className={wrapClassName}
    >
      {(id) => (
        <div className="relative">
          <select
            id={id}
            required={required}
            className={cn("field appearance-none pr-8", error && "border-neg/60", className)}
            {...rest}
          >
            {placeholder !== undefined && <option value="">{placeholder}</option>}
            {options.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {tStatic(option.label)}
              </option>
            ))}
          </select>
          <svg
            viewBox="0 0 24 24"
            className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-ink-3"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="m6 9 6 6 6-6" />
          </svg>
        </div>
      )}
    </FieldShell>
  );
}

export interface TextAreaFieldProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  wrapClassName?: string;
}

export function TextAreaField({
  label,
  hint,
  error,
  wrapClassName,
  className,
  required,
  ...rest
}: TextAreaFieldProps) {
  return (
    <FieldShell
      label={label}
      hint={hint}
      error={error}
      required={required}
      className={wrapClassName}
    >
      {(id) => (
        <textarea
          id={id}
          required={required}
          aria-invalid={error ? true : undefined}
          className={cn("field resize-y leading-relaxed", error && "border-neg/60", className)}
          {...rest}
        />
      )}
    </FieldShell>
  );
}