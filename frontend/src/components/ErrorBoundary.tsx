import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

export interface ErrorBoundaryProps {
  children: ReactNode;
  // Changing this value resets the boundary, which lets a route change recover
  // without a full page reload.
  resetKey?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

// React Query handles failed requests; this catches render-time failures so one
// broken view cannot blank the whole console.
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Structured so a log shipper can pick the fields out of the console stream.
    console.error("ui_render_error", {
      message: error.message,
      stack: error.stack,
      componentStack: info.componentStack,
    });
  }

  override componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  private reload = (): void => {
    this.setState({ error: null });
    window.location.reload();
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="grid min-h-[60vh] place-items-center px-6">
        <div className="max-w-lg text-center">
          <span className="mx-auto grid size-12 place-items-center rounded-xl border border-neg/30 bg-neg/10 text-neg">
            <Icon name="warning" size={22} />
          </span>
          <h1 className="mt-4 text-lg font-semibold text-ink-1">This view failed to render</h1>
          <p className="mt-2 text-sm leading-relaxed text-ink-3">
            The rest of the console is still usable. Reloading re-fetches everything from the API;
            nothing you approved is lost, because approvals are committed server-side.
          </p>
          <pre className="mt-4 max-h-40 overflow-auto rounded-lg border border-line bg-surface-1 p-3 text-left font-mono text-[11px] leading-relaxed text-neg">
            {error.message}
          </pre>
          <div className="mt-5 flex items-center justify-center gap-2">
            <Button variant="primary" icon="refresh" size="md" onClick={this.reload}>
              Reload console
            </Button>
            <Button variant="ghost" size="md" onClick={() => this.setState({ error: null })}>
              Dismiss
            </Button>
          </div>
        </div>
      </div>
    );
  }
}