import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ActionItem } from "@/components/ActionItem";
import type { OptimizationAction } from "@/lib/types";

function makeAction(overrides: Partial<OptimizationAction> = {}): OptimizationAction {
  return {
    id: "act_1",
    run_id: "run_1",
    campaign_id: "camp_a",
    creative_id: null,
    action_type: "adjust_budget",
    status: "proposed",
    before_value: "1000.00",
    after_value: "500.00",
    reason: "ROAS 3.74 trails the portfolio, pulling spend back",
    confidence: 0.75,
    proposed_by: "optimize",
    direction: "decrease",
    basis: { metric: "roas", reference: "portfolio" },
    created_at: null,
    approved_by: null,
    executed_at: null,
    external_reference: null,
    error_message: null,
    ...overrides,
  };
}

function renderItem(
  action: OptimizationAction,
  props: Partial<Parameters<typeof ActionItem>[0]> = {},
): ReturnType<typeof render> {
  return render(
    <MemoryRouter>
      <ActionItem action={action} {...props} />
    </MemoryRouter>,
  );
}

describe("ActionItem", () => {
  it("says which way the proposal moves spend and what it was judged against", () => {
    renderItem(makeAction());

    expect(screen.getByText("moves spend down")).toBeInTheDocument();
    expect(screen.getByText("judged against portfolio")).toBeInTheDocument();
  });

  it("makes a withheld proposal reviewable instead of hiding it", () => {
    renderItem(makeAction({ status: "suppressed" }), { canApprove: true, onApprove: vi.fn() });

    expect(screen.getByText("Withheld by the critic. Approving overrules it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Overrule the critic" })).toBeInTheDocument();
  });

  it("overrules without asking to execute, because approval comes first", async () => {
    const onApprove = vi.fn();
    renderItem(makeAction({ status: "suppressed" }), {
      canApprove: true,
      canExecute: true,
      onApprove,
      onExecute: vi.fn(),
    });

    await userEvent.click(screen.getByRole("button", { name: "Overrule the critic" }));

    expect(onApprove).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("button", { name: "Approve & execute" })).not.toBeInTheDocument();
  });

  it("keeps the ordinary approve path for a pending proposal", () => {
    renderItem(makeAction(), { canApprove: true, onApprove: vi.fn() });

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Overrule the critic" })).not.toBeInTheDocument();
    expect(
      screen.queryByText("Withheld by the critic. Approving overrules it."),
    ).not.toBeInTheDocument();
  });
});
